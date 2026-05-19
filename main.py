"""WebChat Gateway plugin entrypoint.

Wires storage + LLM bridge + HTTP server, and registers a `/webchat` admin
command group whose handlers share `TokenService` with the HTTP admin layer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.audit import AuditLogger
from .core.config import ConfigView
from .core.cookie_logout import CookieLogoutTracker
from .core.event_bus import EventBus
from .core.file_store import FileStore, make_file_store_from_config
from .core.ip_guard import IpGuard
from .core.llm_bridge import LlmBridge
from .core.prune_orchestrator import PruneOrchestrator, PruneRetentionConfig
from .core.ratelimit import PerTokenConcurrency, PerTokenUploadGate
from .core.stream_buffer import InMemoryBuffer, RedisBuffer, StreamBuffer
from .core.stream_registry import StreamRegistry
from .handlers.admin_stats import AdminDeps
from .handlers.admin_tokens import ServiceError, TokenService
from .handlers.chat import ChatDeps
from .handlers.conversations import ConversationDeps, ConversationService
from .handlers.files import UploadDeps
from .handlers.server import ServerDeps, ServerLifecycle, build_app
from .handlers.title import TitleDeps
from .storage import AbstractStorage, get_storage


class WebChatGatewayPlugin(Star):
    """Managed WebChat gateway plugin."""

    # Chat-sync retention. Events older than this are pruned by a daily
    # background task; the long-poll endpoint forces clients past the
    # cutoff to do a cold refetch via tooFar. Soft-deleted session_meta
    # rows older than the deleted-meta cutoff are physically removed.
    # Uncommitted file uploads older than the file-orphan cutoff are
    # collected in the same pass — tab-close abandonment.
    # Periodic retention prune. Cadence is short enough that
    # `_UPLOAD_ORPHAN_RETENTION_SECONDS` (1h) actually fires within
    # 1-2× that window — the original 24h cadence let uncommitted
    # uploads occupy `per_token_storage_mb` for up to 25 hours after
    # tab-close abandonment. The heavy DELETEs are bounded by the
    # expired-row count (NOT table size — indices cover the WHERE),
    # so 4× per day costs roughly the same total work as 1× per day,
    # just amortized.
    _CHAT_SYNC_PRUNE_INTERVAL_SECONDS = 6 * 3600
    _CHAT_SYNC_EVENTS_RETENTION_SECONDS = 14 * 86400
    _CHAT_SYNC_DELETED_META_RETENTION_SECONDS = 90 * 86400
    _UPLOAD_ORPHAN_RETENTION_SECONDS = 3600
    # Boot-time delay before the FIRST prune iteration. Short enough
    # that committed=0 orphans left over from a prior process crash
    # don't occupy quota for a full day; long enough that startup-
    # heavy systems aren't immediately competing with a DELETE pass.
    # Randomised so multi-instance deployments don't sync up.
    _CHAT_SYNC_PRUNE_BOOT_DELAY_MIN_SECONDS = 60
    _CHAT_SYNC_PRUNE_BOOT_DELAY_MAX_SECONDS = 120

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
        self._file_store: FileStore | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._registry: StreamRegistry | None = None
        self._cookie_logout_tracker: CookieLogoutTracker | None = None
        self._event_bus: EventBus | None = None

    async def initialize(self) -> None:
        await self._start()

    async def terminate(self) -> None:
        await self._stop()

    async def _start(self) -> None:
        cfg = ConfigView.from_raw(self.config)
        self._cfg = cfg

        if cfg.storage.driver == "mysql" and not cfg.storage.mysql_dsn:
            # Fail fast on irrecoverable config — better to let AstrBot
            # surface "plugin failed to load" than to register the
            # /webchat command group as healthy while the HTTP server
            # never binds and every command returns "插件未就绪".
            raise RuntimeError(
                "WebChatGateway: mysql driver requires mysql_dsn"
            )

        try:
            storage = get_storage(
                cfg.storage.driver,
                sqlite_path=cfg.storage.sqlite_path,
                mysql_dsn=cfg.storage.mysql_dsn,
                mysql_pool_max=cfg.storage.mysql_pool_max,
            )
            await storage.initialize()
        except Exception as exc:
            logger.exception("[WebChatGateway] storage init failed; aborting startup")
            raise RuntimeError(
                f"WebChatGateway: storage init failed: {exc}"
            ) from exc
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
            upload_gate = PerTokenUploadGate()
            llm_bridge = LlmBridge(
                self.context,
                history_turns=cfg.history_turns,
                persona_id=cfg.persona_id,
                timeout_seconds=cfg.llm_timeout_seconds,
            )

            event_bus = EventBus()
            self._event_bus = event_bus

            # FileStore (image uploads). Always constructed even when
            # `uploads.enabled=False` — the conversations layer needs a
            # FileStore handle to resolve attachments in CM history
            # backfill. With uploads disabled, the upload route is
            # simply not registered, so the store stays idle.
            try:
                file_store: FileStore = make_file_store_from_config(cfg.uploads)
            except Exception:
                logger.exception(
                    "[WebChatGateway] FileStore init failed; uploads disabled"
                )
                from .core.file_store import LocalFileStore

                file_store = LocalFileStore(root=cfg.uploads.local_path)
            self._file_store = file_store

            conv_service = ConversationService(
                storage=storage,
                audit=audit,
                event_bus=event_bus,
                cm=self.context.conversation_manager,
                file_store=file_store,
                concurrency=concurrency,
                llm_bridge=llm_bridge,
            )

            # Streaming buffer + registry. Redis backend is opt-in via
            # `streaming.redis_dsn`; empty string falls back to in-memory.
            # The buffer's eviction-audit hook writes `chat_stream_evicted`
            # rows so cap- and TTL-driven evictions stay visible in the
            # audit log.
            async def _on_evict(stream_id: str, reason: str) -> None:
                try:
                    await audit.write(
                        "chat_stream_evicted",
                        detail={"stream_id": stream_id, "reason": reason},
                    )
                except Exception:
                    logger.exception(
                        "[WebChatGateway] chat_stream_evicted audit failed"
                    )

            buffer: StreamBuffer
            if cfg.streaming.redis_dsn:
                try:
                    buffer = RedisBuffer(
                        dsn=cfg.streaming.redis_dsn,
                        grace_seconds=cfg.streaming.grace_seconds,
                        max_per_token=cfg.streaming.max_per_token,
                        max_global=cfg.streaming.max_global,
                        on_evict=_on_evict,
                    )
                except RuntimeError as exc:
                    # redis-py not installed or DSN unusable. Fall back to
                    # in-memory so the rest of the plugin still starts.
                    # Single-line warning instead of `logger.exception` —
                    # the missing-optional-dep case is a config-driven
                    # choice, not a bug, and a full traceback in the bot
                    # log is noise.
                    logger.warning(
                        "[WebChatGateway] RedisBuffer unavailable (%s); "
                        "falling back to in-memory buffer",
                        exc,
                    )
                    buffer = InMemoryBuffer(
                        grace_seconds=cfg.streaming.grace_seconds,
                        max_per_token=cfg.streaming.max_per_token,
                        max_global=cfg.streaming.max_global,
                        on_evict=_on_evict,
                    )
            else:
                buffer = InMemoryBuffer(
                    grace_seconds=cfg.streaming.grace_seconds,
                    max_per_token=cfg.streaming.max_per_token,
                    max_global=cfg.streaming.max_global,
                    on_evict=_on_evict,
                )
            registry = StreamRegistry(
                buffer=buffer,
                concurrency=concurrency,
                audit=audit,
                conv_service=conv_service,
                storage=storage,
                file_store=file_store,
            )
            self._registry = registry

            # HMAC secret for the /files-auth cookie. Rotates per
            # plugin restart (cookie invalidation is operationally
            # silent — clients re-issue on the next /me probe).
            from .core.file_cookie import (
                DEFAULT_TTL_SECONDS,
                make_secret as _make_cookie_secret,
            )

            file_cookie_secret = _make_cookie_secret()
            # In-memory tracker for server-side cookie invalidation on
            # logout. Lives only for the plugin's runtime; on restart
            # `file_cookie_secret` is rotated too, so all outstanding
            # cookies are invalidated either way. The tracker's TTL
            # mirrors the cookie's TTL so the invalidation window
            # exactly covers any cookie issued at the logout moment.
            cookie_logout_tracker = CookieLogoutTracker(
                default_ttl_seconds=DEFAULT_TTL_SECONDS,
            )
            self._cookie_logout_tracker = cookie_logout_tracker

            chat_deps = ChatDeps(
                storage=storage,
                audit=audit,
                ip_guard=ip_guard,
                concurrency=concurrency,
                llm_bridge=llm_bridge,
                conv_service=conv_service,
                registry=registry,
                file_store=file_store,
                allowed_origins=cfg.allowed_origins,
                max_message_length=cfg.max_message_length,
                max_attachments_per_message=cfg.uploads.max_attachments_per_message,
                trust_forwarded_for=cfg.trust_forwarded_for,
                file_cookie_secret=file_cookie_secret,
                file_cookie_path=cfg.files_cookie_path,
                cookie_logout_tracker=cookie_logout_tracker,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
                allow_missing_origin=cfg.allow_missing_origin,
            )
            admin_deps = AdminDeps(
                storage=storage,
                audit=audit,
                token_service=token_service,
                allowed_origins=cfg.allowed_origins,
                master_admin_key=cfg.master_admin_key,
                trust_forwarded_for=cfg.trust_forwarded_for,
                ip_guard=ip_guard,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
                allow_missing_origin=cfg.allow_missing_origin,
            )
            title_deps = TitleDeps(
                storage=storage,
                audit=audit,
                ip_guard=ip_guard,
                llm_bridge=llm_bridge,
                allowed_origins=cfg.allowed_origins,
                max_message_length=cfg.max_message_length,
                auto_title_enabled=cfg.auto_title_enabled,
                trust_forwarded_for=cfg.trust_forwarded_for,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
                allow_missing_origin=cfg.allow_missing_origin,
            )
            conv_deps = ConversationDeps(
                storage=storage,
                audit=audit,
                event_bus=event_bus,
                cm=self.context.conversation_manager,
                file_store=file_store,
                allowed_origins=cfg.allowed_origins,
                trust_forwarded_for=cfg.trust_forwarded_for,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
                allow_missing_origin=cfg.allow_missing_origin,
                ip_guard=ip_guard,
                concurrency=concurrency,
            )
            # Prefix the wire-format `url` field with the configured
            # endpoint so a non-default `endpoint_prefix` flows through
            # without separate plumbing. The handler appends `{file_id}`.
            files_serve_prefix = f"{cfg.endpoint_prefix}/files/"
            upload_deps = UploadDeps(
                storage=storage,
                audit=audit,
                ip_guard=ip_guard,
                file_store=file_store,
                upload_gate=upload_gate,
                allowed_origins=cfg.allowed_origins,
                max_file_size_mb=cfg.uploads.max_file_size_mb,
                per_token_storage_mb=cfg.uploads.per_token_storage_mb,
                allowed_mime=cfg.uploads.allowed_mime,
                storage_driver=cfg.uploads.storage_driver,
                r2_serving_mode=cfg.uploads.r2_serving_mode,
                r2_direct_link_ttl_seconds=cfg.uploads.r2_direct_link_ttl_seconds,
                files_serve_prefix=files_serve_prefix,
                trust_forwarded_for=cfg.trust_forwarded_for,
                file_cookie_secret=file_cookie_secret,
                cookie_logout_tracker=cookie_logout_tracker,
                trust_referer_as_origin=cfg.trust_referer_as_origin,
                allow_missing_origin=cfg.allow_missing_origin,
            )
            server_deps = ServerDeps(
                config=cfg,
                chat=chat_deps,
                admin=admin_deps,
                title=title_deps,
                conv=conv_deps,
                conv_service=conv_service,
                upload=upload_deps,
            )
            app = build_app(server_deps)

            await self._lifecycle.start(app, host=cfg.host, port=cfg.port)
        except Exception as exc:
            logger.exception(
                "[WebChatGateway] startup failed after storage init; tearing down"
            )
            await self._stop()
            raise RuntimeError(
                f"WebChatGateway: startup failed after storage init: {exc}"
            ) from exc

        # Background retention prune. Drops chat-sync events past the
        # retention window and physically removes long-soft-deleted session
        # meta rows. Runs on the same loop as the gateway; daily cadence is
        # plenty for the volume any single token produces.
        self._prune_task = asyncio.create_task(
            self._chat_sync_prune_loop(),
            name="webchat-chat-sync-prune",
        )

        logger.info(
            "[WebChatGateway] chat=%s admin_api=%s admin_ui=%s storage=%s allowed_origins=%s admin_key=%s llm_timeout=%ss",
            cfg.chat_path,
            cfg.admin_tokens_path,
            cfg.admin_ui_path,
            cfg.storage.driver,
            ", ".join(sorted(cfg.allowed_origins)),
            "enabled" if cfg.master_admin_key else "DISABLED",
            cfg.llm_timeout_seconds,
        )

    async def _chat_sync_prune_loop(self) -> None:
        """Drive `PruneOrchestrator.run_iteration` on the configured
        cadence with a boot-delay jitter.

        Orchestration moved to `core.prune_orchestrator` to keep the
        plugin entry class focused on AstrBot lifecycle. This wrapper
        owns the loop semantics (boot delay + interval sleep) and
        constructs a fresh orchestrator per iteration so storage /
        file_store reads are always against the live `self._XXX`
        (which `_stop` clears). Cancellation propagates cleanly via
        the outer `except asyncio.CancelledError: return`.
        """
        try:
            # Boot delay applied here so we can early-return cleanly
            # if `_start`'s teardown fires before deps are ready.
            first_delay = random.uniform(
                self._CHAT_SYNC_PRUNE_BOOT_DELAY_MIN_SECONDS,
                self._CHAT_SYNC_PRUNE_BOOT_DELAY_MAX_SECONDS,
            )
            await asyncio.sleep(first_delay)
            while True:
                storage = self._storage
                file_store = self._file_store
                if storage is None:
                    return
                orchestrator = PruneOrchestrator(
                    storage=storage,
                    file_store=file_store,
                    cm=self.context.conversation_manager,
                    config=PruneRetentionConfig(
                        events_retention_seconds=self._CHAT_SYNC_EVENTS_RETENTION_SECONDS,
                        deleted_meta_retention_seconds=self._CHAT_SYNC_DELETED_META_RETENTION_SECONDS,
                        upload_orphan_retention_seconds=self._UPLOAD_ORPHAN_RETENTION_SECONDS,
                        audit_retention_seconds=(
                            (self._cfg.audit_retention_days if self._cfg else 7)
                            * 86400
                        ),
                    ),
                    cookie_logout_tracker=self._cookie_logout_tracker,
                    event_bus=self._event_bus,
                )
                try:
                    await orchestrator.run_iteration()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "[WebChatGateway] chat-sync prune iteration failed"
                    )
                await asyncio.sleep(self._CHAT_SYNC_PRUNE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def _stop(self) -> None:
        if self._prune_task is not None:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Surface unexpected crashes during prune-loop cleanup
                # — silent-pass here would mask real bugs in the loop's
                # finally / teardown path.
                logger.exception(
                    "[WebChatGateway] prune_task drain raised on shutdown"
                )
            self._prune_task = None
        # Cancel in-flight stream drivers BEFORE storage closes. A
        # long-poll request parked in event_bus.wait() (≤25s) or a
        # streaming response mid-LLM-call (≤llm_timeout) would
        # otherwise try a storage call after close() and return 500
        # instead of a clean abort.
        if self._registry is not None:
            try:
                pending = self._registry.cancel_all_drivers()
                if pending:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=10.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[WebChatGateway] %d driver(s) didn't drain in 10s",
                            len(pending),
                        )
            except Exception:
                logger.exception(
                    "[WebChatGateway] driver cancel-all raised on shutdown"
                )
        await self._lifecycle.stop()
        if self._storage is not None:
            try:
                await self._storage.close()
            except Exception:
                logger.exception("[WebChatGateway] storage close failed")
            self._storage = None
        self._token_service = None
        self._audit = None
        self._file_store = None
        self._registry = None
        self._cookie_logout_tracker = None
        self._event_bus = None

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
        if not self._ensure_ready():
            yield event.plain_result("[WebChatGateway] 插件未就绪。")
            return
        rows = await self._token_service.list_with_today(include_revoked=True)
        if not rows:
            yield event.plain_result("[WebChatGateway] 暂无 token。")
            return
        # Cap chat-platform replies so a deployment with hundreds of tokens
        # doesn't produce a single multi-thousand-line message that the
        # IM platform truncates or rate-limits. Operators with > 30 tokens
        # should use the admin UI to browse.
        _MAX_LINES_PER_MSG = 30
        lines = ["[WebChatGateway] tokens:"]
        for r in rows:
            status = "REVOKED" if r.revoked_at else "active"
            lines.append(
                f"- {r.name} | {status} | {r.today_usage}/{r.daily_quota} 今日 | "
                f"创建 {self._format_time(r.created_at)}"
            )
        total = len(rows)
        if total > _MAX_LINES_PER_MSG:
            lines = lines[: _MAX_LINES_PER_MSG + 1]
            lines.append(
                f"... (+{total - _MAX_LINES_PER_MSG} more, 请使用 admin UI 查看完整列表)"
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
