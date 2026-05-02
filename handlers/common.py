"""Common HTTP utilities: CORS, JSON envelope, real-IP extraction, Origin allow-list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from aiohttp import web

from ..core.audit import AuditLogger
from ..core.auth import extract_bearer, hash_token
from ..core.ip_guard import IpGuard
from ..storage.base import AbstractStorage


def extract_origin(
    request: web.Request,
    *,
    trust_referer_as_origin: bool = False,
) -> str | None:
    """Return the request Origin, optionally falling back to Referer.

    The Referer fallback is unsafe by default: a browser that strips
    Referer (privacy mode, no-referrer policy) and omits Origin would be
    indistinguishable from a server-side curl call, weakening the Origin
    allow-list as a CSRF mitigation. Only enable the fallback when the
    deployment knowingly accepts that tradeoff.
    """
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin
    if not trust_referer_as_origin:
        return None
    referer = (request.headers.get("Referer") or "").strip()
    if not referer:
        return None
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def is_origin_allowed(
    origin: str | None,
    allowed: set[str],
    *,
    same_origin_host: str | None = None,
    allow_missing: bool = True,
) -> bool:
    if "*" in allowed:
        return True
    if origin is None:
        # Non-browser clients (curl, server-side) typically omit Origin.
        # State-changing endpoints opt out via allow_missing=False so the
        # Origin allow-list is not silently neutered for non-browser callers.
        return allow_missing
    if origin in allowed:
        return True
    if same_origin_host:
        # Auto-allow same-origin requests so the bundled UI works without
        # forcing operators to add the gateway's own URL to allowed_origins.
        # request.host already includes :port, and a URL Origin's netloc
        # includes :port too, so an exact equality check is correct.
        parsed = urlparse(origin)
        if parsed.netloc and parsed.netloc == same_origin_host:
            return True
    return False


def build_cors_headers(
    origin: str | None,
    allowed: set[str],
    *,
    same_origin_host: str | None = None,
) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Max-Age": "600",
    }
    if "*" in allowed:
        # Wildcard + credentials is invalid per spec; if the operator
        # opted into '*', echo the request's Origin so credentials still
        # work for the bundled UI. Falling back to '*' keeps non-browser
        # callers happy.
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        else:
            headers["Access-Control-Allow-Origin"] = "*"
            headers.pop("Access-Control-Allow-Credentials", None)
        return headers
    if origin and (
        origin in allowed
        or (
            same_origin_host
            and urlparse(origin).netloc == same_origin_host
        )
    ):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


def client_ip(request: web.Request, *, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        xff = request.headers.get("X-Forwarded-For", "")
        # Take the LAST entry: the trusted proxy appends the real client IP
        # at the end of the list. The FIRST entry is whatever the client
        # supplied in their request and is attacker-controlled — taking it
        # would let any caller spoof their IP and bypass IP brute-force /
        # poison the audit log. (This assumes a single-hop trusted proxy;
        # multi-hop deployments must overwrite XFF at the edge instead.)
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip
    return request.remote or "unknown"


def json_response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    origin: str | None = None,
    allowed_origins: set[str] | None = None,
    extra_headers: dict[str, str] | None = None,
    same_origin_host: str | None = None,
) -> web.Response:
    headers = build_cors_headers(
        origin, allowed_origins or {"*"}, same_origin_host=same_origin_host
    )
    if extra_headers:
        headers.update(extra_headers)
    return web.json_response(payload, status=status, headers=headers)


def preflight_response(
    *,
    origin: str | None,
    allowed: set[str],
    same_origin_host: str | None = None,
) -> web.Response:
    if not is_origin_allowed(origin, allowed, same_origin_host=same_origin_host):
        return json_response(
            {"error": "forbidden_origin"},
            status=403,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_origin_host,
        )
    return web.Response(
        status=204,
        headers=build_cors_headers(
            origin, allowed, same_origin_host=same_origin_host
        ),
    )


class _GateDeps(Protocol):
    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    allowed_origins: set[str]
    trust_forwarded_for: bool
    trust_referer_as_origin: bool
    allow_missing_origin: bool


@dataclass
class GatePass:
    """Result of a successful pre-LLM gate (origin + IP + auth)."""

    token: Any  # storage Token row
    ip: str
    origin: str | None
    allowed: set[str]
    same_host: str


async def gate_request(
    request: web.Request, deps: _GateDeps
) -> GatePass | web.Response:
    """Run origin → IP brute-force → bearer auth.

    Returns a GatePass on success, or an error Response (already serialized
    with the right CORS headers) that the caller should return verbatim.
    Audit + IP-guard side-effects mirror handlers/chat.py exactly so the
    /title path can't be used to enumerate tokens any cheaper than /chat.
    """
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
        await deps.audit.write("auth_fail", ip=ip, detail={"reason": "no_token"})
        return json_response(
            {"error": "unauthorized"},
            status=401,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_host,
        )
    token = await deps.storage.get_token_by_hash(hash_token(presented))
    if token is None or token.revoked_at is not None:
        if token is None:
            await deps.ip_guard.record_failure(ip)
        await deps.audit.write(
            "auth_fail",
            ip=ip,
            detail={"reason": "revoked" if token else "invalid"},
        )
        return json_response(
            {"error": "unauthorized"},
            status=401,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_host,
        )
    await deps.ip_guard.reset(ip)
    return GatePass(
        token=token, ip=ip, origin=origin, allowed=allowed, same_host=same_host
    )
