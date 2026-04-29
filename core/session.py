"""HMAC-signed admin session tokens.

Sessions are derived from the master_admin_key, so rotating the key
invalidates every outstanding session — that's intentional.

Token wire format: ``b64u(payload).b64u(sig)`` where ``payload`` is JSON
``{"iat": int, "exp": int}`` and ``sig = HMAC-SHA256(master_key, b64u(payload))``.

No third-party deps; only stdlib hmac/hashlib/json/base64/time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

DEFAULT_TTL_SECONDS = 12 * 3600
COOKIE_NAME = "wcg_session"


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: str, payload_b64: str) -> str:
    sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64u_encode(sig)


def issue_session(
    master_key: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, int]:
    """Mint a new session token. Returns (token, expires_at_epoch)."""
    if not master_key:
        raise ValueError("master_key empty")
    iat = int(now if now is not None else time.time())
    exp = iat + max(60, int(ttl_seconds))
    payload_b64 = _b64u_encode(
        json.dumps({"iat": iat, "exp": exp}, separators=(",", ":")).encode("utf-8")
    )
    sig_b64 = _sign(master_key, payload_b64)
    return f"{payload_b64}.{sig_b64}", exp


def verify_session(
    master_key: str,
    token: str,
    *,
    now: int | None = None,
) -> bool:
    """Constant-time verify: signature match and not expired."""
    if not master_key or not token:
        return False
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return False
    expected = _sign(master_key, payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        return False
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    current = int(now if now is not None else time.time())
    return current < exp
