"""Plugin configuration view: parse, clamp, and validate raw config dict."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.star import StarTools

# Pinned to the plugin's package directory name so StarTools.get_data_dir
# resolves to data/plugin_data/astrbot_plugin_webchat_gateway/ regardless of
# call-stack heuristics. Hard-coding the name is intentional: the inferred
# name varies depending on where get_data_dir is called from, and we want a
# stable location for the DB and uploads tree across the codebase.
_PLUGIN_NAME = "astrbot_plugin_webchat_gateway"


def _default_data_dir() -> Path:
    """AstrBot-managed data dir for this plugin.

    StarTools.get_data_dir creates the directory if missing and returns
    an absolute Path under AstrBot's `data/plugin_data/{name}/`. Using
    this for default storage paths means plugin data follows the normal
    AstrBot data-volume lifecycle (backup, restore, reinstall-safe)
    instead of leaking into the AstrBot working-directory root.
    """
    return StarTools.get_data_dir(_PLUGIN_NAME)


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
    mysql_pool_max: int


@dataclass(frozen=True)
class StreamingConfig:
    """Streaming buffer/registry tunables.

    `redis_dsn` empty string = use the in-memory buffer (default).
    Non-empty must parse as `redis://` or `rediss://` (validated in `from_raw`).
    """

    redis_dsn: str
    grace_seconds: int
    max_per_token: int
    max_global: int


_DEFAULT_ALLOWED_MIME: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
)


@dataclass(frozen=True)
class UploadsConfig:
    """Image upload feature configuration.

    Storage backend toggle (`storage_driver`) selects LocalFileStore or
    R2FileStore. `enabled=False` keeps the routes installed but rejects
    uploads at the handler entry (so the frontend can downgrade UX).

    All sizes are MB so the operator-facing surface stays human-readable;
    converted to bytes at the handler call sites.
    """

    enabled: bool
    storage_driver: Literal["local", "r2"]
    local_path: str
    max_file_size_mb: int
    per_token_storage_mb: int
    max_attachments_per_message: int
    allowed_mime: tuple[str, ...]
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_endpoint: str
    r2_serving_mode: Literal["proxy", "direct"]
    r2_direct_link_ttl_seconds: int
    r2_cache_size_mb: int


@dataclass(frozen=True)
class ImageGenConfig:
    """OpenAI-compatible image generation (POST /v1/images/generations).

    `enabled=True` is the operator's intent; the ImageBridge has its
    own "effectively enabled" check that also requires both
    `endpoint` and `api_key` to be non-empty, so a half-filled config
    can never wedge a /image request into a 500.
    """

    enabled: bool
    endpoint: str
    api_key: str
    model: str
    size: str
    timeout_seconds: int


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
    chat_provider_id: str
    default_daily_quota: int
    audit_retention_days: int
    ip_brute_force_max_fails: int
    ip_brute_force_block_seconds: int
    trust_forwarded_for: bool
    trust_referer_as_origin: bool
    allow_missing_origin: bool
    master_admin_key: str
    llm_timeout_seconds: int
    llm_stream_total_timeout_seconds: int
    site_name: str
    welcome_message: str
    show_github_link: bool
    privacy_url: str
    site_icon_url: str
    theme_family: str
    storage: StorageConfig
    streaming: StreamingConfig
    uploads: UploadsConfig
    image_gen: ImageGenConfig

    @property
    def chat_path(self) -> str:
        return f"{self.endpoint_prefix}/chat"

    @property
    def chat_stream_path(self) -> str:
        return f"{self.endpoint_prefix}/chat/stream"

    @property
    def chat_stream_resume_path(self) -> str:
        return f"{self.endpoint_prefix}/chat/stream/{{stream_id}}/resume"

    @property
    def chat_stream_cancel_path(self) -> str:
        return f"{self.endpoint_prefix}/chat/stream/{{stream_id}}/cancel"

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
    def admin_tokens_regenerate_path(self) -> str:
        return f"{self.admin_tokens_item_path}/regenerate"

    @property
    def admin_stats_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/stats"

    @property
    def admin_audit_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/audit"

    @property
    def admin_settings_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/settings"

    @property
    def admin_restart_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/restart"

    @property
    def admin_logs_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/logs"

    @property
    def admin_logs_stream_path(self) -> str:
        return f"{self.endpoint_prefix}/admin/logs/stream"

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

    @property
    def conversations_path(self) -> str:
        return f"{self.endpoint_prefix}/conversations"

    @property
    def conversations_item_path(self) -> str:
        return f"{self.endpoint_prefix}/conversations/{{session_id}}"

    @property
    def conversations_clear_path(self) -> str:
        return f"{self.conversations_item_path}/clear"

    @property
    def conversations_message_path(self) -> str:
        """DELETE-able per-message URL — 0-based index into the rendered
        history surfaced by GET /conversations/{session_id}. The
        message-delete endpoint splices the matching CM entry out and
        emits a `message_deleted` event."""
        return f"{self.conversations_item_path}/messages/{{message_index}}"

    @property
    def conversations_regenerate_path(self) -> str:
        """POST endpoint that drops the assistant message at
        `body.message_index` and re-runs the non-streaming LLM call on
        the truncated history."""
        return f"{self.conversations_item_path}/regenerate"

    @property
    def events_path(self) -> str:
        return f"{self.endpoint_prefix}/events"

    @property
    def upload_path(self) -> str:
        return f"{self.endpoint_prefix}/upload"

    @property
    def files_serve_path(self) -> str:
        return f"{self.endpoint_prefix}/files/{{file_id}}"

    @property
    def files_cookie_path(self) -> str:
        """Path attribute for the file-auth cookie's Set-Cookie header.

        Must scope to the /files-prefix so the cookie is sent on serve
        requests but NOT on /chat or /admin (least-privilege). Builds
        off `endpoint_prefix` so a custom prefix doesn't silently break
        cookie delivery — the browser scopes by exact path prefix.
        """
        return f"{self.endpoint_prefix}/files"

    @property
    def logout_path(self) -> str:
        """POST endpoint that clears + server-side invalidates the file-
        auth cookie. Scoped UNDER the cookie's `Path` attribute (which
        is `{prefix}/files`) so the browser auto-sends the cookie on
        the logout request — without that, `navigator.sendBeacon` (the
        page-unload-safe channel the frontend uses for logout) cannot
        attach the cookie and the handler has no way to identify which
        token's cookies to invalidate server-side.
        """
        return f"{self.endpoint_prefix}/files/logout"

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
        chat_provider_id = str(_get(cfg, "chat_provider_id") or "").strip()
        default_quota = _clamp_int(
            _get(cfg, "default_daily_quota"), default=200, lo=1, hi=1_000_000
        )
        audit_retention_days = _clamp_int(
            _get(cfg, "audit_retention_days"), default=7, lo=1, hi=3650
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
        # Hard floor enforced here so the rest of the codebase never
        # sees a too-short key. constant_time_eq + IP guard cover the
        # online attack but a sub-24-char key is offline-crackable in
        # hours if logs ever leak (the threshold tightened from 16 to
        # 24 in v0.3.2 — see CHANGELOG BREAKING note). Clear the key
        # so admin endpoints behave exactly as if it were unset; the
        # error is logged once at parse time.
        if admin_key and len(admin_key) < 24:
            logger.error(
                "[WebChatGateway] master_admin_key MUST be >= 24 chars; "
                "current length=%d. Admin endpoints DISABLED until rotated. "
                "Generate a fresh one with: "
                "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`",
                len(admin_key),
            )
            admin_key = ""
        llm_timeout = _clamp_int(
            _get(cfg, "llm_timeout_seconds"), default=60, lo=5, hi=600
        )
        # Total stream timeout: 0 means "use the LlmBridge default
        # (8× per-chunk, capped at 600s)"; negative disables. Clamp
        # the positive range to 0..7200 so config typos can't strand
        # a stream for hours.
        raw_total = _get(cfg, "llm_stream_total_timeout_seconds", 0)
        try:
            llm_stream_total_timeout = int(raw_total)
        except (TypeError, ValueError):
            llm_stream_total_timeout = 0
        if llm_stream_total_timeout > 7200:
            llm_stream_total_timeout = 7200
        site_name = str(_get(cfg, "site_name") or "").strip()
        welcome_message = str(_get(cfg, "welcome_message") or "").strip()
        show_github_link = _parse_bool(_get(cfg, "show_github_link"), default=True)
        privacy_url = str(_get(cfg, "privacy_url") or "").strip()
        site_icon_url = str(_get(cfg, "site_icon_url") or "").strip()
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
        sqlite_path_raw = str(_get(raw_storage, "sqlite_path") or "").strip()
        sqlite_path = sqlite_path_raw or str(
            _default_data_dir() / "webchat_gateway.db"
        )
        mysql_dsn = str(_get(raw_storage, "mysql_dsn") or "").strip()
        # MySQL connection pool cap. Default 5 fits friends-list scale;
        # larger gateways should raise to 20-50.
        try:
            mysql_pool_max = int(_get(raw_storage, "mysql_pool_max") or 5)
        except (TypeError, ValueError):
            mysql_pool_max = 5
        mysql_pool_max = max(1, min(mysql_pool_max, 100))

        raw_streaming = _get(cfg, "streaming", {}) or {}
        redis_dsn_raw = str(_get(raw_streaming, "redis_dsn") or "").strip()
        if redis_dsn_raw:
            try:
                parsed_dsn = urlparse(redis_dsn_raw)
            except Exception:
                parsed_dsn = None
            if (
                parsed_dsn is None
                or parsed_dsn.scheme not in ("redis", "rediss")
                or not parsed_dsn.netloc
            ):
                logger.warning(
                    "[WebChatGateway] streaming.redis_dsn=%r is not a valid"
                    " redis://|rediss:// URL; falling back to in-memory buffer",
                    redis_dsn_raw,
                )
                redis_dsn_raw = ""
        grace_seconds = _clamp_int(
            _get(raw_streaming, "grace_seconds"), default=30, lo=5, hi=300
        )
        max_per_token = _clamp_int(
            _get(raw_streaming, "max_per_token"), default=3, lo=1, hi=10
        )
        max_global = _clamp_int(
            _get(raw_streaming, "max_global"), default=200, lo=10, hi=10_000
        )

        raw_uploads = _get(cfg, "uploads", {}) or {}
        uploads_enabled = _parse_bool(_get(raw_uploads, "enabled"), default=True)
        uploads_driver = (
            str(_get(raw_uploads, "storage_driver", "local")).strip().lower()
            or "local"
        )
        if uploads_driver not in {"local", "r2"}:
            logger.warning(
                "[WebChatGateway] unknown uploads.storage_driver=%s, fallback to local",
                uploads_driver,
            )
            uploads_driver = "local"
        local_path_raw = str(_get(raw_uploads, "local_path") or "").strip()
        local_path = local_path_raw or str(
            _default_data_dir() / "webchat_uploads"
        )
        max_file_size_mb = _clamp_int(
            _get(raw_uploads, "max_file_size_mb"), default=20, lo=1, hi=200
        )
        per_token_storage_mb = _clamp_int(
            _get(raw_uploads, "per_token_storage_mb"),
            default=500,
            lo=1,
            hi=1_000_000,
        )
        max_attachments_per_message = _clamp_int(
            _get(raw_uploads, "max_attachments_per_message"),
            default=4,
            lo=1,
            hi=16,
        )
        raw_allowed_mime = _get(raw_uploads, "allowed_mime")
        if raw_allowed_mime is None:
            allowed_mime: tuple[str, ...] = _DEFAULT_ALLOWED_MIME
        elif isinstance(raw_allowed_mime, str):
            parts = tuple(
                p.strip() for p in raw_allowed_mime.split(",") if p.strip()
            )
            allowed_mime = parts or _DEFAULT_ALLOWED_MIME
        elif isinstance(raw_allowed_mime, (list, tuple, set)):
            parts = tuple(str(p).strip() for p in raw_allowed_mime if str(p).strip())
            allowed_mime = parts or _DEFAULT_ALLOWED_MIME
        else:
            allowed_mime = _DEFAULT_ALLOWED_MIME

        raw_r2 = _get(raw_uploads, "r2", {}) or {}
        r2_account_id = str(_get(raw_r2, "account_id") or "").strip()
        r2_access_key_id = str(_get(raw_r2, "access_key_id") or "").strip()
        r2_secret_access_key = str(_get(raw_r2, "secret_access_key") or "").strip()
        r2_bucket = str(_get(raw_r2, "bucket") or "").strip()
        r2_endpoint = str(_get(raw_r2, "endpoint") or "").strip()
        r2_serving_mode = (
            str(_get(raw_r2, "serving_mode", "proxy")).strip().lower() or "proxy"
        )
        if r2_serving_mode not in {"proxy", "direct"}:
            logger.warning(
                "[WebChatGateway] unknown uploads.r2.serving_mode=%s, fallback to proxy",
                r2_serving_mode,
            )
            r2_serving_mode = "proxy"
        r2_direct_link_ttl_seconds = _clamp_int(
            _get(raw_r2, "direct_link_ttl_seconds"),
            default=300,
            lo=30,
            hi=3600,
        )
        r2_cache_size_mb = _clamp_int(
            _get(raw_r2, "cache_size_mb"), default=200, lo=10, hi=100_000
        )

        raw_image_gen = _get(cfg, "image_gen", {}) or {}
        image_gen_enabled = _parse_bool(
            _get(raw_image_gen, "enabled"), default=False
        )
        image_gen_endpoint = str(
            _get(raw_image_gen, "endpoint") or "https://api.openai.com/v1"
        ).strip()
        image_gen_endpoint = image_gen_endpoint.rstrip("/")
        image_gen_api_key = str(_get(raw_image_gen, "api_key") or "").strip()
        image_gen_model = (
            str(_get(raw_image_gen, "model") or "dall-e-3").strip()
            or "dall-e-3"
        )
        image_gen_size = (
            str(_get(raw_image_gen, "size") or "1024x1024").strip()
            or "1024x1024"
        )
        image_gen_timeout = _clamp_int(
            _get(raw_image_gen, "timeout_seconds"),
            default=180,
            lo=5,
            hi=1800,
        )
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
            chat_provider_id=chat_provider_id,
            default_daily_quota=default_quota,
            audit_retention_days=audit_retention_days,
            ip_brute_force_max_fails=ip_max,
            ip_brute_force_block_seconds=ip_block,
            trust_forwarded_for=trust_xff,
            trust_referer_as_origin=trust_ref,
            allow_missing_origin=allow_missing,
            master_admin_key=admin_key,
            llm_timeout_seconds=llm_timeout,
            llm_stream_total_timeout_seconds=llm_stream_total_timeout,
            site_name=site_name,
            welcome_message=welcome_message,
            show_github_link=show_github_link,
            privacy_url=privacy_url,
            site_icon_url=site_icon_url,
            theme_family=theme_family,
            storage=StorageConfig(
                driver=driver,
                sqlite_path=sqlite_path,
                mysql_dsn=mysql_dsn,
                mysql_pool_max=mysql_pool_max,
            ),
            streaming=StreamingConfig(
                redis_dsn=redis_dsn_raw,
                grace_seconds=grace_seconds,
                max_per_token=max_per_token,
                max_global=max_global,
            ),
            uploads=UploadsConfig(
                enabled=uploads_enabled,
                storage_driver=uploads_driver,
                local_path=local_path,
                max_file_size_mb=max_file_size_mb,
                per_token_storage_mb=per_token_storage_mb,
                max_attachments_per_message=max_attachments_per_message,
                allowed_mime=allowed_mime,
                r2_account_id=r2_account_id,
                r2_access_key_id=r2_access_key_id,
                r2_secret_access_key=r2_secret_access_key,
                r2_bucket=r2_bucket,
                r2_endpoint=r2_endpoint,
                r2_serving_mode=r2_serving_mode,
                r2_direct_link_ttl_seconds=r2_direct_link_ttl_seconds,
                r2_cache_size_mb=r2_cache_size_mb,
            ),
            image_gen=ImageGenConfig(
                enabled=image_gen_enabled,
                endpoint=image_gen_endpoint,
                api_key=image_gen_api_key,
                model=image_gen_model,
                size=image_gen_size,
                timeout_seconds=image_gen_timeout,
            ),
        )
        view._emit_warnings()
        return view

    def _emit_warnings(self) -> None:
        if not self.master_admin_key:
            logger.warning(
                "[WebChatGateway] master_admin_key is empty; admin endpoints disabled"
            )
        elif len(self.master_admin_key) < 32:
            logger.warning(
                "[WebChatGateway] master_admin_key is shorter than 32 chars; consider regenerating with `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`"
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
