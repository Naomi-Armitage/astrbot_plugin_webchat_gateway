"""Common HTTP utilities: CORS, JSON envelope, real-IP extraction, Origin allow-list."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from aiohttp import web


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


def is_origin_allowed(origin: str | None, allowed: set[str]) -> bool:
    if "*" in allowed:
        return True
    if origin is None:
        # Non-browser clients (curl, server-side) typically omit Origin.
        return True
    return origin in allowed


def build_cors_headers(origin: str | None, allowed: set[str]) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key",
        "Access-Control-Max-Age": "600",
    }
    if "*" in allowed:
        headers["Access-Control-Allow-Origin"] = "*"
        return headers
    if origin and origin in allowed:
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
) -> web.Response:
    headers = build_cors_headers(origin, allowed_origins or {"*"})
    if extra_headers:
        headers.update(extra_headers)
    return web.json_response(payload, status=status, headers=headers)


def preflight_response(
    *, origin: str | None, allowed: set[str]
) -> web.Response:
    if not is_origin_allowed(origin, allowed):
        return json_response(
            {"error": "forbidden_origin"},
            status=403,
            origin=origin,
            allowed_origins=allowed,
        )
    return web.Response(status=204, headers=build_cors_headers(origin, allowed))
