"""Non-streaming /chat HTTP handler + the preflight handler.

Streaming variants (/chat/stream POST + resume + cancel) live in
handlers/chat_stream.py; the file-auth cookie endpoints (/me +
/files/logout) live in handlers/chat_files_auth.py; shared types and
helpers (ChatDeps, _parse_chat_body, _HEARTBEAT_INTERVAL, etc.) live
in handlers/chat_common.py.

Re-exports the moved handler factories at module level so existing
`from .chat import make_chat_stream_handler` / `make_me_handler` etc.
callers continue to work after the split.
"""

from __future__ import annotations

from datetime import date

from aiohttp import web

from astrbot.api import logger

from ..core.file_lifecycle import (
    commit_attachments_or_release,
    release_files_safely,
)
from ..core.llm_bridge import map_llm_error
from ..storage.base import FileRow
from .chat_common import (  # noqa: F401  (ChatDeps re-exported for backward compat)
    ChatDeps,
    _HEARTBEAT_INTERVAL,
    _ParsedRequest,
    _ParseError,
    _is_expired,
    _parse_chat_body,
    _parse_payload,
)
from .chat_files_auth import (  # noqa: F401
    make_logout_handler,
    make_me_handler,
)
from .chat_stream import (  # noqa: F401
    make_chat_stream_cancel_handler,
    make_chat_stream_handler,
    make_chat_stream_resume_handler,
)
from .common import (
    extract_origin,
    gate_request,
    json_response,
    preflight_response,
)


def make_chat_handler(deps: ChatDeps):
    async def handle(request: web.Request) -> web.Response:
        # Origin allow-list → IP brute-force → bearer auth, all in one
        # shared helper so the non-stream /chat path can't drift from
        # the streaming sibling or any /conversations endpoint.
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host
        ip = gated.ip
        token = gated.token

        # Parse + length check (before taking the per-token lock so a slow
        # body cannot pin the slot). Shared with /chat/stream via
        # _parse_chat_body so the error-shape contract stays in lockstep.
        parsed = await _parse_chat_body(
            request, deps.max_message_length,
            max_attachments=deps.max_attachments_per_message,
            origin=origin, allowed=allowed, same_host=same_host,
        )
        if isinstance(parsed, web.Response):
            return parsed
        data = parsed

        # Validate attachment ownership before taking the per-token lock
        # so a stale or cross-token file_id can't pin the slot during the
        # storage round trip. Each attachment must belong to THIS token
        # AND THIS session — cross-session attachment reuse is rejected
        # to prevent a token from leaking a file_id into another's
        # session via the wire.
        attachment_rows: list[FileRow] = []
        if data.attachments:
            for fid in data.attachments:
                try:
                    row = await deps.storage.get_file(fid)
                except Exception:
                    logger.exception(
                        "[WebChatGateway] get_file failed file_id=%s", fid
                    )
                    return json_response(
                        {"error": "internal_error"},
                        status=500,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                if (
                    row is None
                    or row.token_name != token.name
                    or row.session_id != data.session_id
                ):
                    return json_response(
                        {"error": "invalid_attachment"},
                        status=400,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                attachment_rows.append(row)

        # 5. Concurrency lock
        async with deps.concurrency.acquire(token.name) as acquired:
            if not acquired:
                await deps.audit.write(
                    "concurrent_block", name=token.name, ip=ip, detail=None
                )
                return json_response(
                    {"error": "concurrent_request"},
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 6. Daily quota check (read-then-increment is racy across processes,
            # but per-token concurrency=1 guarantees serial use of a single token).
            today = date.today()
            today_count = await deps.storage.get_today_usage(token.name, day=today)
            if today_count >= token.daily_quota:
                await deps.audit.write(
                    "quota_exceeded",
                    name=token.name,
                    ip=ip,
                    detail={"today_count": today_count, "quota": token.daily_quota},
                )
                return json_response(
                    {
                        "error": "quota_exceeded",
                        "remaining": 0,
                        "daily_quota": token.daily_quota,
                    },
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 7. LLM call
            image_urls: list[str] = []
            attachment_committed = False
            if attachment_rows:
                # Commit before the LLM call so an in-flight failure doesn't
                # leave the file in `committed=0` limbo (the orphan GC would
                # eventually clean it up, but the user has clearly attached
                # this to a send — we'd rather keep the bytes around with
                # the same retention as the chat history). On LLM failure
                # below we release the files explicitly via try/finally
                # so a transient timeout doesn't permanently consume the
                # user's storage quota for a turn that produced no CM
                # record (the orphan GC only sweeps committed=0 rows).
                if not await commit_attachments_or_release(
                    storage=deps.storage,
                    file_store=deps.file_store,
                    rows=attachment_rows,
                    log_label="non_stream_chat",
                    audit=deps.audit,
                ):
                    # Commit failed AND the release attempt completed.
                    # 500 the request so the user retries; CM stays
                    # clean (no half-attached message_added).
                    return json_response(
                        {"error": "internal_error"},
                        status=500,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                attachment_committed = True
                for row in attachment_rows:
                    try:
                        local_path = await deps.file_store.open_local_path(
                            storage_key=row.storage_key
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] open_local_path failed key=%s",
                            row.storage_key,
                        )
                        local_path = None
                    if local_path:
                        image_urls.append(local_path)
                    else:
                        logger.warning(
                            "[WebChatGateway] attachment unresolved file_id=%s",
                            row.file_id,
                        )
            llm_succeeded = False
            try:
                try:
                    reply = await deps.llm_bridge.generate_reply(
                        token_name=token.name,
                        session_id=data.session_id,
                        username=data.username,
                        message=data.message,
                        image_urls=image_urls or None,
                    )
                    llm_succeeded = True
                except Exception as exc:
                    code, status, audit_event = map_llm_error(exc)
                    if status == 500:
                        # Internal exception text may leak provider
                        # names, paths, or context near credentials —
                        # keep in audit/log only and return a stable
                        # error code to the caller.
                        logger.exception("[WebChatGateway] LLM call failed")
                    await deps.audit.write(
                        audit_event,
                        name=token.name,
                        ip=ip,
                        detail=(
                            {"error": str(exc)[:200]}
                            if status == 500
                            else {"msg_len": len(data.message)}
                        ),
                    )
                    return json_response(
                        {"error": code},
                        status=status,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
            finally:
                # If the LLM call didn't return a reply (timeout / error
                # / empty), release the committed files so the user's
                # storage quota isn't permanently consumed by a turn
                # that has no CM record. The orphan GC only sweeps
                # `committed=0` rows; without this explicit release we'd
                # have permanently leaked the bytes for every failed
                # retry. Stream handler does the equivalent via
                # `StreamHandle.attachment_file_ids` + `close_failed`.
                if attachment_committed and not llm_succeeded:
                    try:
                        await release_files_safely(
                            storage=deps.storage,
                            file_store=deps.file_store,
                            rows=attachment_rows,
                            log_label="non_stream_chat_llm_fail",
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] release on llm-fail raised"
                        )

            # 8. Increment usage (atomic)
            new_count = await deps.storage.increment_daily_usage(token.name, day=today)
            remaining = max(0, token.daily_quota - new_count)

            # Record the user/assistant pair into the chat-sync event log so
            # peer devices on the same token long-poll their way to the new
            # state. record_chat_pair swallows its own errors — a failure
            # here must NOT block the chat reply that's already complete.
            user_attachments_payload: list[dict] = (
                [{"file_id": r.file_id, "mime": r.mime} for r in attachment_rows]
                if attachment_rows
                else []
            )
            await deps.conv_service.record_chat_pair(
                token_name=token.name,
                session_id=data.session_id,
                user_text=data.message,
                assistant_text=reply,
                user_attachments=user_attachments_payload,
            )

            # 9. Audit + respond
            await deps.audit.write(
                "chat_ok",
                name=token.name,
                ip=ip,
                detail={
                    "msg_len": len(data.message),
                    "reply_len": len(reply),
                    "remaining": remaining,
                },
            )
            return json_response(
                {
                    "reply": reply,
                    "remaining": remaining,
                    "daily_quota": token.daily_quota,
                },
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

    return handle


def make_preflight_handler(
    allowed: set[str],
    *,
    trust_referer_as_origin: bool = False,
):
    async def handle(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=trust_referer_as_origin
            ),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return handle
