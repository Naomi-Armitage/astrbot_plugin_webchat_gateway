"""HMAC-signed cookie auth for the /files/{id} serve endpoint.

The chat client renders attachments as `<img src="/api/webchat/files/{id}">`.
HTML images can't set `Authorization` headers, so the serve endpoint
needs an out-of-band auth channel. The naive approach — `?t=<bearer>` in
the URL — leaks the bearer into browser history, server access logs,
HTTP proxies, monitoring agents, and (without strict referrer policy)
Referer headers on outbound links. Not acceptable in production.

This module issues a small HMAC-signed cookie that the serve endpoint
verifies instead of (or in addition to) the bearer header. The cookie
carries:

    - `token_name`: the gateway-internal token identifier (NOT the
      bearer secret). Lookup-only — the serve endpoint resolves it via
      `storage.get_token_by_name` and re-checks `revoked_at` /
      `expires_at` so a revoked bearer kills file access immediately.
    - `exp`: absolute Unix timestamp at which the cookie auto-expires.
      The HMAC binds it so a client can't extend the lifetime.
    - `sig`: HMAC-SHA256 of `{token_name}:{exp}` keyed by a per-plugin-
      instance secret. Verifies authenticity + integrity.

The secret is generated fresh on plugin startup (`secrets.token_bytes`)
and held in process memory. AstrBot reload / restart rotates the secret
and invalidates all in-flight cookies — clients silently re-issue on the
next /me probe, so this is operationally transparent.

Cookie attributes set on emission (Set-Cookie):
    - `HttpOnly`: JS can't read the value (mitigates XSS exfil)
    - `SameSite=Lax`: blocks cross-site cookie sends except for top-
      level GETs; `<img>` requests from a 3rd-party site won't carry it
    - `Secure`: required when the request was over HTTPS
    - `Path=/api/webchat/files`: only sent on file-serve requests, not
      bleed-back into /chat or /admin
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import secrets
import time

# Cookie name. Path-scoped to /api/webchat/files in the Set-Cookie
# attributes — see `build_set_cookie` below.
FILE_AUTH_COOKIE_NAME = "wcg_file"

# Default TTL on the cookie. 24h covers the typical "user comes back
# tomorrow" use case without holding the auth window open forever. The
# bearer-revocation check on every serve request means a stolen cookie
# only works as long as the bearer is also still valid.
DEFAULT_TTL_SECONDS = 24 * 3600


def make_secret() -> bytes:
    """Generate a fresh signing secret for this plugin instance."""
    return secrets.token_bytes(32)


def sign(
    secret: bytes,
    *,
    token_name: str,
    token_hash: str,
    exp_ts: int,
) -> str:
    """Build the cookie value: `{token_name}.{exp}.{sig}` where
    `sig = HMAC(secret, f"{token_name}:{token_hash}:{exp_ts}")` URL-
    safe base64.

    `token_hash` is the current `webchat_tokens.token_hash` for the
    token. Folding it into the signature means that any operation
    that rotates the hash (admin `regenerate_token`) invalidates
    every outstanding cookie for that token in one step — without
    rotating the per-plugin HMAC secret. After regenerate, the next
    /me call on the new bearer issues a fresh cookie bound to the
    new hash; the old cookie's sig no longer verifies against the
    current hash, so the serve endpoint rejects it.

    `token_hash` is NOT included in the cookie payload itself — the
    cookie still reads `{token_name}.{exp}.{sig}`. The verify path
    looks the current hash up from storage and recomputes the
    expected sig server-side. This keeps the cookie size small AND
    avoids leaking the (already-hashed) bearer identifier to the
    client.
    """
    payload = f"{token_name}:{token_hash}:{exp_ts}".encode("utf-8")
    digest = hmac.new(secret, payload, hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    # `.` is a safe separator: it's allowed in cookie values per RFC
    # 6265 and the cookie-attribute syntax. Token names may also
    # contain `.` so we rsplit on it at verify time.
    return f"{token_name}.{exp_ts}.{sig}"


def verify(
    secret: bytes,
    cookie_value: str,
    *,
    current_token_hash: str,
) -> tuple[str, int] | None:
    """Decode + verify a cookie value. Returns (token_name, exp_ts) on
    success, or None if the cookie is malformed, the signature doesn't
    match (which now includes the token-rotated case), or the cookie
    is past its expiry. All failure modes collapse to None.

    Caller MUST look up the current `webchat_tokens.token_hash` (e.g.
    via `storage.get_token_by_name(token_name).token_hash`) and pass it
    in. After `regenerate_token` rotates the hash, the old cookie's sig
    won't match the recomputed `HMAC(secret, name:NEW_HASH:exp)`, and
    this returns None — exactly the desired "old cookies invalidated"
    behaviour.

    Token names are allowed to contain `.` (admin charset is
    `[A-Za-z0-9_.\\-]`), so we rsplit instead of plain split.
    """
    if not cookie_value or not isinstance(cookie_value, str):
        return None
    parts = cookie_value.rsplit(".", 2)
    if len(parts) != 3:
        return None
    token_name, exp_str, sig_b64 = parts
    if not token_name or not exp_str or not sig_b64:
        return None
    try:
        exp_ts = int(exp_str)
    except ValueError:
        return None
    if exp_ts <= int(time.time()):
        return None
    payload = f"{token_name}:{current_token_hash}:{exp_ts}".encode("utf-8")
    expected_digest = hmac.new(secret, payload, hashlib.sha256).digest()
    try:
        provided_digest = base64.urlsafe_b64decode(sig_b64 + "==")
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected_digest, provided_digest):
        return None
    return token_name, exp_ts


def build_set_cookie_value(
    secret: bytes,
    *,
    token_name: str,
    token_hash: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    secure: bool = True,
    cookie_path: str = "/api/webchat/files",
) -> tuple[str, str]:
    """Return `(cookie_name, set_cookie_directive_value)` ready for
    `Response.headers["Set-Cookie"]` (or `.add` for multi-cookie).

    `token_hash` is folded into the signature so admin
    `regenerate_token` (which rotates the hash but keeps the name)
    invalidates outstanding cookies immediately. See `sign()` for the
    full rationale.

    Attributes (`HttpOnly`, `SameSite=Lax`, optionally `Secure`, the
    given `Path`, and `Max-Age`) are set inline. `Lax` instead of
    `Strict` because top-level GETs into the chat page need to carry
    it; Lax is the standard chat-app posture.
    """
    exp_ts = int(time.time()) + max(60, int(ttl_seconds))
    value = sign(
        secret,
        token_name=token_name,
        token_hash=token_hash,
        exp_ts=exp_ts,
    )
    attrs = [
        f"{FILE_AUTH_COOKIE_NAME}={value}",
        f"Path={cookie_path}",
        f"Max-Age={max(60, int(ttl_seconds))}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        attrs.append("Secure")
    return FILE_AUTH_COOKIE_NAME, "; ".join(attrs)


def build_clear_cookie_value(
    cookie_path: str = "/api/webchat/files",
) -> str:
    """Return a Set-Cookie value that expires the file-auth cookie.
    Used on logout-equivalent paths (revoke / cookie rotation).
    """
    return (
        f"{FILE_AUTH_COOKIE_NAME}=; Path={cookie_path}; Max-Age=0; "
        "HttpOnly; SameSite=Lax"
    )


__all__ = [
    "FILE_AUTH_COOKIE_NAME",
    "DEFAULT_TTL_SECONDS",
    "make_secret",
    "sign",
    "verify",
    "build_set_cookie_value",
    "build_clear_cookie_value",
]
