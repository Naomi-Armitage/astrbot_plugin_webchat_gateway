"""Plugin configuration view: parse, clamp, and validate raw config dict."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger


def _get(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _clamp_int(raw: Any, *, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _parse_bool(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _parse_origins(raw: Any) -> set[str]:
    if raw is None:
        return {"*"}
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        return {"*"}
    out = {x for x in items if x}
    return out or {"*"}


def _normalize_prefix(raw: Any) -> str:
    path = str(raw or "/api/webchat").strip() or "/api/webchat"
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/api/webchat"


_ADMIN_UI_PATH_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/"
)


def _normalize_admin_ui_path(raw: Any) -> str:
    path = str(raw or "").strip()
    if not path:
        return "/admin"
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/") or "/admin"
    if "//" in path or any(ch not in _ADMIN_UI_PATH_ALLOWED for ch in path):
        logger.warning(
            "[WebChatGateway] admin_ui_path=%r contains unsupported characters; fallback to /admin",
            path,
        )
        return "/admin"
    if len(path) > 128:
        logger.warning(
            "[WebChatGateway] admin_ui_path is longer than 128 chars; fallback to /admin"
        )
        return "/admin"
    if path in {"/", "/chat", "/api"} or path.startswith("/api/"):
        logger.warning(
            "[WebChatGateway] admin_ui_path=%r collides with reserved route; fallback to /admin",
            path,
        )
        return "/admin"
    return path


@dataclass(frozen=True)
class StorageConfig:
    driver: str
    sqlite_path: str
    mysql_dsn: str


@dataclass(frozen=True)
class ConfigView:
    host: str
    port: int
    endpoint_prefix: str
    admin_ui_path: str
    allowed_origins: set[str]
    max_message_length: int
    history_turns: int
    auto_title_enabled: bool
    persona_id: str
    default_daily_quota: int
    ip_brute_force_max_fails: int
    ip_brute_force_block_seconds: int
    trust_forwarded_for: bool
    trust_referer_as_origin: bool
    allow_missing_origin: bool
    master_admin_key: str
    llm_timeout_seconds: int
    site_name: str
    welcome_message: str
    show_github_link: bool
    privacy_url: str
    theme_family: str
    storage: StorageConfig

    @property
    def chat_path(self) -> str:
        return f"{self.endpoint_prefix}/chat"

    @property
    def me_path(self) -> str:
        return f"{self.endpoint_prefix}/me"

    @property
    def title_path(self) -> str:
        return f"{self.endpoint_prefix}/title"

    @property
    def site_info_path(self) -> str:
        return f"{self.endpoint_prefix}/site"

    @property
    def admin_tokens_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/tokens"

    @property
    def admin_tokens_item_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/tokens/{{name}}"

    @property
    def admin_stats_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/stats"

    @property
    def admin_audit_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/audit"

    @property
    def admin_login_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/login"

    @property
    def admin_logout_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/logout"

    @property
    def admin_me_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/me"

    @property
    def admin_cookie_path(self) -> str:
        return f"{self.endpoint_prefix}/admin"

    @classmethod
    def from_raw(cls, cfg: Any) -> "ConfigView":
        host = str(_get(cfg, "host", "0.0.0.0")).strip() or "0.0.0.0"
        port = _clamp_int(_get(cfg, "port"), default=6186, lo=1, hi=65535)
        prefix = _normalize_prefix(_get(cfg, "endpoint_prefix", "/api/webchat"))
        admin_ui_path = _normalize_admin_ui_path(_get(cfg, "admin_ui_path", "/admin"))
        origins = _parse_origins(_get(cfg, "allowed_origins"))
        max_msg = _clamp_int(
            _get(cfg, "max_message_length"), default=4000, lo=16, hi=200_000
        )
        history = _clamp_int(_get(cfg, "history_turns"), default=8, lo=0, hi=50)
        auto_title = _parse_bool(_get(cfg, "auto_title_enabled"), default=True)
        persona = str(_get(cfg, "persona_id") or "").strip()
        default_quota = _clamp_int(
            _get(cfg, "default_daily_quota"), default=200, lo=1, hi=1_000_000
        )
        ip_max = _clamp_int(
            _get(cfg, "ip_brute_force_max_fails"), default=10, lo=0, hi=10_000
        )
        ip_block = _clamp_int(
            _get(cfg, "ip_brute_force_block_seconds"),
            default=900,
            lo=1,
            hi=86_400 * 30,
        )
        trust_xff = _parse_bool(_get(cfg, "trust_forwarded_for"), default=False)
        trust_ref = _parse_bool(_get(cfg, "trust_referer_as_origin"), default=False)
        allow_missing = _parse_bool(_get(cfg, "allow_missing_origin"), default=False)
        admin_key = str(_get(cfg, "master_admin_key") or "").strip()
        llm_timeout = _clamp_int(
            _get(cfg, "llm_timeout_seconds"), default=60, lo=5, hi=600
        )
        site_name = str(_get(cfg, "site_name") or "").strip()
        welcome_message = str(_get(cfg, "welcome_message") or "").strip()
        show_github_link = _parse_bool(_get(cfg, "show_github_link"), default=True)
        privacy_url = str(_get(cfg, "privacy_url") or "").strip()
        theme_family = str(_get(cfg, "theme_family") or "classic").strip().lower()
        if theme_family not in ("notebook", "classic"):
            theme_family = "classic"

        raw_storage = _get(cfg, "storage", {}) or {}
        driver = str(_get(raw_storage, "driver", "sqlite")).strip().lower() or "sqlite"
        if driver not in {"sqlite", "mysql"}:
            logger.warning(
                "[WebChatGateway] unknown storage.driver=%s, fallback to sqlite",
                driver,
            )
            driver = "sqlite"
        sqlite_path = str(
            _get(raw_storage, "sqlite_path") or "data/webchat_gateway.db"
        ).strip()
        mysql_dsn = str(_get(raw_storage, "mysql_dsn") or "").strip()

        view = cls(
            host=host,
            port=port,
            endpoint_prefix=prefix,
            admin_ui_path=admin_ui_path,
            allowed_origins=origins,
            max_message_length=max_msg,
            history_turns=history,
            auto_title_enabled=auto_title,
            persona_id=persona,
            default_daily_quota=default_quota,
            ip_brute_force_max_fails=ip_max,
            ip_brute_force_block_seconds=ip_block,
            trust_forwarded_for=trust_xff,
            trust_referer_as_origin=trust_ref,
            allow_missing_origin=allow_missing,
            master_admin_key=admin_key,
            llm_timeout_seconds=llm_timeout,
            site_name=site_name,
            welcome_message=welcome_message,
            show_github_link=show_github_link,
            privacy_url=privacy_url,
            theme_family=theme_family,
            storage=StorageConfig(
                driver=driver,
                sqlite_path=sqlite_path,
                mysql_dsn=mysql_dsn,
            ),
        )
        view._emit_warnings()
        return view

    def _emit_warnings(self) -> None:
        if not self.master_admin_key:
            logger.warning(
                "[WebChatGateway] master_admin_key is empty; admin endpoints disabled"
            )
        elif len(self.master_admin_key) < 24:
            logger.warning(
                "[WebChatGateway] master_admin_key is shorter than 24 chars; consider regenerating"
            )
        if self.storage.driver == "mysql" and not self.storage.mysql_dsn:
            logger.error(
                "[WebChatGateway] storage.driver=mysql but mysql_dsn is empty; server will not start"
            )
        if "*" in self.allowed_origins:
            logger.warning(
                "[WebChatGateway] allowed_origins='*'; restrict to your friends' frontend in production"
            )
        if self.trust_forwarded_for:
            logger.warning(
                "[WebChatGateway] trust_forwarded_for=true; ensure AstrBot is behind a trusted reverse proxy"
            )
        if self.trust_referer_as_origin:
            logger.warning(
                "[WebChatGateway] trust_referer_as_origin=true; weakens Origin allow-list as a CSRF mitigation"
            )
        if self.allow_missing_origin:
            logger.warning(
                "[WebChatGateway] allow_missing_origin=true; non-browser callers (curl/scripts) bypass the Origin allow-list"
            )

    def is_storage_ready(self) -> bool:
        if self.storage.driver == "mysql":
            return bool(self.storage.mysql_dsn)
        return bool(self.storage.sqlite_path)
