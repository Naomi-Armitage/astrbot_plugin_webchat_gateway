"""Common HTTP utilities: CORS, JSON envelope, real-IP extraction, Origin allow-list."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from aiohttp import web


def extract_origin(request: web.Request) -> str | None:
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin
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
        first = xff.split(",")[0].strip()
        if first:
            return first
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
