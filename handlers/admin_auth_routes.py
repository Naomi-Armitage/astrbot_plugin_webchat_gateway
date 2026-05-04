"""Admin auth routes: login (cookie issue), logout, me (probe).

The session cookie is HttpOnly, SameSite=Lax, scoped to the admin path
prefix. ``Secure`` is set only when the request reached us over HTTPS;
otherwise the browser would refuse to send the cookie back over plain
HTTP and the panel would loop on login.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.auth import (
    constant_time_eq,
    extract_bearer,
    extract_session_cookie,
    has_admin_credentials,
    is_master_admin,
)
from ..core.ip_guard import IpGuard
from ..core.session import COOKIE_NAME, DEFAULT_TTL_SECONDS, issue_session
from .admin_tokens import ServiceError
from .common import (
    client_ip,
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


@dataclass
class AuthRouteDeps:
    audit: AuditLogger
    ip_guard: IpGuard
    allowed_origins: set[str]
    master_admin_key: str
    trust_forwarded_for: bool
    trust_referer_as_origin: bool
    cookie_path: str
    allow_missing_origin: bool = False
    session_ttl_seconds: int = DEFAULT_TTL_SECONDS


def _is_secure(request: web.Request, *, trust_forwarded_for: bool) -> bool:
    if trust_forwarded_for:
        proto = (request.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if proto:
            return proto == "https"
    return request.scheme == "https"


def make_auth_handlers(deps: AuthRouteDeps):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    def _origin(request: web.Request) -> str | None:
        return extract_origin(request, trust_referer_as_origin=trust_referer)

    def _err(request: web.Request, origin, exc: ServiceError) -> web.Response:
        extra = None
        if exc.code == "ip_blocked" and str(exc):
            extra = {"Retry-After": str(exc)}
        return json_response(
            {"error": exc.code, "detail": str(exc) if str(exc) != exc.code else ""},
            status=exc.status,
            origin=origin,
            allowed_origins=allowed,
            extra_headers=extra,
            same_origin_host=request.host,
        )

    async def login(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            if not is_origin_allowed(
                origin,
                allowed,
                same_origin_host=request.host,
                allow_missing=deps.allow_missing_origin,
            ):
                raise ServiceError("forbidden_origin", status=403)
            blocked, retry_after = await deps.ip_guard.is_blocked(ip)
            if blocked:
                await deps.audit.write(
                    "admin_auth_fail",
                    ip=ip,
                    detail={"reason": "ip_blocked", "retry_after": retry_after},
                )
                raise ServiceError(
                    "ip_blocked", status=429, message=str(retry_after)
                )
            if not deps.master_admin_key:
                await deps.audit.write(
                    "admin_auth_fail", ip=ip, detail={"reason": "admin_disabled"}
                )
                raise ServiceError("admin_disabled", status=403)

            try:
                body = await request.json()
            except (ValueError, web.HTTPRequestEntityTooLarge):
                body = {}
            if not isinstance(body, dict):
                body = {}
            presented = str(body.get("key") or "").strip()
            if not presented:
                # Allow header-style login too (curl convenience).
                presented = extract_bearer(request)

            if not presented:
                await deps.ip_guard.record_failure(ip)
                await deps.audit.write(
                    "admin_login_fail", ip=ip, detail={"reason": "no_key"}
                )
                raise ServiceError("unauthorized", status=401)

            # is_master_admin reads the bearer header; do an explicit
            # constant-time compare against the body field instead so we
            # accept either entry point uniformly.
            if not constant_time_eq(presented, deps.master_admin_key):
                await deps.ip_guard.record_failure(ip)
                await deps.audit.write(
                    "admin_login_fail", ip=ip, detail={"reason": "invalid_key"}
                )
                raise ServiceError("unauthorized", status=401)

            await deps.ip_guard.reset(ip)
            token, exp = issue_session(
                deps.master_admin_key, ttl_seconds=deps.session_ttl_seconds
            )
            await deps.audit.write(
                "admin_login", ip=ip, detail={"expires_at": exp}
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin login failed")
            return _err(request, origin, ServiceError("internal_error", status=500))

        resp = json_response(
            {"ok": True, "expires_at": exp},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )
        resp.set_cookie(
            COOKIE_NAME,
            token,
            max_age=deps.session_ttl_seconds,
            path=deps.cookie_path,
            httponly=True,
            secure=_is_secure(
                request, trust_forwarded_for=deps.trust_forwarded_for
            ),
            samesite="Lax",
        )
        return resp

    async def logout(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        if not is_origin_allowed(
            origin,
            allowed,
            same_origin_host=request.host,
            allow_missing=deps.allow_missing_origin,
        ):
            return _err(request, origin, ServiceError("forbidden_origin", status=403))
        had_session = bool(extract_session_cookie(request))
        # Logout is intentionally idempotent: clearing the cookie is safe
        # whether or not the caller had one.
        resp = json_response(
            {"ok": True},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )
        resp.del_cookie(COOKIE_NAME, path=deps.cookie_path)
        if had_session:
            try:
                await deps.audit.write("admin_logout", ip=ip, detail={})
            except Exception:
                logger.exception("[WebChatGateway] admin logout audit failed")
        return resp

    async def me(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        if not is_origin_allowed(
            origin, allowed, same_origin_host=request.host
        ):
            return _err(request, origin, ServiceError("forbidden_origin", status=403))
        if not deps.master_admin_key:
            return _err(request, origin, ServiceError("admin_disabled", status=403))
        # Same IP-guard pipeline as /login: an attacker hitting /me with a
        # parade of bearer guesses would otherwise have an unrate-limited
        # oracle for the master_admin_key. A bare credential probe (no
        # bearer / cookie at all) is treated as a no-op so the admin panel
        # can poll harmlessly on first paint.
        bearer_present = bool(extract_bearer(request))
        cookie_present = bool(extract_session_cookie(request))
        if bearer_present or cookie_present:
            blocked, retry_after = await deps.ip_guard.is_blocked(ip)
            if blocked:
                return _err(
                    request,
                    origin,
                    ServiceError("ip_blocked", status=429, message=str(retry_after)),
                )
        if has_admin_credentials(request, deps.master_admin_key):
            await deps.ip_guard.reset(ip)
            kind = "bearer" if is_master_admin(request, deps.master_admin_key) else "session"
            return json_response(
                {"ok": True, "kind": kind},
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=request.host,
            )
        if bearer_present or cookie_present:
            await deps.ip_guard.record_failure(ip)
            await deps.audit.write(
                "admin_auth_fail",
                ip=ip,
                detail={
                    "reason": "invalid_key" if bearer_present and not cookie_present
                    else "invalid_session" if cookie_present and not bearer_present
                    else "invalid_credentials",
                    "endpoint": "me",
                },
            )
        return _err(request, origin, ServiceError("unauthorized", status=401))

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=_origin(request),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {
        "login": login,
        "logout": logout,
        "me": me,
        "preflight": preflight,
    }
