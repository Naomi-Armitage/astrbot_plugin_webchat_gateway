"""Admin HTTP handlers: tokens (CRUD), stats, audit."""

from __future__ import annotations

import json
from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.ip_guard import IpGuard
from ..storage.base import AbstractStorage
from .admin_tokens import ServiceError, TokenService, gate_admin
from .common import (
    client_ip,
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


@dataclass
class AdminDeps:
    storage: AbstractStorage
    audit: AuditLogger
    token_service: TokenService
    allowed_origins: set[str]
    master_admin_key: str
    trust_forwarded_for: bool
    ip_guard: IpGuard


def _parse_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def _read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise ServiceError("payload_too_large", status=413) from None
    except (json.JSONDecodeError, ValueError):
        raise ServiceError("invalid_json", status=400) from None
    if not isinstance(body, dict):
        raise ServiceError("invalid_payload", status=400)
    return body


def make_admin_handlers(deps: AdminDeps):
    allowed = deps.allowed_origins

    def _err(origin, exc: ServiceError) -> web.Response:
        extra = None
        if exc.code == "ip_blocked" and str(exc):
            extra = {"Retry-After": str(exc)}
        return json_response(
            {"error": exc.code, "detail": str(exc) if str(exc) != exc.code else ""},
            status=exc.status,
            origin=origin,
            allowed_origins=allowed,
            extra_headers=extra,
        )

    async def _gate(request: web.Request, ip: str, origin: str | None) -> None:
        # Origin allow-list — match chat.py ordering: cheap filter before any
        # IpGuard accounting or master-key probe.
        if not is_origin_allowed(origin, allowed):
            raise ServiceError("forbidden_origin", status=403)
        await gate_admin(
            request,
            master_key=deps.master_admin_key,
            ip=ip,
            ip_guard=deps.ip_guard,
            audit=deps.audit,
        )

    async def post_tokens(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin)
            body = await _read_json(request)
            result = await deps.token_service.issue(
                name=str(body.get("name") or ""),
                daily_quota=body.get("daily_quota"),
                note=str(body.get("note") or ""),
                ip=ip,
            )
        except ServiceError as exc:
            return _err(origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin issue failed")
            return _err(origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "name": result.name,
                "token": result.token,
                "daily_quota": result.daily_quota,
                "note": result.note,
                "issued_at": result.issued_at,
            },
            status=201,
            origin=origin,
            allowed_origins=allowed,
        )

    async def delete_token(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        name = request.match_info.get("name", "")
        try:
            await _gate(request, ip, origin)
            ok = await deps.token_service.revoke(name=name, ip=ip)
        except ServiceError as exc:
            return _err(origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin revoke failed")
            return _err(origin, ServiceError("internal_error", status=500))
        if not ok:
            return _err(origin, ServiceError("not_found", status=404))
        return json_response(
            {"name": name, "revoked": True},
            origin=origin,
            allowed_origins=allowed,
        )

    async def list_tokens(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin)
            include = (request.query.get("include_revoked") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            rows = await deps.token_service.list_with_today(
                include_revoked=include, ip=ip
            )
        except ServiceError as exc:
            return _err(origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin list failed")
            return _err(origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "tokens": [
                    {
                        "name": r.name,
                        "daily_quota": r.daily_quota,
                        "note": r.note,
                        "created_at": r.created_at,
                        "revoked_at": r.revoked_at,
                        "today_usage": r.today_usage,
                    }
                    for r in rows
                ]
            },
            origin=origin,
            allowed_origins=allowed,
        )

    async def get_stats(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin)
            name = request.query.get("name") or ""
            days = _parse_int(request.query.get("days"), default=7, lo=1, hi=90)
            data = await deps.token_service.stats(name=name, days=days, ip=ip)
        except ServiceError as exc:
            return _err(origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin stats failed")
            return _err(origin, ServiceError("internal_error", status=500))
        return json_response(data, origin=origin, allowed_origins=allowed)

    async def get_audit(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin)
            limit = _parse_int(request.query.get("limit"), default=100, lo=1, hi=500)
            rows = await deps.storage.get_recent_audit(limit=limit)
            await deps.audit.write(
                "admin_audit", ip=ip, detail={"limit": limit, "count": len(rows)}
            )
        except ServiceError as exc:
            return _err(origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin audit failed")
            return _err(origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "events": [
                    {
                        "id": r.id,
                        "ts": r.ts,
                        "name": r.name,
                        "ip": r.ip,
                        "event": r.event,
                        "detail": r.detail,
                    }
                    for r in rows
                ]
            },
            origin=origin,
            allowed_origins=allowed,
        )

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(origin=extract_origin(request), allowed=allowed)

    return {
        "post_tokens": post_tokens,
        "delete_token": delete_token,
        "list_tokens": list_tokens,
        "get_stats": get_stats,
        "get_audit": get_audit,
        "preflight": preflight,
    }
