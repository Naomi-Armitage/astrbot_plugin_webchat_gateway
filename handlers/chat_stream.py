"""Streaming /chat/stream HTTP handlers (POST + resume + cancel).

Split from handlers/chat.py for maintainability. Shares ChatDeps,
_parse_chat_body, _HEARTBEAT_INTERVAL etc. with the non-streaming
/chat handler via chat_common.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

from aiohttp import web

from astrbot.api import logger

from ..core.image_bridge import is_image_command
from ..core.stream_registry import STREAM_ID_PATTERN
from .chat_common import (
    ChatDeps,
    _HEARTBEAT_INTERVAL,
    prepare_chat_request,
)
from .common import (
    build_cors_headers,
    gate_request,
    json_response,
)


async def _open_stream_or_429(
    deps: ChatDeps,
    prepared,
    user_attachments_payload: list[dict],
):
    """Open the stream on the registry. Returns the StreamHandle on
    success, or an already-CORS'd 429 JSON Response when the per-token
    concurrency slot is held by another caller.

    Lock contention is a precondition failure, not a stream error: the
    client never gets a 200 SSE response. Returning JSON keeps parity
    with /chat so the frontend's existing 429 handler covers both
    endpoints.
    """
    handle_obj = await deps.registry.open(
        token_name=prepared.token.name,
        session_id=prepared.data.session_id,
        attachments=user_attachments_payload,
        attachment_file_ids=[r.file_id for r in prepared.attachment_rows],
    )
    if handle_obj is None:
        await deps.audit.write(
            "concurrent_block", name=prepared.token.name, ip=prepared.ip, detail=None
        )
        return json_response(
            {"error": "concurrent_request"},
            status=429,
            origin=prepared.origin,
            allowed_origins=prepared.allowed,
            same_origin_host=prepared.same_host,
        )
    return handle_obj


async def _check_daily_quota_or_429(
    deps: ChatDeps,
    handle_obj,
    prepared,
    today,
):
    """Check the per-token daily quota under the registry lock, before
    any SSE byte hits the wire. Returns the current `today_count` on
    success, or a JSON 429 Response when the quota is already exhausted
    (the handle is `close_failed("quota_exceeded")` first so the lock
    releases and any peer subscriber sees the terminal frame).
    """
    try:
        today_count = await deps.storage.get_today_usage(
            prepared.token.name, day=today
        )
    except Exception:
        logger.exception("[WebChatGateway] get_today_usage failed")
        today_count = 0
    if today_count >= prepared.token.daily_quota:
        await deps.registry.close_failed(handle_obj, error_code="quota_exceeded")
        await deps.audit.write(
            "quota_exceeded",
            name=prepared.token.name,
            ip=prepared.ip,
            detail={
                "today_count": today_count,
                "quota": prepared.token.daily_quota,
            },
        )
        return json_response(
            {
                "error": "quota_exceeded",
                "remaining": 0,
                "daily_quota": prepared.token.daily_quota,
            },
            status=429,
            origin=prepared.origin,
            allowed_origins=prepared.allowed,
            same_origin_host=prepared.same_host,
        )
    return today_count


async def _resolve_attachment_image_urls(file_store, attachment_rows) -> list[str]:
    """Resolve attachments to provider-visible URLs (local paths or
    file:// URLs). `open_local_path` lazily fetches the bytes for R2
    before returning a path; for LocalFileStore it's a no-op. A partial
    set on failure is accepted (e.g. one of three images can't be
    resolved) — the provider gets the others, the user sees the bubble
    with all three thumbnails (rendered from `attachments`, not
    image_urls), and the failure is logged for the operator.
    """
    image_urls: list[str] = []
    for row in attachment_rows:
        try:
            local_path = await file_store.open_local_path(storage_key=row.storage_key)
        except Exception:
            logger.exception(
                "[WebChatGateway] open_local_path failed key=%s", row.storage_key
            )
            local_path = None
        if local_path:
            image_urls.append(local_path)
        else:
            logger.warning(
                "[WebChatGateway] attachment unresolved file_id=%s", row.file_id
            )
    return image_urls


async def _close_failed_quietly(
    deps: ChatDeps, handle_obj, *, error_code: str, log_label: str
) -> None:
    """Best-effort `registry.close_failed` used by the SSE handshake
    error paths, where a secondary failure to close the handle must
    not mask the original exception. Logs an exception traceback so
    the operator can see both failures.
    """
    try:
        await deps.registry.close_failed(handle_obj, error_code=error_code)
    except Exception:
        logger.exception(
            "[WebChatGateway] close_failed during %s raised sid=%s",
            log_label,
            handle_obj.stream_id,
        )


async def _emit_terminal_safety_net(
    deps: ChatDeps, handle_obj, *, collected: list[str], terminal_emitted: bool
) -> None:
    """Belt-and-suspenders for the outermost finally: if any path above
    forgot to call a `registry.close_*`, the lock would stay held
    indefinitely. `close_failed` is idempotent — the registry no-ops on
    an already-closed handle (see core/stream_registry.py).

    Reaching this branch with `terminal_emitted=False` means a code
    path returned without setting the flag — almost always a refactor
    bug. Log it loudly (with a stack via `stack_info=True`) so the
    operator can find it; otherwise the buffer transitions to
    `closed_failed("internal_error")` silently and any peer device
    that resumes onto it sees a generic error frame with no
    server-side trace.
    """
    if terminal_emitted:
        return
    logger.warning(
        "[WebChatGateway] stream handler missed terminal close "
        "(stream_id=%s, collected=%d) — forcing close_failed",
        handle_obj.stream_id,
        len("".join(collected)),
        stack_info=True,
    )
    try:
        await deps.registry.close_failed(handle_obj, error_code="internal_error")
    except Exception:
        logger.exception(
            "[WebChatGateway] terminal close_failed in finally raised"
        )


async def _drain_pull_task(task: asyncio.Task | None) -> None:
    """Cancel and await a stream-iter pull task so the inner `__anext__`
    finishes (cancelled or otherwise) before the generator is touched.
    Calling `stream_iter.aclose()` while a pull is in flight races with
    that pull and can raise "aclose() got called when the generator
    was already running" — drain first.
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration, Exception):
        pass


def make_chat_stream_handler(deps: ChatDeps):
    # SSE variant of /chat. Stream lifecycle is owned by StreamRegistry —
    # the lock is held by the registry across the LLM stream + persist, the
    # buffer carries chunks for resume/peer-attach, and `close_*` releases
    # the lock + emits the chat-sync `stream_ended` event.
    #
    # Key wire contract additions vs. the v1 stream:
    #   * First data frame is `{"stream_id": "..."}` so the client can
    #     persist for resume.
    #   * Each chunk frame carries `seq` (registry-assigned, monotonic).
    #   * `done`/`error` frames carry their own `seq` (last_chunk_seq + 1)
    #     and `done` carries `incomplete: bool`.
    #
    # Client-disconnect semantics: a write failure on this handler ONLY
    # tears down the live SSE response. The registry's driver task — i.e.
    # the LLM iteration itself — keeps running so the buffer fills, peer
    # subscribers continue receiving chunks, and the stream persists with
    # `incomplete=False` (full reply, just no live viewer for one POSTer).

    async def handle(request: web.Request) -> web.StreamResponse:
        # Steps 1-4: gate (origin / IP / auth) + body parse + attachment
        # ownership are all delegated to the shared preamble so /chat
        # and /chat/stream can't drift on the wire contract for any of
        # those failure cases. The streaming-specific bits (registry
        # open, SSE handshake, driver registration, quota check) pick
        # up right after.
        prepared = await prepare_chat_request(request, deps)
        if isinstance(prepared, web.Response):
            return prepared
        token = prepared.token
        origin = prepared.origin
        allowed = prepared.allowed
        same_host = prepared.same_host
        data = prepared.data
        # Defense in depth: /image commands must NOT stream. The client
        # already routes them to the non-streaming /chat path (only that
        # path runs the image bridge), but if a future client or a direct
        # caller POSTs an image command here, the SSE driver would feed
        # "/image ..." to the chat model as plain text and yield a bogus
        # text reply. Reject explicitly so image routing isn't enforced
        # only client-side.
        if is_image_command(data.message):
            return json_response(
                {"error": "image_not_streamable"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        attachment_rows = prepared.attachment_rows
        user_attachments_payload: list[dict] = (
            [{"file_id": r.file_id, "mime": r.mime} for r in attachment_rows]
            if attachment_rows
            else []
        )

        # Step 5: open stream via the registry (acquires per-token lock,
        # creates buffer entry and audits). chat-sync `stream_started` is
        # emitted only after the SSE handshake succeeds, so peer devices
        # do not attach to a stream the origin client never opened.
        handle_obj = await _open_stream_or_429(
            deps, prepared, user_attachments_payload
        )
        if isinstance(handle_obj, web.Response):
            return handle_obj

        # Register the driver Task IMMEDIATELY after open() returns a
        # handle, BEFORE any other awaitable (quota lookup, SSE setup).
        # The previous ordering called register_driver only at SSE
        # response setup time — so a shutdown landing in the gap
        # between open() and register_driver wouldn't see this driver
        # in `cancel_all_drivers()` and the handler kept writing SSE
        # bytes / appending chunks into a registry mid-teardown. The
        # window was small but spanned `storage.get_today_usage`, which
        # is exactly the kind of await that lets a shutdown sneak in.
        # The matching `unregister_driver` runs in the outermost
        # finally so the entry never outlives the request task.
        driver_task = asyncio.current_task()
        if driver_task is not None:
            deps.registry.register_driver(
                stream_id=handle_obj.stream_id,
                token_name=token.name,
                task=driver_task,
            )

        # Step 6: quota check — under the registry lock, before any SSE
        # bytes hit the wire so we can still return a JSON 429.
        today = date.today()
        quota_check = await _check_daily_quota_or_429(
            deps, handle_obj, prepared, today
        )
        if isinstance(quota_check, web.Response):
            return quota_check
        today_count = quota_check

        # Step 7: open SSE response. (Driver registration moved up to
        # immediately after open() — see the block above.)
        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        response = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                # nginx: disable response buffering. Apache mod_proxy obeys
                # this same header. Without it, intermediate proxies hold
                # chunks until a buffer fills, defeating streaming.
                "X-Accel-Buffering": "no",
            },
        )
        # SSE handshake + first frames. If the peer is already gone (which
        # can happen if a fast client retry beats the previous request to
        # response.prepare), `prepare` and `write` raise — without this
        # try/except the buffer would stay PENDING, the lock would stay
        # held, and a peer device resuming onto the stream_id from the
        # already-emitted `stream_started` event would see a stuck buffer.
        # Force close_failed("internal_error") + a logged stack so the
        # operator can see the failure in the AstrBot log instead of
        # debugging a silent "请求失败: internal_error" on the client.
        try:
            await response.prepare(request)
            # Comment frame so the browser sees bytes immediately and any
            # transparent proxy flushes its buffer.
            await response.write(b": ready\n\n")
            # Operational log for the admin panel's live viewer. Pairs
            # with the close_ok / close_failed lines so an operator
            # scanning the log can see when a stream opened and how
            # it ended.
            logger.info(
                "[WebChatGateway] /chat/stream open name=%s session=%s sid=%s",
                token.name,
                data.session_id,
                handle_obj.stream_id,
            )
            # First data frame announces the stream_id so the client can
            # persist it and reconnect via /resume on transient failure.
            await response.write(
                (
                    "data: "
                    + json.dumps(
                        {"stream_id": handle_obj.stream_id}, ensure_ascii=False
                    )
                    + "\n\n"
                ).encode("utf-8")
            )
            # Mark attachments committed BEFORE emitting stream_started so
            # that peer devices that immediately fetch /conversations/{id}
            # see the committed=1 state. mark_files_committed is idempotent;
            # re-commits no-op on the storage side.
            #
            # Failure here is FATAL — we used to silently swallow it,
            # which let the stream proceed and record_chat_pair eventually
            # write a CM ImageURLPart pointing at a committed=0 file → the
            # orphan GC would later delete the file and the next history
            # fetch would render a broken thumbnail. Propagate the
            # exception into the outer handler so close_failed runs (and
            # releases the files since user_message_emitted is still False).
            if attachment_rows:
                await deps.storage.mark_files_committed(
                    [r.file_id for r in attachment_rows],
                    now=int(time.time()),
                )
            # Now that the origin SSE is actually open and the client has
            # received the stream_id, announce the stream to peer devices.
            # This also emits the user's message_added event, so peers can
            # show the user bubble immediately before assistant chunks arrive.
            await deps.conv_service.emit_stream_started(
                token_name=token.name,
                session_id=data.session_id,
                user_text=data.message,
                stream_id=handle_obj.stream_id,
                attachments=user_attachments_payload,
            )
            # Once emit_stream_started has been awaited, the
            # message_added(user) event has been written to
            # webchat_updates AND peer devices have been notified —
            # they may have rendered the user bubble (including the
            # attachment file_ids) already. A subsequent close_failed
            # MUST NOT release those files: deleting them now would
            # 404 every `<img src>` on the peer side, leaving ghost
            # bubbles with broken thumbnails. The flag is checked in
            # `_release_attached_files`; trade-off documented on
            # `StreamHandle.user_message_emitted`. emit_stream_started
            # is documented "never raise", so this assignment runs
            # unconditionally once we get here.
            handle_obj.user_message_emitted = True
        except asyncio.CancelledError:
            logger.warning(
                "[WebChatGateway] SSE handshake cancelled sid=%s",
                handle_obj.stream_id,
                stack_info=True,
            )
            await _close_failed_quietly(
                deps, handle_obj,
                error_code="cancelled",
                log_label="SSE handshake cancellation cleanup",
            )
            raise
        except (ConnectionResetError, ConnectionError):
            # Peer dropped the connection between our 200/headers and
            # the first data frame (typical when a fast client retries
            # before the previous request settled, or when a flaky
            # transit RST'd the TCP session right after `: ready\n\n`).
            # This is operationally a `cancelled` — there's no
            # WebChatGateway-side fault to surface — so route it that
            # way instead of letting it land in the generic Exception
            # branch below and pollute the `internal_error` audit count
            # / dashboard alerts. info-level (not exception): a
            # traceback per peer-drop is noise. Return the response
            # (already headers-flushed) instead of re-raising — the
            # client is gone, so propagating would just trip aiohttp's
            # own peer-disconnect handling for nothing.
            logger.info(
                "[WebChatGateway] SSE handshake peer-dropped sid=%s",
                handle_obj.stream_id,
            )
            await _close_failed_quietly(
                deps, handle_obj,
                error_code="cancelled",
                log_label="handshake-drop cleanup",
            )
            return response
        except Exception:
            logger.exception(
                "[WebChatGateway] SSE handshake failed sid=%s",
                handle_obj.stream_id,
            )
            await _close_failed_quietly(
                deps, handle_obj,
                error_code="internal_error",
                log_label="SSE handshake cleanup",
            )
            raise

        collected: list[str] = []
        client_gone = False
        terminal_emitted = False  # registry.close_* called → don't double-close
        # Defensive sequence-monotonicity guard for terminal frames.
        # The registry hands back a fresh seq per chunk via `append`; in
        # the happy path `handle_obj.next_seq` advances to one past the
        # last appended seq and a terminal `done`/`error` frame uses
        # `next_seq` directly. Under extreme races (close_incomplete /
        # close_failed running concurrently with a buffer flush, or a
        # buffer driver that resets next_seq for any reason), next_seq
        # has in the past been observed equal to the last appended seq
        # instead of one-past, producing a non-monotonic `seq` in the
        # terminal frame the client sees. Tracking the last appended
        # seq locally and clamping `last_seq = max(next_seq,
        # last_appended_seq + 1)` on every terminal-frame write makes
        # this purely additive: nominal behavior is unchanged (next_seq
        # already equals last_appended_seq + 1 in the happy path), but
        # the pathological case can no longer leak into the wire.
        last_appended_seq = -1

        async def _write_frame(frame: bytes) -> bool:
            # Write returns when the chunk has been handed to the transport.
            # ConnectionResetError is the canonical "peer dropped" signal
            # from aiohttp; transport.is_closing() catches the half-closed
            # window where the FIN has arrived but the next write hasn't
            # tripped a reset yet.
            if request.transport is None or request.transport.is_closing():
                return False
            try:
                await response.write(frame)
            except (ConnectionResetError, ConnectionError):
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WebChatGateway] SSE write failed")
                return False
            return True

        # Resolve attachments to provider-visible URLs. See helper for
        # the full failure-mode rationale (we accept a partial set so a
        # single broken attachment doesn't tank a multi-image turn).
        image_urls = await _resolve_attachment_image_urls(
            deps.file_store, attachment_rows
        )

        stream = deps.llm_bridge.generate_reply_stream(
            token_name=token.name,
            session_id=data.session_id,
            username=data.username,
            message=data.message,
            image_urls=image_urls or None,
        )
        stream_iter = stream.__aiter__()
        # Persistent pull task across heartbeats. Reusing the same
        # task is the only correct way to combine a heartbeat timeout
        # with `__anext__`: `wait_for(shield(...))` on a *fresh*
        # `__anext__()` each iteration would leave the prior call
        # still running in the background, and the next iteration's
        # `__anext__()` would crash with "asynchronous generator is
        # already running". Here we drive a single in-flight Task and
        # only allocate a new one after the previous chunk has been
        # consumed (or the previous task has terminated).
        pull_task: asyncio.Task | None = None

        async def _drain_pull(task: asyncio.Task | None) -> None:
            await _drain_pull_task(task)

        try:
            try:
                while True:
                    if pull_task is None:
                        pull_task = asyncio.ensure_future(stream_iter.__anext__())
                    try:
                        # shield protects the persistent task from being
                        # cancelled when wait_for times out — the task
                        # keeps running in the background and we re-await
                        # it on the next iteration.
                        chunk = await asyncio.wait_for(
                            asyncio.shield(pull_task),
                            timeout=_HEARTBEAT_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        # No chunk in 20s — heartbeat keeps the connection
                        # alive across idle proxies and surfaces a peer
                        # disconnect on the next write. The pull_task is
                        # still in flight; we'll await it again next loop.
                        if not client_gone:
                            if not await _write_frame(b": keepalive\n\n"):
                                client_gone = True
                        # Either way, keep iterating — the LLM stream
                        # continues feeding the buffer for peer subscribers
                        # and partial-on-abort persistence even if the
                        # POSTer's transport has dropped.
                        continue
                    except StopAsyncIteration:
                        # The persistent task completed with end-of-stream.
                        pull_task = None
                        break

                    # Chunk consumed → task is done; the next iteration
                    # creates a fresh task for the next pull.
                    pull_task = None

                    collected.append(chunk)
                    seq = await deps.registry.append(handle_obj, chunk)
                    last_appended_seq = seq
                    if not client_gone:
                        frame = (
                            "data: "
                            + json.dumps(
                                {"chunk": chunk, "seq": seq}, ensure_ascii=False
                            )
                            + "\n\n"
                        ).encode("utf-8")
                        if not await _write_frame(frame):
                            client_gone = True
                            # Do NOT break: keep pulling so the buffer
                            # fills, peers see the rest, and the stream
                            # persists with the full reply.
            except RuntimeError as exc:
                code = str(exc)
                full_text = "".join(collected)
                last_seq = max(handle_obj.next_seq, last_appended_seq + 1)  # clamp — see init comment
                if code == "llm_timeout":
                    if collected:
                        # Aborted with content — persist as incomplete.
                        new_count = await deps.storage.increment_daily_usage(
                            token.name, day=today
                        )
                        remaining = max(0, token.daily_quota - new_count)
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text=full_text,
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="llm_timeout",
                            user_attachments=user_attachments_payload,
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "done": True,
                                            "seq": last_seq,
                                            "remaining": remaining,
                                            "daily_quota": token.daily_quota,
                                            "incomplete": True,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                    else:
                        # Zero chunks before timeout → no persist.
                        await deps.registry.close_failed(
                            handle_obj, error_code="llm_timeout"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "llm_timeout", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                else:
                    # `empty_reply` is a soft outcome (upstream returned
                    # finish_reason=stop with zero tokens) — by definition no
                    # chunks were yielded, so the close path is always
                    # close_failed, never close_incomplete. The error frame
                    # carries the specific code so the frontend can render
                    # actionable copy instead of generic "请求失败".
                    if code == "empty_reply":
                        await deps.registry.close_failed(
                            handle_obj, error_code="empty_reply"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "empty_reply", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                        await _drain_pull(pull_task)
                        pull_task = None
                        await stream_iter.aclose()
                        return response
                    logger.exception("[WebChatGateway] LLM stream failed")
                    if collected:
                        new_count = await deps.storage.increment_daily_usage(
                            token.name, day=today
                        )
                        remaining = max(0, token.daily_quota - new_count)
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text=full_text,
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="llm_call_failed",
                            user_attachments=user_attachments_payload,
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "done": True,
                                            "seq": last_seq,
                                            "remaining": remaining,
                                            "daily_quota": token.daily_quota,
                                            "incomplete": True,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                    else:
                        await deps.registry.close_failed(
                            handle_obj, error_code="llm_call_failed"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "llm_call_failed", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                # Drain pull then aclose the upstream generator so its
                # `finally` runs and the provider connection is released.
                await _drain_pull(pull_task)
                pull_task = None
                await stream_iter.aclose()
                return response
            except asyncio.CancelledError:
                # Server-initiated shutdown. Mark the buffer as failed so
                # the lock releases; subscribers see the error frame too.
                await _drain_pull(pull_task)
                pull_task = None
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass
                if not terminal_emitted:
                    if collected:
                        # Best-effort partial persist on shutdown.
                        try:
                            new_count = await deps.storage.increment_daily_usage(
                                token.name, day=today
                            )
                            remaining = max(0, token.daily_quota - new_count)
                        except Exception:
                            remaining = 0
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text="".join(collected),
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="cancelled",
                            user_attachments=user_attachments_payload,
                        )
                    else:
                        await deps.registry.close_failed(
                            handle_obj, error_code="cancelled"
                        )
                    terminal_emitted = True
                raise
            except Exception as exc:
                logger.exception("[WebChatGateway] LLM stream failed")
                last_seq = max(handle_obj.next_seq, last_appended_seq + 1)
                if collected:
                    new_count = await deps.storage.increment_daily_usage(
                        token.name, day=today
                    )
                    remaining = max(0, token.daily_quota - new_count)
                    await deps.registry.close_incomplete(
                        handle_obj,
                        user_text=data.message,
                        partial_text="".join(collected),
                        remaining=remaining,
                        daily_quota=token.daily_quota,
                        reason="internal_error",
                        user_attachments=user_attachments_payload,
                    )
                    terminal_emitted = True
                    if not client_gone:
                        await _write_frame(
                            (
                                "data: "
                                + json.dumps(
                                    {
                                        "done": True,
                                        "seq": last_seq,
                                        "remaining": remaining,
                                        "daily_quota": token.daily_quota,
                                        "incomplete": True,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            ).encode("utf-8")
                        )
                else:
                    await deps.registry.close_failed(
                        handle_obj, error_code="internal_error"
                    )
                    terminal_emitted = True
                    if not client_gone:
                        await _write_frame(
                            (
                                "data: "
                                + json.dumps(
                                    {"error": "internal_error", "seq": last_seq},
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            ).encode("utf-8")
                        )
                await _drain_pull(pull_task)
                pull_task = None
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass
                logger.debug(
                    "[WebChatGateway] stream handler exception suppressed: %s",
                    str(exc)[:200],
                )
                return response

            # Successful stream end. Persist + audit + done frame.
            full_reply = "".join(collected)
            last_seq = max(handle_obj.next_seq, last_appended_seq + 1)
            if not full_reply:
                # Provider returned end-of-stream without any chunks. No
                # persist, no quota — but distinct from `llm_timeout` for
                # observability.
                await deps.registry.close_failed(
                    handle_obj, error_code="empty_reply"
                )
                terminal_emitted = True
                if not client_gone:
                    await _write_frame(
                        (
                            "data: "
                            + json.dumps(
                                {"error": "empty_reply", "seq": last_seq},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        ).encode("utf-8")
                    )
                return response

            try:
                new_count = await deps.storage.increment_daily_usage(
                    token.name, day=today
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] increment_daily_usage failed; persisting anyway"
                )
                new_count = today_count + 1
            remaining = max(0, token.daily_quota - new_count)
            await deps.registry.close_ok(
                handle_obj,
                user_text=data.message,
                full_text=full_reply,
                remaining=remaining,
                daily_quota=token.daily_quota,
                user_attachments=user_attachments_payload,
            )
            terminal_emitted = True
            logger.info(
                "[WebChatGateway] /chat/stream close_ok name=%s sid=%s "
                "reply_len=%d remaining=%d",
                token.name,
                handle_obj.stream_id,
                len(full_reply),
                remaining,
            )
            if not client_gone:
                done_frame = (
                    "data: "
                    + json.dumps(
                        {
                            "done": True,
                            "seq": last_seq,
                            "remaining": remaining,
                            "daily_quota": token.daily_quota,
                            "incomplete": False,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
                await _write_frame(done_frame)
            return response
        finally:
            await _emit_terminal_safety_net(
                deps,
                handle_obj,
                collected=collected,
                terminal_emitted=terminal_emitted,
            )

    return handle


def make_chat_stream_resume_handler(deps: ChatDeps):
    """GET /chat/stream/{stream_id}/resume — replay missed chunks then
    attach as a live subscriber.

    Auth gate identical to the POST handler (origin/IP/bearer/revoked/expired).
    Cross-token resume returns 404 (NOT 403) so attackers can't enumerate
    stream existence across tokens. Cross-stream-id mismatch on the buffer
    is also 404. The resume call does NOT take the per-token lock — multiple
    subscribers attach freely; only the original POST holds the lock.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host

        stream_id = (request.match_info.get("stream_id") or "").strip()
        if not stream_id or not STREAM_ID_PATTERN.match(stream_id):
            return json_response(
                {"error": "invalid_stream_id"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        after_seq_raw = request.query.get("after_seq")
        try:
            after_seq = int(after_seq_raw) if after_seq_raw is not None else -1
        except (TypeError, ValueError):
            return json_response(
                {"error": "invalid_after_seq"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        # Allow -1 sentinel; clamp negative-but-not-(-1) to -1 so the client
        # is forgiving of edge cases without exposing a parser quirk.
        if after_seq < -1:
            after_seq = -1

        snapshot = await deps.registry.fetch(
            stream_id=stream_id, token_name=token.name
        )
        if snapshot is None:
            # `fetch` enforces the cross-token-returns-404 invariant: a
            # missing entry AND a wrong-owner entry both come back as
            # None, so there's no timing leak between the two cases.
            return json_response(
                {"error": "stream_not_found"},
                status=404,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # Heuristic for the `peer` audit flag: a STREAMING/PENDING stream
        # with a registered driver lock means another connection is the
        # POSTer; this resume call therefore comes from a peer (or the
        # same device after a network blip — indistinguishable, leans
        # peer-true). A CLOSED_* snapshot means the original POST has
        # already finished and any resumer is "self-after-the-fact".
        is_live = snapshot.state in ("pending", "streaming")
        peer = bool(is_live)
        await deps.audit.write(
            "chat_stream_resumed",
            name=token.name,
            ip=ip,
            detail={
                "stream_id": stream_id,
                "after_seq": after_seq,
                "peer": peer,
            },
        )

        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        response = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(b": ready\n\n")

        async def _write_frame(frame: bytes) -> bool:
            if request.transport is None or request.transport.is_closing():
                return False
            try:
                await response.write(frame)
            except (ConnectionResetError, ConnectionError):
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WebChatGateway] SSE resume write failed")
                return False
            return True

        # Replay all chunks with seq > after_seq. The snapshot from
        # registry.fetch contains the entire chunk list; filter on the
        # way out so we don't double-emit anything the client already
        # consumed.
        last_replayed = after_seq
        for seq, text in snapshot.chunks:
            if seq <= after_seq:
                continue
            ok = await _write_frame(
                (
                    "data: "
                    + json.dumps({"chunk": text, "seq": seq}, ensure_ascii=False)
                    + "\n\n"
                ).encode("utf-8")
            )
            if not ok:
                return response
            if seq > last_replayed:
                last_replayed = seq

        # If the snapshot was already terminal, emit the appropriate done
        # / error frame and return — no point attaching to a closed buffer.
        if snapshot.state in ("closed_ok", "closed_incomplete"):
            final = snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            done_payload = {
                "done": True,
                "seq": last_seq_terminal,
                "remaining": final.get("remaining", 0),
                "daily_quota": final.get("daily_quota", 0),
                "incomplete": snapshot.state == "closed_incomplete",
            }
            await _write_frame(
                ("data: " + json.dumps(done_payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            return response
        if snapshot.state == "closed_failed":
            final = snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            err_payload = {
                "error": final.get("error", "internal_error"),
                "seq": last_seq_terminal,
            }
            await _write_frame(
                ("data: " + json.dumps(err_payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            return response

        # Live subscription: yields (seq, text) tuples of chunks newer
        # than `last_replayed`. The iterator returns (StopAsyncIteration)
        # when the buffer reaches a terminal state OR is evicted; the
        # closing frame itself is read separately via registry.fetch
        # after the loop. Heartbeats keep the connection alive via the
        # same persistent-task pattern used by the POST handler.
        sub = deps.registry.buffer.iter_subscribe(stream_id, last_replayed)
        sub_iter = sub.__aiter__()
        pull_task: asyncio.Task | None = None

        async def _drain_pull(task: asyncio.Task | None) -> None:
            if task is None or task.done():
                return
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass

        try:
            while True:
                if pull_task is None:
                    pull_task = asyncio.ensure_future(sub_iter.__anext__())
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(pull_task), timeout=_HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    if not await _write_frame(b": keepalive\n\n"):
                        await _drain_pull(pull_task)
                        pull_task = None
                        return response
                    continue
                except StopAsyncIteration:
                    pull_task = None
                    break

                pull_task = None
                seq, text = event
                ok = await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {"chunk": text, "seq": seq}, ensure_ascii=False
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                if not ok:
                    return response
                if seq > last_replayed:
                    last_replayed = seq

            # Iterator returned → buffer is in a terminal state OR has
            # been evicted. Re-fetch via the registry to read the
            # terminal payload and decide which closing frame to emit.
            terminal_snapshot = await deps.registry.fetch(
                stream_id=stream_id, token_name=token.name
            )
            if terminal_snapshot is None:
                # Evicted before we could read the close. Surface as
                # stream_not_found so the client falls back to history.
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {
                                "error": "stream_not_found",
                                "seq": last_replayed + 1,
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            # In rare races, more chunks may have appeared between the
            # iterator returning and this fetch (close() sets the
            # terminal Event, but the buffer can have late chunks queued
            # ahead of close in the same coroutine — defensive only).
            # The snapshot from registry.fetch contains all chunks; only
            # emit those past last_replayed.
            for seq, text in terminal_snapshot.chunks:
                if seq <= last_replayed:
                    continue
                ok = await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {"chunk": text, "seq": seq}, ensure_ascii=False
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                if not ok:
                    return response
                if seq > last_replayed:
                    last_replayed = seq
            final = terminal_snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            if terminal_snapshot.state in ("closed_ok", "closed_incomplete"):
                done_payload = {
                    "done": True,
                    "seq": last_seq_terminal,
                    "remaining": final.get("remaining", 0),
                    "daily_quota": final.get("daily_quota", 0),
                    "incomplete": terminal_snapshot.state == "closed_incomplete",
                }
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(done_payload, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            if terminal_snapshot.state == "closed_failed":
                err_payload = {
                    "error": final.get("error", "internal_error"),
                    "seq": last_seq_terminal,
                }
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(err_payload, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            # Should be unreachable: iter_subscribe only returns on
            # terminal/evicted, and the snapshot is non-None here. Defensive
            # error frame keeps the wire contract clean.
            await _write_frame(
                (
                    "data: "
                    + json.dumps(
                        {"error": "internal_error", "seq": last_seq_terminal},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
            )
            return response
        except asyncio.CancelledError:
            await _drain_pull(pull_task)
            pull_task = None
            try:
                await sub_iter.aclose()
            except Exception:
                pass
            raise
        finally:
            await _drain_pull(pull_task)
            try:
                await sub_iter.aclose()
            except Exception:
                pass

    return handle


def make_chat_stream_cancel_handler(deps: ChatDeps):
    """POST /chat/stream/{stream_id}/cancel — user-initiated stop.

    The client-side AbortController only tears down the live SSE viewer;
    the LLM iteration continues server-side (by design, so peer devices
    and resume callers still get the full reply). This endpoint is the
    real "stop the task" signal: it cancels the driver Task registered by
    the POST handler, which raises asyncio.CancelledError inside the
    iteration loop. The existing CancelledError branch in that loop then
    persists whatever partial reply was collected (close_incomplete with
    reason="cancelled") or, if no chunks arrived yet, close_failed.

    Cross-token cancel returns 404 — identical to /resume — so an
    attacker cannot enumerate stream existence across tokens by probing
    cancel responses. A cancel for a stream that has already terminated
    naturally also returns 204; that's the inherent race between the
    client reading the `done` frame and clicking stop, and treating it
    as success keeps the client code simpler.
    """

    async def handle(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host

        stream_id = (request.match_info.get("stream_id") or "").strip()
        if not stream_id or not STREAM_ID_PATTERN.match(stream_id):
            return json_response(
                {"error": "invalid_stream_id"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # `cancel` enforces the cross-token-returns-404 invariant: both
        # "no such stream" and "stream owned by a different token" come
        # back as False, so no timing leak between the cases.
        ok = deps.registry.cancel(stream_id=stream_id, token_name=token.name)
        if not ok:
            return json_response(
                {"error": "stream_not_found"},
                status=404,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        try:
            await deps.audit.write(
                "chat_stream_cancelled",
                name=token.name,
                ip=ip,
                detail={"stream_id": stream_id},
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] audit chat_stream_cancelled failed sid=%s",
                stream_id,
            )

        # 204 with CORS headers so the browser doesn't strip the preflight
        # cache. No body — the client treats any 2xx as "cancel accepted"
        # and waits for the SSE `done`/`error` frame for the actual outcome.
        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        return web.Response(status=204, headers=cors)

    return handle
