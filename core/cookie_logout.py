"""In-memory tracker for server-side cookie invalidation on logout.

Problem
-------
The file-auth cookie (`wcg_file`, see `core/file_cookie.py`) is HMAC-
signed and HttpOnly + SameSite=Lax. When a user logs out from the chat
page, the frontend can clear the browser-side cookie (Max-Age=0) — but
the cookie's signature remains valid against the per-plugin HMAC secret
until either (a) the cookie's `exp_ts` passes, or (b) the plugin
restarts (rotating the secret), or (c) the admin regenerates the
token (rotating its hash, which is folded into the signature).

A user who clicked "logout" reasonably expects their cookie to be
killed *immediately*, not "in 24 hours" or "when the admin rotates
your token". If the cookie was exfiltrated (e.g., by a malicious
browser extension that bypassed HttpOnly via host permissions), the
attacker keeps using it post-logout until natural expiry.

Mechanism
---------
We keep a per-process `dict[str, int]` mapping `token_name → invalid
exp_ts threshold`. On logout, we record
`threshold = now + default_ttl_seconds`. Any cookie whose `exp_ts`
is **less than or equal to** the threshold was issued at or before
the logout moment (because `issued_at = exp_ts - ttl`) and is
considered invalidated. Cookies issued AFTER the logout have a
fresh `exp_ts > threshold` and verify normally.

The dict lives only in the gateway process. Multi-process deployments
do not coordinate — same caveat as `PerTokenConcurrency`. Plugin
restart loses the dict, but `file_cookie_secret` is rotated on the
same event so all cookies are invalidated anyway. Net effect: no
worse than the existing single-process invariant.

Storage layer is intentionally untouched — there is no schema
migration for this feature. The state is recoverable (rotates with
the secret) and the failure mode (a logged-out cookie surviving a
plugin restart for the brief window where the new secret hasn't
been minted yet) is impossible by construction: the secret is set
in `WebChatGatewayPlugin._start` before the HTTP server accepts any
requests.
"""

from __future__ import annotations

import time


class CookieLogoutTracker:
    """Track per-token logout timestamps to invalidate stale cookies."""

    def __init__(self, *, default_ttl_seconds: int) -> None:
        self._thresholds: dict[str, int] = {}
        # Floor at 60s so a misconfigured TTL doesn't degenerate to a
        # zero-second window that effectively bypasses the invalidation.
        self._default_ttl = max(60, int(default_ttl_seconds))

    def record(self, token_name: str, *, now: int | None = None) -> int:
        """Mark `token_name` as logged out at `now` (defaults to current
        time). Returns the new rejection threshold — cookies with
        `exp_ts <= threshold` are considered invalidated.

        Uses `max` against any existing threshold so a re-logout cannot
        shrink the invalidation window from a prior longer logout.
        """
        ts = int(time.time()) if now is None else int(now)
        threshold = ts + self._default_ttl
        existing = self._thresholds.get(token_name, 0)
        if threshold > existing:
            self._thresholds[token_name] = threshold
        return self._thresholds[token_name]

    def is_invalidated(self, token_name: str, *, exp_ts: int) -> bool:
        """True iff a cookie with the given `exp_ts` has been
        invalidated by a recorded logout for `token_name`.

        A cookie issued AFTER the logout has `exp_ts > threshold`
        (because `issued_at > logout_time` → `exp_ts =
        issued_at + ttl > logout_time + ttl = threshold`) and
        returns False — verifies normally.
        """
        threshold = self._thresholds.get(token_name)
        if threshold is None:
            return False
        return exp_ts <= threshold


__all__ = ["CookieLogoutTracker"]
