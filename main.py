"""WebChat Gateway plugin entrypoint.

Wires storage + LLM bridge + HTTP server, and registers a `/webchat` admin
command group whose handlers share `TokenService` with the HTTP admin layer.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.audit import AuditLogger
from .core.config import ConfigView
from .core.ip_guard import IpGuard
from .core.llm_bridge import LlmBridge
from .core.ratelimit import PerTokenConcurrency
from .handlers.admin_stats import AdminDeps
from .handlers.admin_tokens import ServiceError, TokenService
from .handlers.chat import ChatDeps
from .handlers.server import ServerDeps, ServerLifecycle, build_app
from .storage import AbstractStorage, get_storage


class WebChatGatewayPlugin(Star):
    """Managed WebChat gateway plugin."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config
        self._cfg: ConfigView | None = None
        self._storage: AbstractStorage | None = None
        self._lifecycle = ServerLifecycle()
        self._token_service: TokenService | None = None
        self._audit: AuditLogger | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._start()

    async def terminate(self) -> None:
        await self._stop()

    async def _start(self) -> None:
        cfg = ConfigView.from_raw(self.config)
        self._cfg = cfg

        if cfg.storage.driver == "mysql" and not cfg.storage.mysql_dsn:
            logger.error(
                "[WebChatGateway] mysql driver requires mysql_dsn; aborting startup"
            )
            return

        try:
            storage = get_storage(
                cfg.storage.driver,
                sqlite_path=cfg.storage.sqlite_path,
                mysql_dsn=cfg.storage.mysql_dsn,
            )
            await storage.initialize()
        except Exception:
            logger.exception("[WebChatGateway] storage init failed; aborting startup")
            return
        self._storage = storage

        try:
            audit = AuditLogger(storage)
            self._audit = audit
            token_service = TokenService(
                storage,
                audit,
                default_daily_quota=cfg.default_daily_quota,
            )
            self._token_service = token_service

            ip_guard = IpGuard(
                storage,
                max_fails=cfg.ip_brute_force_max_fails,
                block_seconds=cfg.ip_brute_force_block_seconds,
            )
            concurrency = PerTokenConcurrency()
            llm_bridge = LlmBridge(
                self.context,
                history_turns=cfg.history_turns,
                persona_id=cfg.persona_id,
                timeout_seconds=cfg.llm_timeout_seconds,
            )

            chat_deps = ChatDeps(
                storage=storage,
                audit=audit,
                ip_guard=ip_guard,
                concurrency=concurrency,
                llm_bridge=llm_bridge,
                allowed_origins=cfg.allowed_origins,
                max_message_length=cfg.max_message_length,
                trust_forwarded_for=cfg.trust_forwarded_for,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
            )
            admin_deps = AdminDeps(
                storage=storage,
                audit=audit,
                token_service=token_service,
                allowed_origins=cfg.allowed_origins,
                master_admin_key=cfg.master_admin_key,
                trust_forwarded_for=cfg.trust_forwarded_for,
                ip_guard=ip_guard,
            )
            server_deps = ServerDeps(config=cfg, chat=chat_deps, admin=admin_deps)
            app = build_app(server_deps)

            await self._lifecycle.start(app, host=cfg.host, port=cfg.port)
        except Exception:
            logger.exception(
                "[WebChatGateway] startup failed after storage init; tearing down"
            )
            await self._stop()
            return

        logger.info(
            "[WebChatGateway] chat=%s admin=%s storage=%s allowed_origins=%s admin_key=%s llm_timeout=%ss",
            cfg.chat_path,
            cfg.admin_tokens_path,
            cfg.storage.driver,
            ", ".join(sorted(cfg.allowed_origins)),
            "enabled" if cfg.master_admin_key else "DISABLED",
            cfg.llm_timeout_seconds,
        )

    async def _stop(self) -> None:
        await self._lifecycle.stop()
        if self._storage is not None:
            try:
                await self._storage.close()
            except Exception:
                logger.exception("[WebChatGateway] storage close failed")
            self._storage = None
        self._token_service = None
        self._audit = None

    # ----- AstrBot in-bot admin commands -----

    @filter.command_group("webchat")
    def webchat_group(self):
        """`/webchat` admin command group."""

    @staticmethod
    def _format_time(ts: int | None) -> str:
        if not ts:
            return "-"
        try:
            return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, OverflowError):
            return str(ts)

    def _ensure_ready(self) -> bool:
        return self._token_service is not None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @webchat_group.command("issue")
    async def cmd_issue(
        self,
        event: AstrMessageEvent,
        name: str,
        daily_quota: int = 0,
    ):
        if not event.is_admin():
            return
        if not self._ensure_ready():
            yield event.plain_result("[WebChatGateway] 插件未就绪，请先检查日志。")
            return
        if not event.is_private_chat():
            yield event.plain_result(
                "[WebChatGateway] 出于安全考虑，请在私聊中执行 /webchat issue。"
            )
            return
        try:
            result = await self._token_service.issue(
                name=name,
                daily_quota=daily_quota or None,
                note=f"issued via bot at {self._format_time(int(_dt.datetime.now().timestamp()))}",
            )
        except ServiceError as exc:
            yield event.plain_result(
                f"[WebChatGateway] 签发失败: {exc.code} {exc}".strip()
            )
            return
        yield event.plain_result(
            "[WebChatGateway] 已签发 (此 Token 仅显示一次,请立即保存):\n"
            f"name = {result.name}\n"
            f"daily_quota = {result.daily_quota}\n"
            f"token = {result.token}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @webchat_group.command("revoke")
    async def cmd_revoke(self, event: AstrMessageEvent, name: str):
        if not event.is_admin():
            return
        if not self._ensure_ready():
            yield event.plain_result("[WebChatGateway] 插件未就绪。")
            return
        try:
            ok = await self._token_service.revoke(name=name)
        except ServiceError as exc:
            yield event.plain_result(f"[WebChatGateway] 撤销失败: {exc.code}")
            return
        yield event.plain_result(
            f"[WebChatGateway] {'已撤销' if ok else '未找到或已撤销'}: {name}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @webchat_group.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        if not event.is_admin():
            return
        if not self._ensure_ready():
            yield event.plain_result("[WebChatGateway] 插件未就绪。")
            return
        rows = await self._token_service.list_with_today(include_revoked=True)
        if not rows:
            yield event.plain_result("[WebChatGateway] 暂无 token。")
            return
        lines = ["[WebChatGateway] tokens:"]
        for r in rows:
            status = "REVOKED" if r.revoked_at else "active"
            lines.append(
                f"- {r.name} | {status} | {r.today_usage}/{r.daily_quota} 今日 | "
                f"创建 {self._format_time(r.created_at)}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @webchat_group.command("stats")
    async def cmd_stats(
        self,
        event: AstrMessageEvent,
        name: str,
        days: int = 7,
    ):
        if not event.is_admin():
            return
        if not self._ensure_ready():
            yield event.plain_result("[WebChatGateway] 插件未就绪。")
            return
        try:
            data = await self._token_service.stats(name=name, days=days)
        except ServiceError as exc:
            yield event.plain_result(f"[WebChatGateway] 查询失败: {exc.code}")
            return
        history: list[dict[str, Any]] = data["history"]
        total = sum(item["count"] for item in history)
        lines = [
            f"[WebChatGateway] {data['name']} 近 {len(history)} 天用量 (合计 {total},"
            f" 配额 {data['daily_quota']}):"
        ]
        for item in history:
            lines.append(f"- {item['day']}: {item['count']}")
        if data.get("revoked"):
            lines.append("状态: REVOKED")
        yield event.plain_result("\n".join(lines))
