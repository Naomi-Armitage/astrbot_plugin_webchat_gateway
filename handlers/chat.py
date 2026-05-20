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
from typing import Any

from aiohttp import web

from astrbot.api import logger

from ..core.file_lifecycle import (
    commit_attachments_or_release,
    release_files_safely,
)
from ..core.image_bridge import (
    ImageBridgeError,
    is_image_command,
    persist_generated_image,
    strip_image_prefix,
)
from ..core.llm_bridge import map_llm_error
from .chat_common import (  # noqa: F401  (ChatDeps re-exported for backward compat)
    ChatDeps,
    _HEARTBEAT_INTERVAL,
    _ParsedRequest,
    _ParseError,
    _is_expired,
    _parse_chat_body,
    _parse_payload,
    prepare_chat_request,
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
    json_response,
    preflight_response,
)


def make_chat_handler(deps: ChatDeps):
    async def handle(request: web.Request) -> web.Response:
        # Origin → IP → bearer auth → body parse → attachment ownership
        # are all delegated to the shared preamble so /chat and
        # /chat/stream can't drift on the wire contract for any of
        # those failure cases. Returns either a typed bundle or an
        # already-CORS'd error Response.
        prepared = await prepare_chat_request(request, deps)
        if isinstance(prepared, web.Response):
            return prepared
        token = prepared.token
        ip = prepared.ip
        origin = prepared.origin
        allowed = prepared.allowed
        same_host = prepared.same_host
        data = prepared.data
        attachment_rows = prepared.attachment_rows

        # Operational log for the admin panel's live log viewer. INFO
        # so a quiet plugin still has visible activity, but no user
        # content or full message — token name + length only. The
        # audit_log captures the same event with structured fields;
        # this is the human-readable mirror.
        logger.info(
            "[WebChatGateway] /chat received name=%s session=%s msg_len=%d attachments=%d",
            token.name,
            data.session_id,
            len(data.message),
            len(attachment_rows),
        )

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

            # Image-generation slash branch: `/image …` / `/draw …` /
            # `/img …` short-circuits the LLM call. Attachments that the
            # operator sent alongside an image command are ignored (the
            # OpenAI Images API doesn't accept input images on the
            # generation endpoint); we still release them so quota
            # doesn't leak.
            if is_image_command(data.message):
                if attachment_rows:
                    try:
                        await release_files_safely(
                            storage=deps.storage,
                            file_store=deps.file_store,
                            rows=attachment_rows,
                            log_label="image_cmd_drop_attachments",
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] image cmd: release on drop raised"
                        )
                if deps.image_bridge is None or not deps.image_bridge.enabled:
                    await deps.audit.write(
                        "image_failed",
                        name=token.name,
                        ip=ip,
                        detail={
                            "code": "image_disabled",
                            "prompt_len": len(data.message),
                        },
                    )
                    return json_response(
                        {"error": "image_disabled"},
                        status=503,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                prompt = strip_image_prefix(data.message)
                if not prompt:
                    return json_response(
                        {"error": "image_prompt_empty"},
                        status=400,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                try:
                    result = await deps.image_bridge.generate(prompt)
                except ImageBridgeError as exc:
                    status_code = 504 if exc.code == "image_timeout" else 502
                    if exc.code == "image_disabled":
                        status_code = 503
                    # Surface the upstream error string into the audit
                    # detail (truncated). Operators reading the audit
                    # log will see "upstream 400: Unknown parameter:
                    # 'response_format'" instead of just
                    # "image_call_failed" — the former actually tells
                    # them what to change. The string is the message
                    # arg the bridge passed when raising; for the
                    # "happy" disabled / timeout codes it's empty.
                    detail_str = str(exc)
                    audit_detail = {
                        "code": exc.code,
                        "prompt_len": len(prompt),
                    }
                    if detail_str and detail_str != exc.code:
                        audit_detail["upstream"] = detail_str[:200]
                    await deps.audit.write(
                        "image_failed",
                        name=token.name,
                        ip=ip,
                        detail=audit_detail,
                    )
                    error_body: dict[str, Any] = {"error": exc.code}
                    if detail_str and detail_str != exc.code:
                        # Echo to the client too so the chat bubble
                        # can show a one-line "失败: <upstream msg>"
                        # instead of an opaque error code.
                        error_body["detail"] = detail_str[:200]
                    return json_response(
                        error_body,
                        status=status_code,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                except Exception as exc:
                    logger.exception(
                        "[WebChatGateway] image gen unexpected failure"
                    )
                    await deps.audit.write(
                        "image_failed",
                        name=token.name,
                        ip=ip,
                        detail={
                            "code": "image_call_failed",
                            "error": str(exc)[:200],
                            "prompt_len": len(prompt),
                        },
                    )
                    return json_response(
                        {"error": "image_call_failed"},
                        status=502,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                import time as _time
                now_ts = int(_time.time())
                try:
                    attachment = await persist_generated_image(
                        storage=deps.storage,
                        file_store=deps.file_store,
                        token_name=token.name,
                        result=result,
                        now=now_ts,
                    )
                except Exception:
                    logger.exception(
                        "[WebChatGateway] image persist failed"
                    )
                    await deps.audit.write(
                        "image_failed",
                        name=token.name,
                        ip=ip,
                        detail={
                            "code": "image_call_failed",
                            "stage": "persist",
                            "prompt_len": len(prompt),
                        },
                    )
                    return json_response(
                        {"error": "image_call_failed"},
                        status=500,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                new_count = await deps.storage.increment_daily_usage(
                    token.name, day=today
                )
                remaining = max(0, token.daily_quota - new_count)
                # CM history can't usefully store binary image bytes,
                # so the assistant_text is a brief Chinese tag and the
                # actual image surfaces through the assistant_attachments
                # field that record_chat_pair forwards into the
                # message_added event.
                assistant_text = "[已生成 1 张图片]"
                await deps.conv_service.record_chat_pair(
                    token_name=token.name,
                    session_id=data.session_id,
                    user_text=data.message,
                    assistant_text=assistant_text,
                    assistant_attachments=[attachment],
                )
                await deps.audit.write(
                    "image_generated",
                    name=token.name,
                    ip=ip,
                    detail={
                        "prompt_len": len(prompt),
                        "model": deps.image_bridge.model,
                        "size": deps.image_bridge.size,
                        "file_id": attachment["file_id"],
                    },
                )
                logger.info(
                    "[WebChatGateway] image generated name=%s model=%s "
                    "size=%s bytes=%d file_id=%s",
                    token.name,
                    deps.image_bridge.model,
                    deps.image_bridge.size,
                    len(result.content),
                    attachment["file_id"],
                )
                return json_response(
                    {
                        "reply": assistant_text,
                        "attachments": [attachment],
                        "remaining": remaining,
                        "daily_quota": token.daily_quota,
                    },
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
            logger.info(
                "[WebChatGateway] /chat ok name=%s session=%s "
                "reply_len=%d remaining=%d",
                token.name,
                data.session_id,
                len(reply),
                remaining,
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
