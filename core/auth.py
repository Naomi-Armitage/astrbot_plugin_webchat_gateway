"""Token generation, hashing, header extraction, constant-time comparison."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from aiohttp import web

from .session import COOKIE_NAME, verify_session


def generate_token() -> str:
    """Return a 43-char URL-safe token (32 bytes of entropy)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def extract_bearer(request: web.Request) -> str:
    """Read bearer token from `X-API-Key` or `Authorization: Bearer ...`."""
    x_api_key = (request.headers.get("X-API-Key") or "").strip()
    if x_api_key:
        return x_api_key
    authorization = (request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def is_master_admin(request: web.Request, master_key: str) -> bool:
    if not master_key:
        return False
    presented = extract_bearer(request)
    if not presented:
        return False
    return constant_time_eq(presented, master_key)


def extract_session_cookie(request: web.Request) -> str:
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def has_admin_credentials(request: web.Request, master_key: str) -> bool:
    """True if EITHER bearer matches master_key OR a valid session cookie is present."""
    if not master_key:
        return False
    if is_master_admin(request, master_key):
        return True
    cookie = extract_session_cookie(request)
    return bool(cookie) and verify_session(master_key, cookie)


def admin_credential_kind(request: web.Request) -> str:
    """Tag for audit logs: 'bearer', 'session', or 'none'.

    Does NOT validate — purely for logging which channel the caller used,
    so audit trails can distinguish UI sessions from script automation.
    """
    if extract_bearer(request):
        return "bearer"
    if extract_session_cookie(request):
        return "session"
    return "none"
