"""Public site config endpoint.

Returns operator-facing branding strings (site name, welcome message,
external links) so the bundled landing page can be re-skinned without
editing HTML. Unauthenticated by design — the values are already
displayed publicly to anyone who reaches the page.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def make_site_handlers(deps: SiteDeps):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    payload = {
        "site_name": deps.site_name or _DEFAULT_SITE_NAME,
        "welcome_message": deps.welcome_message,
        "show_github_link": deps.show_github_link,
        "privacy_url": deps.privacy_url,
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
