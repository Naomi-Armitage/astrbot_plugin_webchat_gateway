"""Admin HTTP handlers: live log viewer + SSE tail.

Surfaces the in-process `LogBuffer` (see `core/log_buffer.py`) so the
admin panel can show recent log lines + tail new ones live, without
the operator having to SSH to AstrBot's host to read journald / the
main log file. Same audience and same gate as the rest of `/admin/*`.

Two endpoints:

  * `GET /admin/logs` — replay the buffer's tail. Query:
      since=<id>      cursor; return entries with id > since
      level=<NAME>    "at-or-above" filter (INFO also gets WARN+)
      grep=<sub>      case-insensitive substring on message
      limit=<N>       cap (default 500, max == buffer capacity)
    Response: `{"entries": [...], "max_id": <int>, "capacity": <int>}`
    Clients persist `max_id` and feed it back as `since` on the
    next call.

  * `GET /admin/logs/stream` — SSE feed. After auth + (optional)
    initial-since query, the handler emits each new entry as a
    `data: { ... }` frame and a periodic `: keepalive` comment so
    intermediate proxies don't reap idle connections. Browser
    consumers reuse the existing `consumeSseStream` pattern from
    the chat client.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.ip_guard import IpGuard
from ..core.log_buffer import LEVEL_NAMES, LogBuffer, entry_to_dict
from .admin_tokens import ServiceError, gate_admin
from .common import (
    build_cors_headers,
    client_ip,
    error_response,
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


# SSE heartbeat cadence. Short enough that nginx / cloudflare default
# idle-timeout (60s) doesn't cut a quiet stream; long enough that a
# busy stream isn't drowned in comment frames.
_SSE_KEEPALIVE_SECONDS = 20.0

# Default + cap on `limit` for the replay endpoint. The cap is taken
# from the buffer's own capacity at handler-build time (passing it
# through the dataclass would couple the handler to the buffer ctor
# arg; cleaner to read deps.buffer.capacity at call time).
_DEFAULT_REPLAY_LIMIT = 500


@dataclass
class AdminLogsDeps:
    """Wiring surface for the /admin/logs endpoints."""

    buffer: LogBuffer
    audit: AuditLogger
    allowed_origins: set[str]
    master_admin_key: str
    ip_guard: IpGuard
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


def _parse_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _normalise_level(raw: str | None) -> str | None:
    if not raw:
        return None
    upper = str(raw).strip().upper()
    if upper in LEVEL_NAMES:
        return upper
    return None


def make_admin_logs_handlers(deps: AdminLogsDeps):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    def _origin(request: web.Request) -> str | None:
        return extract_origin(request, trust_referer_as_origin=trust_referer)

    def _err(request: web.Request, origin, exc: ServiceError) -> web.Response:
        return error_response(request, origin=origin, allowed=allowed, exc=exc)

    async def _gate(
        request: web.Request,
        ip: str,
        origin: str | None,
        *,
        allow_missing: bool,
    ) -> None:
        if not is_origin_allowed(
            origin,
            allowed,
            same_origin_host=request.host,
            allow_missing=allow_missing,
        ):
            raise ServiceError("forbidden_origin", status=403)
        await gate_admin(
            request,
            master_key=deps.master_admin_key,
            ip=ip,
            ip_guard=deps.ip_guard,
            audit=deps.audit,
        )

    async def get_logs(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            # GETs allow missing Origin so curl-driven log scraping
            # still works for admins; the master-key still gates.
            await _gate(request, ip, origin, allow_missing=True)
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin logs GET gate failed")
            return _err(
                request, origin, ServiceError("internal_error", status=500)
            )
        q = request.query
        since = _parse_int(q.get("since"), default=0, lo=0, hi=2**62)
        level = _normalise_level(q.get("level"))
        grep = (q.get("grep") or "").strip() or None
        limit = _parse_int(
            q.get("limit"),
            default=_DEFAULT_REPLAY_LIMIT,
            lo=1,
            hi=deps.buffer.capacity,
        )
        entries, max_id = deps.buffer.snapshot(
            since=since, level=level, grep=grep, limit=limit
        )
        # No audit row per GET — operators may poll this; one row per
        # poll would drown audit log. Drop a single row when SSE
        # subscribes, which IS a coarse-grained event.
        return json_response(
            {
                "entries": [entry_to_dict(e) for e in entries],
                "max_id": max_id,
                "capacity": deps.buffer.capacity,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def stream_logs(request: web.Request) -> web.StreamResponse:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin, allow_missing=True)
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin logs stream gate failed")
            return _err(
                request, origin, ServiceError("internal_error", status=500)
            )
        q = request.query
        since = _parse_int(q.get("since"), default=0, lo=0, hi=2**62)
        level = _normalise_level(q.get("level"))
        grep = (q.get("grep") or "").strip() or None

        cors = build_cors_headers(
            origin, allowed, same_origin_host=request.host
        )
        response = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                # nginx default response buffer would otherwise hold
                # everything until the connection closes — defeats
                # streaming.
                "X-Accel-Buffering": "no",
            },
        )

        try:
            await response.prepare(request)
        except (ConnectionResetError, asyncio.CancelledError):
            return response
        except Exception:
            logger.exception(
                "[WebChatGateway] admin logs SSE handshake failed"
            )
            return response

        # One audit row per subscription — coarse-grained, doesn't
        # drown the log even on a chatty page.
        try:
            await deps.audit.write(
                "admin_logs_view",
                ip=ip,
                detail={"mode": "stream", "since": since, "level": level or "", "grep": grep or ""},
            )
        except Exception:
            logger.exception("[WebChatGateway] admin_logs_view audit failed")

        cursor = since
        last_keepalive = time.monotonic()
        try:
            # Initial backfill: drain whatever the buffer already has
            # past `since` so the browser doesn't have to make a
            # separate GET-then-upgrade round-trip.
            entries, cursor = deps.buffer.snapshot(
                since=cursor, level=level, grep=grep, limit=deps.buffer.capacity
            )
            for entry in entries:
                payload = json.dumps(
                    entry_to_dict(entry), ensure_ascii=False, default=str
                )
                await response.write(
                    f"data: {payload}\n\n".encode("utf-8")
                )
            while True:
                await deps.buffer.wait_for_new(
                    timeout=_SSE_KEEPALIVE_SECONDS
                )
                now = time.monotonic()
                entries, cursor = deps.buffer.snapshot(
                    since=cursor,
                    level=level,
                    grep=grep,
                    limit=deps.buffer.capacity,
                )
                if entries:
                    for entry in entries:
                        payload = json.dumps(
                            entry_to_dict(entry),
                            ensure_ascii=False,
                            default=str,
                        )
                        await response.write(
                            f"data: {payload}\n\n".encode("utf-8")
                        )
                    last_keepalive = now
                elif now - last_keepalive >= _SSE_KEEPALIVE_SECONDS:
                    # Heartbeat comment frame — keeps intermediate
                    # proxies from reaping the connection on quiet
                    # systems. Comments start with `:` per the SSE
                    # spec and don't dispatch any event.
                    try:
                        await response.write(b": keepalive\n\n")
                    except (
                        ConnectionResetError,
                        asyncio.CancelledError,
                    ):
                        break
                    last_keepalive = now
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception(
                "[WebChatGateway] admin logs SSE pump raised"
            )
        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=_origin(request),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {
        "get_logs": get_logs,
        "stream_logs": stream_logs,
        "preflight": preflight,
    }


__all__ = ["AdminLogsDeps", "make_admin_logs_handlers"]
