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


def make_site_handlers(deps: SiteDeps):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    payload = {
        "site_name": deps.site_name or _DEFAULT_SITE_NAME,
        "welcome_message": deps.welcome_message,
        "show_github_link": deps.show_github_link,
        "privacy_url": deps.privacy_url,
        "theme_family": deps.theme_family,
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
