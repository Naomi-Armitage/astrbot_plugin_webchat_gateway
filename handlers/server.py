"""aiohttp server bootstrap: build routing table from deps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiohttp import web

from astrbot.api import logger

from ..core.config import ConfigView
from .admin_stats import AdminDeps, make_admin_handlers
from .chat import ChatDeps, make_chat_handler, make_preflight_handler


@dataclass
class ServerDeps:
    config: ConfigView
    chat: ChatDeps
    admin: AdminDeps


def build_app(deps: ServerDeps) -> web.Application:
    app = web.Application()
    cfg = deps.config

    chat_handler = make_chat_handler(deps.chat)
    chat_preflight = make_preflight_handler(cfg.allowed_origins)

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
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, host, port)
            await self._site.start()
            logger.info("[WebChatGateway] HTTP server started at http://%s:%s", host, port)

    async def stop(self) -> None:
        async with self._lock:
            if not self._runner:
                return
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("[WebChatGateway] HTTP server stopped")
