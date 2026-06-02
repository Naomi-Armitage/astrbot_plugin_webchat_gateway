"""Public site config endpoint.

Returns operator-facing branding strings (site name, welcome message,
external links) so the bundled landing page can be re-skinned without
editing HTML. Unauthenticated by design — the values are already
displayed publicly to anyone who reaches the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aiohttp import web

from .common import (
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


_DEFAULT_SITE_NAME = "WebChat Gateway"


@dataclass
class SiteDeps:
    site_name: str
    welcome_message: str
    show_github_link: bool
    privacy_url: str
    site_icon_url: str
    theme_family: str
    allowed_origins: set[str]
    trust_referer_as_origin: bool
    # Upload-config view exposed to the chat client so the FE's
    # composer enforces the same caps as the server (otherwise a
    # rebind from default 4 to e.g. 8 on the operator side wouldn't
    # take effect in the browser until a code change).
    uploads_enabled: bool
    uploads_max_file_size_mb: int
    uploads_max_attachments_per_message: int
    uploads_allowed_mime: tuple[str, ...]
    # Live read of `image_gen.enabled`. Resolved on every /site
    # request rather than snapshotted at construct time because the
    # image_gen.* fields hot-reload (admin panel save → no restart)
    # and the chat client's 生图 button visibility needs to track
    # the live state. Default factory returns False so a deployment
    # that forgets to wire the callback degrades safely (button
    # hidden) instead of false-positive (button shown, sends fail).
    image_gen_enabled_provider: Callable[[], bool] = lambda: False
    # Live read of img2img (reference-image edit) capability:
    # `image_gen.enabled AND image_gen.img2img` resolved on the bridge.
    # Drives whether the chat client keeps an attached image on an /image
    # command (vs dropping it). Default False → safe degrade (drop + notice).
    image_gen_img2img_provider: Callable[[], bool] = lambda: False


def make_site_handlers(deps: SiteDeps):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    # Branding strings stay snapshot-style — site_name / welcome /
    # privacy_url are restart-required in the schema, so re-resolving
    # them per request would be wasted work. image_gen.enabled is the
    # one field that's expected to flip live, so we read it inside
    # the handler.
    static_payload = {
        "site_name": deps.site_name or _DEFAULT_SITE_NAME,
        "welcome_message": deps.welcome_message,
        "show_github_link": deps.show_github_link,
        "privacy_url": deps.privacy_url,
        "site_icon_url": deps.site_icon_url,
        "theme_family": deps.theme_family,
        "uploads": {
            "enabled": deps.uploads_enabled,
            "max_file_size_mb": deps.uploads_max_file_size_mb,
            "max_attachments_per_message": deps.uploads_max_attachments_per_message,
            "allowed_mime": list(deps.uploads_allowed_mime),
        },
    }

    async def get_site(request: web.Request) -> web.Response:
        origin = extract_origin(request, trust_referer_as_origin=trust_referer)
        if not is_origin_allowed(origin, allowed, same_origin_host=request.host):
            return json_response(
                {"error": "forbidden_origin"},
                status=403,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=request.host,
            )
        try:
            image_gen_enabled = bool(deps.image_gen_enabled_provider())
        except Exception:
            image_gen_enabled = False
        try:
            image_gen_img2img = image_gen_enabled and bool(
                deps.image_gen_img2img_provider()
            )
        except Exception:
            image_gen_img2img = False
        payload = {
            **static_payload,
            "image_gen": {
                "enabled": image_gen_enabled,
                "img2img": image_gen_img2img,
            },
        }
        return json_response(
            payload,
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=request.host,
            extra_headers={"Cache-Control": "no-cache"},
        )

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=trust_referer
            ),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {"get_site": get_site, "preflight": preflight}
