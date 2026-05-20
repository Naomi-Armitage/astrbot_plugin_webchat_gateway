"""Admin HTTP handlers: settings whitelist GET + PATCH + service restart.

Operates on the live ``AstrBotConfig`` mapping (dict-like) passed in
through ``AdminSettingsDeps.config``. Writes go through the schema
helpers in ``core.settings_schema`` so type coercion + range checks +
dotted-path navigation stay in one place.

PATCH is atomic: every key in ``updates`` is validated first, and only
if ALL validate does the handler call ``config.save_config()`` and the
optional reload callback. A single bad key rejects the whole batch.

POST /admin/restart triggers the plugin's own ``_stop`` + ``_start``
in a fire-and-forget background task — boot-time config (host/port/
storage stay frozen anyway) makes a full lifecycle bounce the only
way to surface most settings changes without operator intervention
on the AstrBot side. The handler returns 202 BEFORE the restart runs
so the response itself isn't cancelled by the server shutting down
mid-write.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.ip_guard import IpGuard
from ..core.settings_schema import (
    FIELDS,
    SettingField,
    SettingsError,
    apply_update,
    read_value,
    validate,
)
from .admin_tokens import ServiceError, gate_admin
from .common import (
    client_ip,
    error_response,
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


@dataclass
class AdminSettingsDeps:
    config: Any  # AstrBotConfig / dict-like
    audit: AuditLogger
    allowed_origins: set[str]
    master_admin_key: str
    ip_guard: IpGuard
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False
    # Called AFTER save_config succeeds so the runtime ConfigView in
    # the plugin host can be rebuilt to pick up the new values on the
    # next read (audit_retention_days, et al.). Optional so tests can
    # stub it out.
    on_reload: Callable[[], Awaitable[None]] | None = None
    # Called when the operator clicks "重启服务" in the admin panel.
    # Should perform a full `_stop` + `_start` cycle. The handler
    # schedules this as a background task AFTER it returns the 202
    # response (otherwise the in-flight response would be cancelled
    # by the aiohttp server going away mid-restart). Optional so
    # tests can stub it out.
    on_restart: Callable[[], Awaitable[None]] | None = None


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


def _field_payload(spec: SettingField, value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": spec.key,
        "label": spec.label or spec.key,
        "section": spec.section,
        "type": spec.type,
        "value": value,
        "hint": spec.hint,
        "restart_required": spec.restart_required,
        "secret": spec.secret,
    }
    if spec.type == "int":
        payload["min"] = spec.min
        payload["max"] = spec.max
    elif spec.type == "options":
        payload["options"] = list(spec.options)
    return payload


def make_admin_settings_handlers(deps: AdminSettingsDeps):
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

    async def get_settings(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            # GETs allow missing Origin so curl/admin scripts can read
            # the schema; the master key (or session cookie) still gates.
            await _gate(request, ip, origin, allow_missing=True)
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin settings GET gate failed")
            return _err(request, origin, ServiceError("internal_error", status=500))
        # FIELDS is already in stable section + intra-section order; the
        # client renders that order verbatim so no re-sorting needed.
        items = [_field_payload(f, read_value(deps.config, f.key)) for f in FIELDS]
        return json_response(
            {"fields": items},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    async def patch_settings(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(
                request, ip, origin, allow_missing=deps.allow_missing_origin
            )
            body = await _read_json(request)
            updates = body.get("updates")
            if not isinstance(updates, dict) or not updates:
                raise ServiceError("invalid_payload", status=400)

            # Pass 1: validate every key + value before touching config.
            # Iterating in stable order makes the audit list deterministic
            # for any given payload (dict preserves insertion order in
            # Python 3.7+; the wire format already implies an order).
            staged: list[tuple[SettingField, str, Any]] = []
            for key, raw in updates.items():
                if not isinstance(key, str):
                    raise SettingsError("unknown_field", 400)
                spec, normalised = validate(key, raw)
                staged.append((spec, key, normalised))
        except SettingsError as exc:
            return _err(
                request,
                origin,
                ServiceError(exc.code, status=exc.status, message=str(exc)),
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin settings PATCH validate failed")
            return _err(request, origin, ServiceError("internal_error", status=500))

        # Pass 2: write through. Validation already coerced every value
        # so apply_update only ever fails for "unknown_field" — which is
        # impossible here since validate already accepted each key.
        try:
            for _spec, key, _value in staged:
                apply_update(deps.config, key, _value)
            save_config = getattr(deps.config, "save_config", None)
            if callable(save_config):
                # AstrBotConfig.save_config is synchronous; call directly.
                # If a deployment swaps in an async one, the result is
                # awaited so we don't leak a never-awaited coroutine.
                result = save_config()
                if hasattr(result, "__await__"):
                    await result
            if deps.on_reload is not None:
                await deps.on_reload()
        except Exception:
            logger.exception("[WebChatGateway] admin settings save/reload failed")
            return _err(
                request, origin, ServiceError("internal_error", status=500)
            )

        saved_keys = [key for _spec, key, _value in staged]
        restart_required = [
            key for spec, key, _value in staged if spec.restart_required
        ]
        hot_reloaded = [
            key for spec, key, _value in staged if not spec.restart_required
        ]

        try:
            await deps.audit.write(
                "admin_settings_update",
                ip=ip,
                detail={"keys": saved_keys},
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] admin_settings_update audit write failed"
            )

        return json_response(
            {
                "saved": saved_keys,
                "restart_required": restart_required,
                "hot_reloaded": hot_reloaded,
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

    async def post_restart(request: web.Request) -> web.Response:
        origin = _origin(request)
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        try:
            await _gate(
                request, ip, origin, allow_missing=deps.allow_missing_origin
            )
        except ServiceError as exc:
            return _err(request, origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] admin restart gate failed")
            return _err(request, origin, ServiceError("internal_error", status=500))

        if deps.on_restart is None:
            # Restart wiring is optional — if the plugin host didn't
            # supply a callback, surface 503 rather than pretending it
            # worked. UI can fall back to "请联系管理员" or read the
            # AstrBot logs.
            return _err(
                request, origin, ServiceError("restart_not_supported", status=503)
            )

        try:
            await deps.audit.write(
                "admin_restart",
                ip=ip,
                detail={"phase": "requested"},
            )
        except Exception:
            logger.exception("[WebChatGateway] admin_restart audit write failed")

        # Schedule the actual stop+start in a background task so this
        # handler can finish writing its 202 first. A small initial
        # sleep gives aiohttp time to flush the response onto the
        # socket before the server lifecycle starts tearing it down.
        async def _delayed_restart() -> None:
            try:
                await asyncio.sleep(0.25)
                await deps.on_restart()  # type: ignore[misc]
            except Exception:
                # The restart implementation should already log its own
                # exceptions; this is the last-resort net. Operator
                # falls back to AstrBot's plugin-reload to recover.
                logger.exception("[WebChatGateway] admin restart failed")

        # We deliberately don't track the task — the new lifecycle
        # will be installed by _start anyway, and we don't want the
        # outgoing 202 to block on it.
        asyncio.create_task(
            _delayed_restart(), name="webchat-admin-restart"
        )

        return json_response(
            {"status": "restarting"},
            status=202,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
        )

    return {
        "get_settings": get_settings,
        "patch_settings": patch_settings,
        "post_restart": post_restart,
        "preflight": preflight,
    }
