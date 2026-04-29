"""aiohttp server bootstrap: build routing table from deps.

Routes:
- API:   /api/webchat/chat (configurable prefix), /api/webchat/admin/{tokens,stats,audit,login,logout,me}
- UI:    / (landing), /admin (admin panel), /chat (chat client) — same-origin so the bundled HTML works without manual CORS entries
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from astrbot.api import logger

from ..core.config import ConfigView
from .admin_auth_routes import AuthRouteDeps, make_auth_handlers
from .admin_stats import AdminDeps, make_admin_handlers
from .chat import ChatDeps, make_chat_handler, make_preflight_handler


@dataclass
class ServerDeps:
    config: ConfigView
    chat: ChatDeps
    admin: AdminDeps


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES_DIR = _PLUGIN_ROOT / "examples"


def _file_handler(path: Path):
    """Build a GET handler that serves a single static HTML file.

    aiohttp's add_static is overkill for three fixed pages and complicates
    the route table. Each page is self-contained (CSS + JS inlined), so a
    plain FileResponse is enough.
    """

    async def handle(_: web.Request) -> web.StreamResponse:
        if not path.is_file():
            # Plugin shipped without examples/ — return 404 rather than
            # raising so the API still works.
            return web.Response(status=404, text="ui_not_installed")
        return web.FileResponse(
            path,
            headers={
                # Short cache so reloads pick up edits during development;
                # production deployments behind a CDN should override.
                "Cache-Control": "no-cache",
            },
        )

    return handle


async def _redirect_with_slash(request: web.Request) -> web.Response:
    # /admin → /admin/  so relative links inside the page (../landing/...)
    # resolve against the admin/ directory, not /. Cheap and avoids
    # surprising 404s when someone hand-types the bare path.
    target = request.path.rstrip("/") + "/"
    if request.query_string:
        target = f"{target}?{request.query_string}"
    return web.Response(status=308, headers={"Location": target})


def build_app(deps: ServerDeps) -> web.Application:
    cfg = deps.config
    # Cap incoming body size: max_message_length is char count; multiply for
    # JSON envelope + unicode escaping headroom, with a 64 KB floor.
    body_cap = max(64 * 1024, cfg.max_message_length * 4)
    app = web.Application(client_max_size=body_cap)

    chat_handler = make_chat_handler(deps.chat)
    chat_preflight = make_preflight_handler(
        cfg.allowed_origins,
        trust_referer_as_origin=deps.chat.trust_referer_as_origin,
    )

    app.router.add_post(cfg.chat_path, chat_handler)
    app.router.add_options(cfg.chat_path, chat_preflight)

    admin = make_admin_handlers(deps.admin)

    app.router.add_post(cfg.admin_tokens_path, admin["post_tokens"])
    app.router.add_get(cfg.admin_tokens_path, admin["list_tokens"])
    app.router.add_options(cfg.admin_tokens_path, admin["preflight"])

    item_path = cfg.admin_tokens_item_path
    app.router.add_delete(item_path, admin["delete_token"])
    app.router.add_options(item_path, admin["preflight"])

    app.router.add_get(cfg.admin_stats_path, admin["get_stats"])
    app.router.add_options(cfg.admin_stats_path, admin["preflight"])

    app.router.add_get(cfg.admin_audit_path, admin["get_audit"])
    app.router.add_options(cfg.admin_audit_path, admin["preflight"])

    auth = make_auth_handlers(
        AuthRouteDeps(
            audit=deps.admin.audit,
            ip_guard=deps.admin.ip_guard,
            allowed_origins=cfg.allowed_origins,
            master_admin_key=cfg.master_admin_key,
            trust_forwarded_for=cfg.trust_forwarded_for,
            trust_referer_as_origin=deps.admin.trust_referer_as_origin,
            cookie_path=cfg.admin_cookie_path,
        )
    )
    app.router.add_post(cfg.admin_login_path, auth["login"])
    app.router.add_options(cfg.admin_login_path, auth["preflight"])
    app.router.add_post(cfg.admin_logout_path, auth["logout"])
    app.router.add_options(cfg.admin_logout_path, auth["preflight"])
    app.router.add_get(cfg.admin_me_path, auth["me"])
    app.router.add_options(cfg.admin_me_path, auth["preflight"])

    # Bundled UI (same-origin so allowed_origins doesn't need an entry).
    landing = _EXAMPLES_DIR / "landing" / "index.html"
    admin_html = _EXAMPLES_DIR / "admin_panel" / "index.html"
    chat_html = _EXAMPLES_DIR / "chat_client" / "index.html"
    app.router.add_get("/", _file_handler(landing))
    app.router.add_get("/admin", _redirect_with_slash)
    app.router.add_get("/admin/", _file_handler(admin_html))
    app.router.add_get("/chat", _redirect_with_slash)
    app.router.add_get("/chat/", _file_handler(chat_html))
    # Legacy paths (existing links inside the HTML pages reference
    # `../admin_panel/index.html` and friends). Keep them working.
    app.router.add_get("/landing/", _file_handler(landing))
    app.router.add_get("/landing/index.html", _file_handler(landing))
    app.router.add_get("/admin_panel/", _file_handler(admin_html))
    app.router.add_get("/admin_panel/index.html", _file_handler(admin_html))
    app.router.add_get("/chat_client/", _file_handler(chat_html))
    app.router.add_get("/chat_client/index.html", _file_handler(chat_html))

    return app


class ServerLifecycle:
    """Manage AppRunner + TCPSite startup/shutdown."""

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()

    async def start(self, app: web.Application, *, host: str, port: int) -> None:
        async with self._lock:
            if self._runner:
                return
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                site = web.TCPSite(runner, host, port)
                await site.start()
            except BaseException:
                # AppRunner.setup() succeeded — if TCPSite construction or
                # start fails (port in use, perms), the runner still owns
                # an aiohttp server and signal handlers. Tearing it down
                # here keeps the lifecycle re-entrant: the next `start`
                # call would otherwise see `self._runner` is None but a
                # stale runner would already be holding resources.
                try:
                    await runner.cleanup()
                except Exception:
                    logger.exception(
                        "[WebChatGateway] runner cleanup after start failure failed"
                    )
                raise
            self._runner = runner
            self._site = site
            logger.info("[WebChatGateway] HTTP server started at http://%s:%s", host, port)

    async def stop(self) -> None:
        async with self._lock:
            if not self._runner:
                return
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("[WebChatGateway] HTTP server stopped")
