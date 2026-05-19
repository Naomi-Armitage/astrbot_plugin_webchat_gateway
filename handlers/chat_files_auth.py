"""GET /me + POST /files/logout — file-auth cookie management.

Split from handlers/chat.py. /me probes the bearer + issues the
file-auth cookie used by the /files serve endpoint; /logout clears
that cookie AND records the invalidation so any concurrently issued
cookies are server-side-rejected for the cookie's TTL window.
"""

from __future__ import annotations

import time
from datetime import date

from aiohttp import web

from astrbot.api import logger

from ..core.auth import extract_bearer, hash_token
from ..core.file_cookie import (
    FILE_AUTH_COOKIE_NAME,
    build_clear_cookie_value,
    build_set_cookie_value,
    verify as verify_file_cookie,
)
from .chat_common import ChatDeps, _is_expired
from .common import (
    build_cors_headers,
    client_ip,
    extract_origin,
    is_origin_allowed,
    json_response,
)


def make_me_handler(deps: ChatDeps):
    """GET /me — token-authed quota probe.

    Same Origin / IP-guard / bearer auth as the chat handler so an
    attacker can't use it to enumerate tokens any more cheaply than
    /chat itself. Skips the per-token concurrency lock and audit
    logging because legitimate clients call this on every chat-page
    load and refresh; auditing it would flood the table.
    """

    async def handle(request: web.Request) -> web.Response:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host

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

        presented = extract_bearer(request)
        if not presented:
            await deps.ip_guard.record_failure(ip)
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        token = await deps.storage.get_token_by_hash(hash_token(presented))
        now_ts = int(time.time())
        if token is None or token.revoked_at is not None or _is_expired(token, now_ts):
            if token is None:
                await deps.ip_guard.record_failure(ip)
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        await deps.ip_guard.reset(ip)

        today = date.today()
        today_count = await deps.storage.get_today_usage(token.name, day=today)
        remaining = max(0, token.daily_quota - today_count)
        extra: dict[str, str] = {"Cache-Control": "no-store"}
        # Issue / refresh the file-auth cookie. `<img src>` cannot set
        # Authorization, so file-serve relies on this cookie instead
        # of leaking the bearer into URLs (which would land in browser
        # history + access logs + monitoring). Secret rotates on plugin
        # restart, invalidating old cookies. SameSite=Lax + HttpOnly +
        # Path-scoped to /api/webchat/files.
        if deps.file_cookie_secret:
            scheme = (
                request.headers.get("X-Forwarded-Proto")
                or request.scheme
                or "http"
            ).lower()
            secure = scheme == "https"
            _, set_cookie_value = build_set_cookie_value(
                deps.file_cookie_secret,
                token_name=token.name,
                token_hash=token.token_hash,
                ttl_seconds=deps.file_cookie_ttl_seconds,
                secure=secure,
                cookie_path=deps.file_cookie_path,
            )
            extra["Set-Cookie"] = set_cookie_value
        return json_response(
            {
                "name": token.name,
                "remaining": remaining,
                "daily_quota": token.daily_quota,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_host,
            extra_headers=extra,
        )

    return handle


def make_logout_handler(deps: ChatDeps):
    """POST {prefix}/files/logout — logout + server-side cookie invalidation.

    Wire contract:
      * Path is intentionally under `/files/` so the browser auto-attaches
        the `wcg_file` cookie on `navigator.sendBeacon` (the page-unload-
        safe channel the frontend prefers). Without that the handler
        could not identify which token's cookies to invalidate when the
        bearer header is missing (sendBeacon can't set custom headers).
      * Bearer header is ALSO accepted as a fallback for CLI / non-
        browser callers and for browsers where sendBeacon is unavailable
        and the keepalive-fetch path is used instead.
      * Auth-less requests get 401 — clearing a cookie still happens (so
        a returning browser session gets a fresh /me probe) but no
        server-side invalidation can occur without a token name.

    Server-side invalidation: records `token_name → now` into the
    process-wide `CookieLogoutTracker`. The serve endpoint
    (handlers/files.py) consults this tracker AFTER the HMAC verify
    succeeds: any cookie whose `exp_ts <= recorded_logout_time +
    ttl_seconds` is rejected. New cookies issued post-logout (by the
    next `/me` call) have a fresh exp_ts above the threshold and
    verify normally. Trade-off documented on `CookieLogoutTracker`.

    Returns 204 with a `Set-Cookie: wcg_file=; Max-Age=0` directive in
    every path (auth ok, auth fail, no auth) so the browser-side cookie
    is always cleared even when server-side state can't be touched.
    """

    async def handle(request: web.Request) -> web.Response:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host
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
        # IP-guard BEFORE peeking into the cookie. The cookie's
        # `peek_name` (pre-HMAC) feeds a DB lookup that's otherwise an
        # unauthenticated timing oracle for token-name existence —
        # rate-limiting via the shared brute-force tracker forces an
        # attacker through the same ip-block cadence as a /chat
        # bearer-guess campaign.
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        blocked, retry_after = await deps.ip_guard.is_blocked(ip)
        if blocked:
            # 429 path intentionally does NOT include the
            # `Set-Cookie: Max-Age=0` directive: a probing attacker
            # whose IP just got blocked shouldn't get a side-channel
            # confirming that the request reached the logout handler
            # (vs. being dropped earlier). Legitimate users whose
            # IP is shared with a probing attacker will retry from
            # a different IP or wait out the block; their browser-
            # side cookie clear can happen via the next /me probe.
            headers = build_cors_headers(
                origin, allowed, same_origin_host=same_host
            )
            headers["Retry-After"] = str(retry_after)
            return web.Response(status=429, headers=headers)
        # Identify the token: prefer bearer (CLI / fetch keepalive),
        # fall back to cookie (sendBeacon). The cookie's HMAC is
        # verified against the CURRENT token_hash so a stolen cookie
        # for a regenerated token still gets rejected at the verify
        # step — we ignore is_invalidated here since we're about to
        # add to the invalidation set, not enforce it.
        token_name: str | None = None
        presented = extract_bearer(request)
        if presented:
            tok = await deps.storage.get_token_by_hash(hash_token(presented))
            if tok is not None:
                token_name = tok.name
        if token_name is None and deps.file_cookie_secret:
            cookie_value = request.cookies.get(FILE_AUTH_COOKIE_NAME)
            if cookie_value:
                peek_parts = cookie_value.rsplit(".", 2)
                peek_name = (
                    peek_parts[0] if len(peek_parts) == 3 else ""
                )
                row = (
                    await deps.storage.get_token_by_name(peek_name)
                    if peek_name
                    else None
                )
                if row is not None:
                    verified = verify_file_cookie(
                        deps.file_cookie_secret,
                        cookie_value,
                        current_token_hash=row.token_hash,
                    )
                    if verified is not None:
                        token_name = row.name
        # Always emit a cookie-clear directive; record server-side
        # invalidation only when we identified the token.
        headers = build_cors_headers(
            origin, allowed, same_origin_host=same_host
        )
        headers["Set-Cookie"] = build_clear_cookie_value(
            cookie_path=deps.file_cookie_path
        )
        headers["Cache-Control"] = "no-store"
        if token_name is None:
            # No auth — still 204 (clears browser cookie) but log that
            # server-side invalidation didn't fire so operators can
            # spot misconfigured frontends. Also bump the IP failure
            # counter so a logout-endpoint enumeration campaign (no
            # bearer + cookies with forged peek-names) burns through
            # the brute-force budget the same way a /chat bearer-
            # guess does — kills the timing oracle that would
            # otherwise be readable across the cookie peek-name DB
            # lookup.
            try:
                await deps.ip_guard.record_failure(ip)
            except Exception:
                logger.exception(
                    "[WebChatGateway] logout ip_guard.record_failure failed"
                )
            try:
                await deps.audit.write(
                    "logout_no_auth",
                    ip=ip,
                    detail={},
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] logout_no_auth audit failed"
                )
            return web.Response(status=204, headers=headers)
        await deps.ip_guard.reset(ip)
        if deps.cookie_logout_tracker is not None:
            try:
                deps.cookie_logout_tracker.record(token_name)
            except Exception:
                logger.exception(
                    "[WebChatGateway] cookie_logout_tracker.record failed"
                )
        try:
            await deps.audit.write(
                "logout",
                name=token_name,
                ip=client_ip(request, trust_forwarded_for=deps.trust_forwarded_for),
                detail={},
            )
        except Exception:
            pass
        return web.Response(status=204, headers=headers)

    return handle

