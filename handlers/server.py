"""aiohttp server bootstrap: build routing table from deps.

Routes:
- API:   /api/webchat/chat (configurable prefix), /api/webchat/me (token quota probe), /api/webchat/admin/{tokens,stats,audit,login,logout,me}, /api/webchat/site (public branding)
- UI:    / (landing), /login (token entry), {admin_ui_path} (admin panel, default /admin), /chat (chat client) — same-origin so the bundled HTML works without manual CORS entries
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from aiohttp import web

from astrbot.api import logger

from ..core.config import ConfigView
from .admin_auth_routes import AuthRouteDeps, make_auth_handlers
from .admin_stats import AdminDeps, make_admin_handlers
from .chat import (
    ChatDeps,
    make_chat_handler,
    make_chat_stream_cancel_handler,
    make_chat_stream_handler,
    make_chat_stream_resume_handler,
    make_logout_handler,
    make_me_handler,
    make_preflight_handler,
)
from .conversations import (
    ConversationDeps,
    ConversationService,
    make_conversation_handlers,
)
from .files import (
    UploadDeps,
    make_files_preflight,
    make_serve_handler,
    make_upload_handler,
)
from .site import SiteDeps, make_site_handlers
from .title import TitleDeps, make_title_handler


@dataclass
class ServerDeps:
    config: ConfigView
    chat: ChatDeps
    admin: AdminDeps
    title: TitleDeps
    conv: ConversationDeps
    conv_service: ConversationService
    upload: UploadDeps


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES_DIR = _PLUGIN_ROOT / "examples"


def _file_handler(path: Path, *, theme_family: str | None = None):
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
        headers = {
            # Short cache so reloads pick up edits during development;
            # production deployments behind a CDN should override.
            "Cache-Control": "no-cache",
        }
        if theme_family in {"classic", "notebook"}:
            html = path.read_text(encoding="utf-8")
            html = html.replace(
                "<html ",
                f'<html data-default-theme-family="{theme_family}" ',
                1,
            )
            return web.Response(text=html, content_type="text/html", headers=headers)
        return web.FileResponse(path, headers=headers)

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
    # Uploads use the same cap, so when image uploads are enabled raise the
    # ceiling to fit the largest single file plus a multipart envelope
    # overhead. The cap applies to BOTH /chat (JSON) and /upload (multipart);
    # /chat doesn't suffer from the higher value because its own
    # `max_message_length` check rejects bloated text long before we look at
    # the bytes.
    upload_cap_bytes = 0
    if cfg.uploads.enabled:
        upload_cap_bytes = cfg.uploads.max_file_size_mb * 1024 * 1024 + 256 * 1024
    body_cap = max(64 * 1024, cfg.max_message_length * 4, upload_cap_bytes)
    app = web.Application(client_max_size=body_cap)

    # Stream buffer eviction sweeper — runs for the lifetime of the app.
    # `cleanup_ctx` semantics: yield once, the framework calls it on
    # startup, awaits the generator on shutdown. The buffer's start_sweeper
    # is idempotent (no-op if already running) and stop_sweeper drains
    # gracefully with a 10s timeout so a stuck sweeper can't block plugin
    # shutdown. The buffer is owned by the registry; we reach through
    # the registry's read-only `buffer` accessor rather than carrying a
    # second ref on ServerDeps.
    stream_buffer = deps.chat.registry.buffer

    async def _stream_buffer_lifecycle(_app: web.Application) -> AsyncIterator[None]:
        await stream_buffer.start_sweeper(_app)
        try:
            yield
        finally:
            await stream_buffer.stop_sweeper()

    app.cleanup_ctx.append(_stream_buffer_lifecycle)

    chat_handler = make_chat_handler(deps.chat)
    chat_preflight = make_preflight_handler(
        cfg.allowed_origins,
        trust_referer_as_origin=deps.chat.trust_referer_as_origin,
    )

    app.router.add_post(cfg.chat_path, chat_handler)
    app.router.add_options(cfg.chat_path, chat_preflight)

    chat_stream_handler = make_chat_stream_handler(deps.chat)
    app.router.add_post(cfg.chat_stream_path, chat_stream_handler)
    app.router.add_options(cfg.chat_stream_path, chat_preflight)

    chat_stream_resume_handler = make_chat_stream_resume_handler(deps.chat)
    app.router.add_get(cfg.chat_stream_resume_path, chat_stream_resume_handler)
    app.router.add_options(cfg.chat_stream_resume_path, chat_preflight)

    chat_stream_cancel_handler = make_chat_stream_cancel_handler(deps.chat)
    app.router.add_post(cfg.chat_stream_cancel_path, chat_stream_cancel_handler)
    app.router.add_options(cfg.chat_stream_cancel_path, chat_preflight)

    me_handler = make_me_handler(deps.chat)
    app.router.add_get(cfg.me_path, me_handler)
    app.router.add_options(cfg.me_path, chat_preflight)

    logout_handler = make_logout_handler(deps.chat)
    app.router.add_post(cfg.logout_path, logout_handler)
    app.router.add_options(cfg.logout_path, chat_preflight)

    # Upload + serve. Same allow-list, same IP guard, same bearer gate
    # as /chat — wired off `deps.upload` so the handler doesn't have to
    # reach into ChatDeps for storage/audit/ip_guard.
    # Serve route is ALWAYS registered, regardless of uploads.enabled —
    # disabling new uploads must not also brick reading historical
    # images that are already in DB + storage. Only the POST upload
    # route gates on the flag.
    serve_handler = make_serve_handler(deps.upload)
    files_preflight = make_files_preflight(deps.upload)
    app.router.add_get(cfg.files_serve_path, serve_handler)
    app.router.add_options(cfg.files_serve_path, files_preflight)
    if cfg.uploads.enabled:
        upload_handler = make_upload_handler(deps.upload)
        app.router.add_post(cfg.upload_path, upload_handler)
        app.router.add_options(cfg.upload_path, files_preflight)

    title_handler = make_title_handler(deps.title)
    app.router.add_post(cfg.title_path, title_handler)
    app.router.add_options(cfg.title_path, chat_preflight)

    conv = make_conversation_handlers(deps.conv, deps.conv_service)
    app.router.add_get(cfg.conversations_path, conv["list"])
    app.router.add_options(cfg.conversations_path, conv["preflight"])
    app.router.add_get(cfg.conversations_item_path, conv["get"])
    app.router.add_patch(cfg.conversations_item_path, conv["patch"])
    app.router.add_options(cfg.conversations_item_path, conv["preflight"])
    app.router.add_post(cfg.conversations_clear_path, conv["clear"])
    app.router.add_options(cfg.conversations_clear_path, conv["preflight"])
    app.router.add_get(cfg.events_path, conv["events"])
    app.router.add_options(cfg.events_path, conv["preflight"])

    admin = make_admin_handlers(deps.admin)

    app.router.add_post(cfg.admin_tokens_path, admin["post_tokens"])
    app.router.add_get(cfg.admin_tokens_path, admin["list_tokens"])
    app.router.add_options(cfg.admin_tokens_path, admin["preflight"])

    item_path = cfg.admin_tokens_item_path
    app.router.add_delete(item_path, admin["delete_token"])
    app.router.add_patch(item_path, admin["patch_token"])
    app.router.add_options(item_path, admin["preflight"])

    regen_path = cfg.admin_tokens_regenerate_path
    app.router.add_post(regen_path, admin["regenerate_token"])
    app.router.add_options(regen_path, admin["preflight"])

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
            allow_missing_origin=deps.admin.allow_missing_origin,
        )
    )
    app.router.add_post(cfg.admin_login_path, auth["login"])
    app.router.add_options(cfg.admin_login_path, auth["preflight"])
    app.router.add_post(cfg.admin_logout_path, auth["logout"])
    app.router.add_options(cfg.admin_logout_path, auth["preflight"])
    app.router.add_get(cfg.admin_me_path, auth["me"])
    app.router.add_options(cfg.admin_me_path, auth["preflight"])

    site = make_site_handlers(
        SiteDeps(
            site_name=cfg.site_name,
            welcome_message=cfg.welcome_message,
            show_github_link=cfg.show_github_link,
            privacy_url=cfg.privacy_url,
            theme_family=cfg.theme_family,
            allowed_origins=cfg.allowed_origins,
            trust_referer_as_origin=deps.chat.trust_referer_as_origin,
            uploads_enabled=cfg.uploads.enabled,
            uploads_max_file_size_mb=cfg.uploads.max_file_size_mb,
            uploads_max_attachments_per_message=cfg.uploads.max_attachments_per_message,
            uploads_allowed_mime=tuple(cfg.uploads.allowed_mime),
        )
    )
    app.router.add_get(cfg.site_info_path, site["get_site"])
    app.router.add_options(cfg.site_info_path, site["preflight"])

    # Bundled UI (same-origin so allowed_origins doesn't need an entry).
    landing = _EXAMPLES_DIR / "landing" / "index.html"
    login_html = _EXAMPLES_DIR / "login" / "index.html"
    admin_html = _EXAMPLES_DIR / "admin_panel" / "index.html"
    chat_html = _EXAMPLES_DIR / "chat_client" / "index.html"
    app.router.add_get("/", _file_handler(landing, theme_family=cfg.theme_family))
    app.router.add_get("/login", _redirect_with_slash)
    app.router.add_get(
        "/login/", _file_handler(login_html, theme_family=cfg.theme_family)
    )
    # admin UI lives at the operator-chosen path. Only the trailing-slash
    # variant serves the page; the bare path 308s to it so relative links
    # inside the page resolve. We deliberately do NOT register
    # /admin_panel/* fallbacks: those would leak the entry regardless of
    # how obscure admin_ui_path is.
    admin_ui_path = cfg.admin_ui_path
    app.router.add_get(admin_ui_path, _redirect_with_slash)
    app.router.add_get(admin_ui_path + "/", _file_handler(admin_html))
    app.router.add_get("/chat", _redirect_with_slash)
    app.router.add_get(
        "/chat/", _file_handler(chat_html, theme_family=cfg.theme_family)
    )

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
