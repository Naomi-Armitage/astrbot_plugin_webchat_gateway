"""Admin HTTP handlers: tokens (CRUD), stats, audit."""

from __future__ import annotations

import json
from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.ip_guard import IpGuard
from ..storage.base import _UNSET, AbstractStorage
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
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


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

    async def _gate(
        request: web.Request,
        ip: str,
        origin: str | None,
        *,
        allow_missing: bool,
    ) -> None:
        # Origin allow-list — match chat.py ordering: cheap filter before any
        # IpGuard accounting or master-key probe.
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

    async def post_tokens(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin, allow_missing=deps.allow_missing_origin)
            body = await _read_json(request)
            # `expires_at` is optional on issuance: pass it through if present
            # so an admin can mint a token that already has an expiry. Absent
            # key (vs explicit null) leaves it unset — both end up as None
            # because the service treats null as "never expires".
            result = await deps.token_service.issue(
                name=str(body.get("name") or ""),
                daily_quota=body.get("daily_quota"),
                note=str(body.get("note") or ""),
                expires_at=body.get("expires_at"),
                ip=ip,
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin issue failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "name": result.name,
                "token": result.token,
                "daily_quota": result.daily_quota,
                "note": result.note,
                "issued_at": result.issued_at,
                "expires_at": result.expires_at,
            },
            status=201,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def delete_token(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        name = request.match_info.get("name", "")
        try:
            await _gate(request, ip, origin, allow_missing=deps.allow_missing_origin)
            ok = await deps.token_service.revoke(name=name, ip=ip)
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin revoke failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        if not ok:
            return _err(request, origin, ServiceError("not_found", status=404))
        return json_response(
            {"name": name, "revoked": True},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    def _summary_payload(summary) -> dict:
        return {
            "name": summary.name,
            "daily_quota": summary.daily_quota,
            "note": summary.note,
            "created_at": summary.created_at,
            "revoked_at": summary.revoked_at,
            "expires_at": summary.expires_at,
            "today_usage": summary.today_usage,
        }

    async def patch_token(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        url_name = request.match_info.get("name", "")
        try:
            await _gate(request, ip, origin, allow_missing=deps.allow_missing_origin)
            body = await _read_json(request)

            current_name = url_name
            summary = None

            # 1. Revocation toggle. Done first so a single PATCH that both
            # restores and updates fields cannot be observed by a client
            # mid-step in the "revoked + new quota" intermediate state.
            if "revoked" in body:
                raw_revoked = body.get("revoked")
                if not isinstance(raw_revoked, bool):
                    raise ServiceError("invalid_payload", status=400)
                summary = await deps.token_service.set_revoked(
                    name=current_name, revoked=raw_revoked, ip=ip
                )

            # 2. Field updates (daily_quota, note, expires_at). Service
            # treats "key missing" and "key present" distinctly so callers
            # can clear `expires_at` by sending `null` without also
            # rewriting other columns.
            field_keys = {"daily_quota", "note", "expires_at"}
            if any(k in body for k in field_keys):
                expires_arg: object = _UNSET
                if "expires_at" in body:
                    expires_arg = body.get("expires_at")
                summary = await deps.token_service.update_fields(
                    name=current_name,
                    daily_quota=body.get("daily_quota"),
                    note=body.get("note"),
                    expires_at=expires_arg,
                    ip=ip,
                )

            # 3. Rename — last so audit/history under the old name is
            # complete before the cascade rewrites it.
            if "new_name" in body:
                raw_new = body.get("new_name")
                if not isinstance(raw_new, str):
                    raise ServiceError("invalid_name", status=400)
                if raw_new.strip() != current_name:
                    summary = await deps.token_service.rename(
                        old_name=current_name, new_name=raw_new, ip=ip
                    )
                    current_name = summary.name

            if summary is None:
                # Empty body / no recognised keys — surface the current row
                # so the admin UI's optimistic refresh has a value to render.
                summary = await deps.token_service.update_fields(
                    name=current_name, ip=ip
                )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin patch failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        return json_response(
            _summary_payload(summary),
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def regenerate_token(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        name = request.match_info.get("name", "")
        try:
            await _gate(request, ip, origin, allow_missing=deps.allow_missing_origin)
            # Body is optional — empty body means "generate random". Tolerate
            # zero-length body as well as an empty `{}` so the UI's
            # `fetch(..., {method: "POST"})` without a body works.
            if request.can_read_body and request.content_length:
                body = await _read_json(request)
            else:
                body = {}
            custom_token = body.get("custom_token")
            result = await deps.token_service.regenerate(
                name=name, custom_token=custom_token, ip=ip
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin regenerate failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "name": result.name,
                "token": result.token,
                "daily_quota": result.daily_quota,
                "note": result.note,
                "expires_at": result.expires_at,
                "revoked_at": result.revoked_at,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def list_tokens(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin, allow_missing=True)
            include = (request.query.get("include_revoked") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            rows = await deps.token_service.list_with_today(
                include_revoked=include, ip=ip
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin list failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        return json_response(
            {
                "tokens": [
                    {
                        "name": r.name,
                        "daily_quota": r.daily_quota,
                        "note": r.note,
                        "created_at": r.created_at,
                        "revoked_at": r.revoked_at,
                        "expires_at": r.expires_at,
                        "today_usage": r.today_usage,
                    }
                    for r in rows
                ]
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def get_stats(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin, allow_missing=True)
            name = request.query.get("name") or ""
            days = _parse_int(request.query.get("days"), default=7, lo=1, hi=90)
            data = await deps.token_service.stats(name=name, days=days, ip=ip)
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin stats failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        return json_response(
            data,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def get_audit(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(request, ip, origin, allow_missing=True)
            limit = _parse_int(request.query.get("limit"), default=100, lo=1, hi=500)
            rows = await deps.storage.get_recent_audit(limit=limit)
            await deps.audit.write(
                "admin_audit", ip=ip, detail={"limit": limit, "count": len(rows)}
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin audit failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
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
            same_origin_host=request.host,
        )

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=_origin(request),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {
        "post_tokens": post_tokens,
        "delete_token": delete_token,
        "patch_token": patch_token,
        "regenerate_token": regenerate_token,
        "list_tokens": list_tokens,
        "get_stats": get_stats,
        "get_audit": get_audit,
        "preflight": preflight,
    }
