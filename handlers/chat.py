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

import asyncio
import contextlib
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


async def _await_or_cancel_on_disconnect(
    request: web.Request, task: "asyncio.Task[Any]"
):
    """Await ``task`` but cancel it if the client disconnects.

    aiohttp's ``handler_cancellation`` defaults to False, so a client
    abort does NOT cancel this handler — image generation would
    otherwise run to its full timeout even after the user hit stop or
    closed the tab. We poll ``request.transport.is_closing()`` in a
    0.5s loop racing the generation task and cancel it on disconnect.
    Returns the task result on completion (re-raising whatever the task
    raised), or ``None`` if it was cancelled due to disconnect.

    Proxy buffering can delay the disconnect signal, so the bridge's own
    total timeout stays the hard backstop for the worst case.
    """
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if task in done:
            return task.result()
        if request.transport is None or request.transport.is_closing():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return None


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
            # `/img …` short-circuits the LLM call.
            #
            # Two sub-paths depending on attachments + operator config:
            #   * img2img (edits): when the operator opted into
            #     `image_gen.img2img` (edit-capable model) AND the user
            #     attached a reference image — route to the /images/edits
            #     endpoint with the input image. The reference image is
            #     committed + recorded on the user turn (so it shows in
            #     history AND the client's optimistic echo matches → no
            #     duplicate bubble).
            #   * text-only generation: otherwise. The /images/generations
            #     endpoint can't accept input images, so any attachment is
            #     released (quota doesn't leak).
            if is_image_command(data.message):
                use_img2img = (
                    bool(attachment_rows)
                    and deps.image_bridge is not None
                    and deps.image_bridge.edit_enabled
                )
                edit_bytes: bytes | None = None
                edit_mime = "image/png"
                # User-turn attachments to record — only the committed
                # reference image, and only on the img2img success path.
                image_user_attachments: list[dict] | None = None
                if use_img2img:
                    base_row = attachment_rows[0]
                    # Read the reference bytes for the multipart upload. The
                    # row is still committed=0 here; FileStore.read works on
                    # the stored bytes regardless of the commit flag. We
                    # commit only AFTER a successful edit (below), so a failed
                    # turn leaves the row committed=0 → swept by the 1h orphan
                    # GC (no leak, no release-on-failure bookkeeping needed).
                    try:
                        edit_bytes = await deps.file_store.read(
                            storage_key=base_row.storage_key
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] image edit: read base image failed"
                        )
                        edit_bytes = None
                    if edit_bytes:
                        edit_mime = base_row.mime or "image/png"
                    else:
                        # Couldn't read the reference → fall back to text2img.
                        use_img2img = False
                # Release attachments we won't use: all of them when not doing
                # img2img, or just the extras (v1 uses a single reference).
                drop_rows = attachment_rows if not use_img2img else attachment_rows[1:]
                if drop_rows:
                    try:
                        await release_files_safely(
                            storage=deps.storage,
                            file_store=deps.file_store,
                            rows=drop_rows,
                            log_label=(
                                "image_cmd_drop_attachments"
                                if not use_img2img
                                else "image_edit_drop_extra"
                            ),
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
                    if use_img2img:
                        gen_task = asyncio.create_task(
                            deps.image_bridge.edit(
                                prompt, edit_bytes, edit_mime, size=data.size
                            )
                        )
                    else:
                        gen_task = asyncio.create_task(
                            deps.image_bridge.generate(prompt, size=data.size)
                        )
                    # Make generation cancellable: race it against a
                    # client-disconnect poll. A cancel here lands BEFORE
                    # any commit / quota increment / record_chat_pair
                    # below, so a stopped generation charges no quota,
                    # writes no CM turn, and leaves an img2img reference
                    # committed=0 for the orphan GC — no leak.
                    result = await _await_or_cancel_on_disconnect(
                        request, gen_task
                    )
                    if result is None:
                        await deps.audit.write(
                            "image_cancelled",
                            name=token.name,
                            ip=ip,
                            detail={"prompt_len": len(prompt)},
                        )
                        return web.Response(status=499)
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
                # Reference image survived an img2img edit → commit it now so
                # it persists with the chat history and the user turn echoes
                # it. Matching the client's optimistic bubble is what makes
                # the events long-poll's message_added DEDUP (equal attachment
                # keys) instead of rendering a second, image-less copy of the
                # question. Commit-after-success means a failed edit left the
                # row committed=0 → swept by the orphan GC, no leak.
                if use_img2img:
                    try:
                        committed_ok = await commit_attachments_or_release(
                            storage=deps.storage,
                            file_store=deps.file_store,
                            rows=[attachment_rows[0]],
                            log_label="image_edit_commit",
                            audit=deps.audit,
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] image edit: commit base raised"
                        )
                        committed_ok = False
                    if committed_ok:
                        _base = attachment_rows[0]
                        image_user_attachments = [
                            {"file_id": _base.file_id, "mime": _base.mime}
                        ]
                new_count = await deps.storage.increment_daily_usage(
                    token.name, day=today
                )
                remaining = max(0, token.daily_quota - new_count)
                # Empty assistant text — the image IS the reply. A
                # placeholder string like "[已生成 1 张图片]" reads
                # as awkward filler in the chat bubble, and the
                # client renders the attachment grid as a Telegram-
                # style image-only bubble when the text is empty.
                # CM history sees an empty assistant turn here; the
                # next turn's prompt builder treats it the same as
                # any empty-content row (no context contribution).
                assistant_text = ""
                await deps.conv_service.record_chat_pair(
                    token_name=token.name,
                    session_id=data.session_id,
                    user_text=data.message,
                    assistant_text=assistant_text,
                    user_attachments=image_user_attachments,
                    assistant_attachments=[attachment],
                )
                await deps.audit.write(
                    "image_generated",
                    name=token.name,
                    ip=ip,
                    detail={
                        "prompt_len": len(prompt),
                        "model": deps.image_bridge.model,
                        "size": result.size,
                        "file_id": attachment["file_id"],
                    },
                )
                logger.info(
                    "[WebChatGateway] image generated name=%s model=%s "
                    "size=%s bytes=%d file_id=%s",
                    token.name,
                    deps.image_bridge.model,
                    result.size,
                    len(result.content),
                    attachment["file_id"],
                )
                return json_response(
                    {
                        "reply": assistant_text,
                        "attachments": [attachment],
                        "size": result.size,
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
