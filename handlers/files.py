"""Upload + serve HTTP handlers for the image attachment feature.

Pipeline mirrors `/chat` for auth so an attacker can't use the upload
endpoint to enumerate tokens any cheaper than the chat endpoint itself:

    1. Origin allow-list
    2. IP brute-force gate
    3. Bearer auth (revoked + expired branches)
    4. Multipart parse + size check
    5. PIL verify + MIME whitelist
    6. Per-token storage quota
    7. Persist to FileStore + DB row (committed=0)
    8. Audit + respond

Serve endpoint is GET-only:

    1. Origin + IP + auth (same gate as upload)
    2. Strict file_id regex
    3. Storage lookup; cross-token returns 404 (no existence leak)
    4. R2-direct mode → 302 to presigned URL
    5. Else → proxy bytes back with the stored MIME

`make_files_preflight` is a thin CORS preflight wrapper so the upload UI
in the browser sees the response before the browser blocks the actual
POST. Same `preflight_response` helper /chat uses.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.auth import extract_bearer
from ..core.cookie_logout import CookieLogoutTracker
from ..core.file_cookie import (
    FILE_AUTH_COOKIE_NAME,
    verify as verify_file_cookie,
)
from ..core.file_store import FileStore
from ..core.image_util import (
    ALLOWED_MIME_TO_EXT,
    detect_image_mime_async,
    ext_for_mime,
)
from ..core.ip_guard import IpGuard
from ..core.ratelimit import PerTokenUploadGate
from ..storage.base import AbstractStorage
from .common import (
    build_cors_headers,
    client_ip,
    extract_origin,
    gate_request,
    is_origin_allowed,
    json_response,
    preflight_response,
)


# ^[a-zA-Z0-9_-]{16}$ — `secrets.token_urlsafe(12)` always emits 16 chars
# from the URL-safe base64 alphabet (no `+`/`/`/`=` padding). The strict
# regex on the serve endpoint defeats path-traversal probes that try to
# sneak `..` or `/` into the file_id slot.
_FILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{16}$")
_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.]{1,128}$")


@dataclass
class UploadDeps:
    """Wiring surface for the upload + serve handlers.

    Carries the same auth-side deps `ChatDeps` does (storage, audit,
    ip_guard, origin policy) plus the upload-specific knobs (FileStore,
    size caps, MIME whitelist, R2 serving mode).

    `file_store` is opaque here — handlers call `save`/`read`/`signed_url`
    via the Protocol so swapping LocalFileStore ↔ R2FileStore is a config
    change, not a code change.
    """

    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    file_store: FileStore
    upload_gate: PerTokenUploadGate
    allowed_origins: set[str]
    max_file_size_mb: int
    per_token_storage_mb: int
    allowed_mime: tuple[str, ...]
    storage_driver: str  # "local" | "r2"
    r2_serving_mode: str  # "proxy" | "direct"
    r2_direct_link_ttl_seconds: int
    files_serve_prefix: str  # e.g. "/api/webchat/files/"
    trust_forwarded_for: bool
    # HMAC secret for the file-auth cookie. The serve endpoint accepts
    # `Authorization: Bearer` headers AND a path-scoped cookie issued
    # by /me — neither exposes the bearer in URLs. Empty bytes = cookie
    # auth disabled (header-only).
    file_cookie_secret: bytes = b""
    # In-memory tracker for server-side cookie invalidation on logout.
    # When the user logs out, `make_logout_handler` records the
    # token_name → now; the serve endpoint here checks the tracker
    # after a successful HMAC verify and rejects cookies whose
    # `exp_ts` falls inside the invalidation window. Without this
    # check, a "logout" only clears the browser-side cookie but the
    # server still accepts any HMAC-valid cookie until natural expiry.
    cookie_logout_tracker: CookieLogoutTracker | None = None
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


def make_upload_handler(deps: UploadDeps):
    """POST {prefix}/upload — multipart image upload.

    Single file per request, called repeatedly for batches by the
    composer. Lock-free: uploads are file I/O, not LLM serialization, so
    a token's concurrent uploads run in parallel.
    """

    max_size_bytes = deps.max_file_size_mb * 1024 * 1024
    per_token_quota_bytes = deps.per_token_storage_mb * 1024 * 1024
    allowed_mime: set[str] = set(deps.allowed_mime)

    async def handle(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host

        if not (request.content_type or "").startswith("multipart/"):
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        except web.HTTPRequestEntityTooLarge:
            return json_response(
                {"error": "payload_too_large"},
                status=413,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        file_content: bytes | None = None
        session_id: str | None = None
        try:
            while True:
                try:
                    part = await reader.next()
                except web.HTTPRequestEntityTooLarge:
                    return json_response(
                        {"error": "payload_too_large"},
                        status=413,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                if part is None:
                    break
                name = (part.name or "").strip()
                if name == "file" and file_content is None:
                    buf = bytearray()
                    while True:
                        try:
                            chunk = await part.read_chunk(size=64 * 1024)
                        except web.HTTPRequestEntityTooLarge:
                            return json_response(
                                {"error": "payload_too_large"},
                                status=413,
                                origin=origin,
                                allowed_origins=allowed,
                                same_origin_host=same_host,
                            )
                        if not chunk:
                            break
                        buf.extend(chunk)
                        if len(buf) > max_size_bytes:
                            # Drain rest of the part so the connection
                            # doesn't stall on the client's next write,
                            # then bail with 413.
                            return json_response(
                                {"error": "payload_too_large"},
                                status=413,
                                origin=origin,
                                allowed_origins=allowed,
                                same_origin_host=same_host,
                            )
                    file_content = bytes(buf)
                elif name == "session_id" and session_id is None:
                    try:
                        raw = await part.text()
                    except Exception:
                        raw = ""
                    session_id = raw.strip()
        except Exception:
            logger.exception("[WebChatGateway] upload multipart parse failed")
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        if file_content is None or not file_content:
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        if not session_id:
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        if not _SESSION_ID_PATTERN.match(session_id):
            return json_response(
                {"error": "invalid_session_id"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        mime = await detect_image_mime_async(file_content)
        if mime is None or mime not in allowed_mime:
            await deps.audit.write(
                "upload_rejected",
                name=token.name,
                ip=ip,
                detail={
                    "reason": "unsupported_mime",
                    "detected": mime,
                    "size": len(file_content),
                },
            )
            return json_response(
                {"error": "unsupported_mime"},
                status=415,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # token_urlsafe(12) → 16 chars from the URL-safe base64 alphabet.
        # Defensive regex check covers the (impossible) case where a
        # future Python builds emits a different length.
        file_id = secrets.token_urlsafe(12)
        if not _FILE_ID_PATTERN.match(file_id):
            logger.error(
                "[WebChatGateway] generated file_id failed validation: %r",
                file_id,
            )
            return json_response(
                {"error": "internal_error"},
                status=500,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        ext = ext_for_mime(mime) or ALLOWED_MIME_TO_EXT.get(mime, "")
        storage_key = f"{token.name}/{file_id}{ext}"

        # Quota check + reservation are wrapped in a per-token gate so
        # concurrent uploads on the same token can't all pass the same
        # check-then-act and collectively write past the cap. The lock
        # is BLOCKING (uploads queue, they don't 429) and ONLY scopes
        # the DB-side critical section; file_store.save() runs outside.
        #
        # We compute the audit metrics + insert the file row with
        # committed=0 inside the lock — that "reserves" the quota slot
        # by making total_size_for_token return the new total on the
        # next concurrent caller. If the subsequent disk write fails
        # we roll back the row, restoring the quota.
        committed_total = 0
        async with deps.upload_gate.acquire(token.name):
            try:
                committed_total = await deps.storage.total_committed_size_for_token(
                    token.name
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] total_committed_size_for_token failed"
                )
                committed_total = 0
            try:
                stored_total = await deps.storage.total_size_for_token(token.name)
            except Exception:
                logger.exception(
                    "[WebChatGateway] total_size_for_token failed"
                )
                stored_total = committed_total
            if stored_total + len(file_content) > per_token_quota_bytes:
                await deps.audit.write(
                    "upload_rejected",
                    name=token.name,
                    ip=ip,
                    detail={
                        "reason": "storage_quota_exceeded",
                        "committed": committed_total,
                        "uncommitted": max(0, stored_total - committed_total),
                        "size": len(file_content),
                    },
                )
                return json_response(
                    {"error": "storage_quota_exceeded"},
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            now = int(time.time())
            try:
                await deps.storage.insert_file(
                    file_id=file_id,
                    token_name=token.name,
                    session_id=session_id,
                    mime=mime,
                    size_bytes=len(file_content),
                    storage_key=storage_key,
                    now=now,
                )
            except Exception:
                logger.exception("[WebChatGateway] insert_file failed")
                return json_response(
                    {"error": "internal_error"},
                    status=500,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

        # Disk write OUTSIDE the per-token lock — bytes flow doesn't
        # need serialization, and keeping the critical section short
        # means concurrent uploads on the same token only queue on the
        # tiny DB step. If save fails after the row was already inserted,
        # we roll back the row so the quota is restored.
        try:
            await deps.file_store.save(
                storage_key=storage_key, content=file_content, mime=mime
            )
        except Exception:
            logger.exception("[WebChatGateway] file_store.save failed")
            try:
                await deps.storage.delete_files_by_ids([file_id])
            except Exception:
                logger.exception(
                    "[WebChatGateway] insert_file rollback (delete row) failed"
                )
            return json_response(
                {"error": "internal_error"},
                status=500,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        await deps.audit.write(
            "upload_ok",
            name=token.name,
            ip=ip,
            detail={
                "file_id": file_id,
                "size": len(file_content),
                "mime": mime,
                "session_id": session_id,
                # `committed_total` is the pre-upload committed sum (NOT
                # including this file yet, NOT including uncommitted
                # orphans). Operators monitoring abuse can correlate
                # rapid uploads against the slow-rising committed sum:
                # a spike in upload_ok events with flat committed_total
                # indicates the "upload many, attach few" pattern that
                # plan §"Storage quota check timing" calls out as the
                # accepted 1-hour orphan-window spike.
                "committed_total": committed_total,
            },
        )
        # Wire-format `url` is the path under the gateway, not absolute;
        # the frontend resolves it against the page origin. Building it
        # off the request prefix lets a customised `endpoint_prefix`
        # flow through without a separate config plumbing.
        return json_response(
            {
                "file_id": file_id,
                "mime": mime,
                "size": len(file_content),
                "url": deps.files_serve_prefix + file_id,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_host,
        )

    return handle


def make_serve_handler(deps: UploadDeps):
    """GET {prefix}/files/{file_id} — return the stored image bytes.

    Two auth paths, GET-only-read endpoint:

    1. `Authorization: Bearer <token>` / `X-API-Key: <token>` — used by
       JS-driven fetches (admin tools, future XHR consumers).
    2. `Cookie: wcg_file=<token_name>.<exp>.<sig>` — used by `<img src>`
       which cannot set custom headers. The cookie is HMAC-signed
       (see `core/file_cookie.py`), HttpOnly + SameSite=Lax + path-
       scoped to /api/webchat/files. /me issues + refreshes it.

    The previous `?t=<bearer>` query-string fallback has been removed
    — leaking the bearer into browser history / access logs / Referer
    headers was an unacceptable production risk.

    Ownership: regardless of auth path, the bearer/cookie owner's
    token_name MUST match `webchat_files.token_name`. Cross-token
    returns 404 (NOT 403) — no existence-by-timing leak.

    R2 + direct mode → 302 to a short-lived presigned URL.
    Local OR R2-proxy → bytes streamed back with stored MIME and a
    24-hour private cache header.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host

        # Origin / IP-guard gate (runs for BOTH auth paths so cookie-
        # auth requests are still subject to IP brute-force accounting
        # and origin allow-list).
        if not is_origin_allowed(
            origin,
            allowed,
            same_origin_host=same_host,
            allow_missing=deps.allow_missing_origin,
        ):
            return json_response(
                {"error": "forbidden_origin"},
                status=403,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        blocked, retry_after = await deps.ip_guard.is_blocked(ip)
        if blocked:
            return json_response(
                {"error": "ip_blocked", "retry_after": retry_after},
                status=429,
                origin=origin,
                allowed_origins=allowed,
                extra_headers={"Retry-After": str(retry_after)},
                same_origin_host=same_host,
            )

        # Try header-bearer first (lets a curl/JS caller bypass the
        # cookie path), fall back to file-auth cookie.
        token = None
        token_name_via_cookie: str | None = None
        presented = extract_bearer(request)
        cookie_value: str | None = None
        if presented:
            # Header path — full gate_request gives us a uniform
            # GatePass. We use it directly.
            gated = await gate_request(request, deps)
            if isinstance(gated, web.Response):
                return gated
            token = gated.token
        elif deps.file_cookie_secret:
            cookie_value = request.cookies.get(FILE_AUTH_COOKIE_NAME)
            # Verify-with-hash flow: the cookie's signature now folds
            # the current `token_hash` into HMAC input, so admin
            # `regenerate_token` (which rotates the hash but keeps the
            # name) invalidates outstanding cookies immediately. We
            # peek at the token_name from the cookie structure
            # (rsplit, no HMAC yet), look up the row to get the
            # CURRENT hash, then call verify with that hash — sig
            # mismatch on stale cookies → None → 401.
            if cookie_value:
                peek_parts = cookie_value.rsplit(".", 2)
                peek_name = (
                    peek_parts[0] if len(peek_parts) == 3 else ""
                )
                token_row = (
                    await deps.storage.get_token_by_name(peek_name)
                    if peek_name
                    else None
                )
                if token_row is None:
                    pass  # fall through to 401
                else:
                    verified = verify_file_cookie(
                        deps.file_cookie_secret,
                        cookie_value,
                        current_token_hash=token_row.token_hash,
                    )
                    if verified is None:
                        pass  # fall through to 401
                    else:
                        token_name_via_cookie, _exp = verified
                        # Server-side logout invalidation check: a cookie
                        # whose `exp_ts` falls at or below the recorded
                        # logout threshold for this token was issued
                        # before the user clicked "logout". Reject it
                        # uniformly with the malformed/expired/wrong-sig
                        # case so an attacker can't distinguish "your
                        # cookie was killed by logout" from "your sig is
                        # bad" (timing-equivalent 401).
                        if (
                            deps.cookie_logout_tracker is not None
                            and deps.cookie_logout_tracker.is_invalidated(
                                token_row.name, exp_ts=_exp
                            )
                        ):
                            pass  # fall through to 401
                        else:
                            token = token_row
                            # Cookie attests the token_name but does NOT
                            # cover revocation / expiry — recheck against
                            # the live row so a revoked bearer kills file
                            # access immediately even before its cookie
                            # naturally expires.
                            now_ts = int(time.time())
                            if (
                                token.revoked_at is not None
                                or (
                                    token.expires_at is not None
                                    and token.expires_at <= now_ts
                                )
                            ):
                                token = None
        if token is None:
            # Brute-force accounting only fires when NEITHER credential
            # type was presented — bearer absent AND no file-auth cookie
            # on the request. A cookie that was present but failed to
            # verify (bad sig, expired, logout-invalidated, sig over a
            # rotated token_hash, or covering a revoked/expired token)
            # is NOT counted: those are the user's own cookies racing
            # against admin-side state changes (e.g. regenerate_token
            # rotates the hash → every open tab fails HMAC at once →
            # IP self-lockout). The 96-bit file_id space already makes
            # serve enumeration infeasible without a cookie; the
            # brute-force deterrent is only meaningful against
            # unauthenticated probes, which is the case we count here.
            no_credential_presented = (not presented) and (not cookie_value)
            if no_credential_presented:
                try:
                    await deps.ip_guard.record_failure(ip)
                except Exception:
                    logger.exception(
                        "[WebChatGateway] /files ip_guard.record_failure failed"
                    )
            try:
                await deps.audit.write(
                    "auth_fail",
                    ip=ip,
                    detail={
                        "reason": "no_token" if no_credential_presented else "bad_cookie",
                        "endpoint": "files",
                    },
                )
            except Exception:
                pass
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        await deps.ip_guard.reset(ip)

        file_id = (request.match_info.get("file_id") or "").strip()
        if not file_id or not _FILE_ID_PATTERN.match(file_id):
            return json_response(
                {"error": "invalid_file_id"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        try:
            row = await deps.storage.get_file(file_id)
        except Exception:
            logger.exception("[WebChatGateway] get_file failed")
            return json_response(
                {"error": "internal_error"},
                status=500,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # Uniform 404 on missing + wrong-owner. No 403 branch — that
        # would let an attacker distinguish "exists, not yours" from
        # "doesn't exist".
        if row is None or row.token_name != token.name:
            # Best-effort forensic audit: log cross-token probes (row
            # exists but belongs to a different token) distinctly from
            # genuine misses. The client still sees a uniform 404 (no
            # timing/content leak), but operators get a signal for
            # probing patterns. Bare miss is silent to avoid flooding
            # the audit log with normal 404s from stale URLs.
            if row is not None and row.token_name != token.name:
                try:
                    await deps.audit.write(
                        "file_serve_blocked",
                        name=token.name,
                        ip=ip,
                        detail={"file_id": file_id, "reason": "cross_token"},
                    )
                except Exception:
                    pass
            return json_response(
                {"error": "not_found"},
                status=404,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        if (
            deps.storage_driver == "r2"
            and deps.r2_serving_mode == "direct"
        ):
            try:
                signed = await deps.file_store.signed_url(
                    storage_key=row.storage_key,
                    ttl_seconds=deps.r2_direct_link_ttl_seconds,
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] file_store.signed_url failed; falling back to proxy"
                )
                signed = None
            if signed:
                return web.Response(
                    status=302,
                    headers={
                        "Location": signed,
                        "Cache-Control": "private, max-age=60",
                    },
                )
            # signed_url returned None — fall through to proxy read so
            # the client still gets a response (matching local-store
            # behaviour). The PLAN allows this graceful degradation.

        try:
            payload = await deps.file_store.read(storage_key=row.storage_key)
        except Exception:
            logger.exception("[WebChatGateway] file_store.read failed")
            return json_response(
                {"error": "internal_error"},
                status=500,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        if payload is None:
            # DB row exists but bytes are gone — likely a partially-
            # failed rollback in the past. Return 404 so the client
            # falls through to its placeholder render.
            return json_response(
                {"error": "not_found"},
                status=404,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        return web.Response(
            body=payload,
            status=200,
            headers={
                **cors,
                "Content-Type": row.mime,
                "Cache-Control": "private, max-age=86400",
                # Defense in depth: lock the browser to the stored MIME
                # so a future relax of the allowed_mime whitelist (e.g.
                # adding SVG / TIFF) doesn't accidentally enable stored-
                # XSS via content sniffing. `inline` keeps the natural
                # `<img>` render path; no filename is leaked beyond the
                # opaque file_id which is already the URL path segment.
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'inline; filename="{file_id}"',
                # Referrer-Policy on the image response itself prevents
                # leakage of the (random but session-tied) file_id URL
                # via outbound `<a href>` clicks on the same page.
                "Referrer-Policy": "no-referrer",
            },
        )

    return handle


def make_files_preflight(deps: UploadDeps):
    """OPTIONS preflight for both /upload and /files/{file_id}.

    Mirrors `make_preflight_handler` in handlers/chat.py — same allow-list
    semantics, same CORS headers, just lifted onto UploadDeps so the
    upload routes don't have to pull ChatDeps in.
    """

    async def handle(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=deps.trust_referer_as_origin
            ),
            allowed=deps.allowed_origins,
            same_origin_host=request.host,
        )

    return handle


__all__ = [
    "UploadDeps",
    "make_upload_handler",
    "make_serve_handler",
    "make_files_preflight",
]
