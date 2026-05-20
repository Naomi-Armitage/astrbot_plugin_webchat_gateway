"""Declarative whitelist of plugin config fields editable via /admin/settings.

The HTTP layer (``handlers/admin_settings``) consults this module to
validate PATCH payloads and project GET responses. Out-of-whitelist
fields stay editable through the AstrBot plugin sidebar only.

Two flavors of refusal collapse to the same wire code so the admin UI
doesn't need branching logic AND so the blacklist isn't leaked: both
"never heard of this key" and "known but sensitive" return 400
``unknown_field``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SettingsError(Exception):
    """Validation failure surfacing a stable code + HTTP status."""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class SettingField:
    key: str
    section: str
    type: str  # "string" | "int" | "bool" | "options" | "csv"
    restart_required: bool = True
    min: int | None = None
    max: int | None = None
    options: tuple[str, ...] = ()
    hint: str = ""


# Whitelist. Sections in stable order; fields in spec-table order
# within each section. Hint strings mirror `_conf_schema.json` so the
# operator sees the same copy in admin UI as in the AstrBot sidebar.
FIELDS: tuple[SettingField, ...] = (
    # --- Branding -----------------------------------------------------------
    SettingField(
        key="site_name",
        section="Branding",
        type="string",
        hint="留空则使用默认 WebChat Gateway。",
    ),
    SettingField(
        key="welcome_message",
        section="Branding",
        type="string",
        hint="可选；空则隐藏。建议 1-2 行简短文本，会显示在首页 hero 区。",
    ),
    SettingField(
        key="show_github_link",
        section="Branding",
        type="bool",
        hint="私密部署可关闭，避免暴露项目来源。",
    ),
    SettingField(
        key="privacy_url",
        section="Branding",
        type="string",
        hint="落地页页脚显示；空则隐藏。",
    ),
    SettingField(
        key="theme_family",
        section="Branding",
        type="options",
        options=("notebook", "classic"),
        hint="classic = 经典 GitHub 风（默认），notebook = 手写笔记本风。",
    ),
    # --- Behaviour ----------------------------------------------------------
    SettingField(
        key="max_message_length",
        section="Behaviour",
        type="int",
        min=16,
        max=200_000,
        hint="超过此长度返回 400 message_too_long，防止 prompt 炸弹。",
    ),
    SettingField(
        key="history_turns",
        section="Behaviour",
        type="int",
        min=0,
        max=50,
        hint="每次请求携带最近 N 轮历史，建议 4-12。",
    ),
    SettingField(
        key="auto_title_enabled",
        section="Behaviour",
        type="bool",
        hint=(
            "若关闭，POST /api/webchat/title 直接返回 503 title_disabled。"
            "前端会保持自动生成的“新会话”标题，用户仍可手动重命名。"
        ),
    ),
    SettingField(
        key="llm_timeout_seconds",
        section="Behaviour",
        type="int",
        min=5,
        max=600,
        hint="范围 5-600；超时返回错误并记录 audit 事件 llm_timeout。建议 30-120。",
    ),
    SettingField(
        key="llm_stream_total_timeout_seconds",
        section="Behaviour",
        type="int",
        min=0,
        max=7200,
        hint=(
            "流式回复的总 wall-clock 上限。0 表示使用默认"
            "（min(8 × llm_timeout_seconds, 600)），负值禁用总超时；"
            "UI 夹紧到 0-7200。"
        ),
    ),
    SettingField(
        key="default_daily_quota",
        section="Behaviour",
        type="int",
        min=1,
        max=1_000_000,
        hint="签发新 Token 时默认每日配额。范围 1-1000000。",
    ),
    SettingField(
        key="audit_retention_days",
        section="Behaviour",
        type="int",
        restart_required=False,
        min=1,
        max=3650,
        hint=(
            "超过该天数的审计日志会被后台清理任务删除。"
            "范围 1-3650，默认 7 天。修改后下一次清理迭代生效，无需重启。"
        ),
    ),
    # --- Security -----------------------------------------------------------
    SettingField(
        key="allowed_origins",
        section="Security",
        type="csv",
        hint="* 允许任意；或逗号分隔列表，如 http://localhost:1234,https://chat.example.com",
    ),
    SettingField(
        key="ip_brute_force_max_fails",
        section="Security",
        type="int",
        min=0,
        max=10_000,
        hint="防爆破阈值，0 表示禁用。",
    ),
    SettingField(
        key="ip_brute_force_block_seconds",
        section="Security",
        type="int",
        min=1,
        max=2_592_000,
        hint="触发封禁阈值后封禁该 IP 的持续时长，单位秒（默认 900 = 15 分钟）。",
    ),
    SettingField(
        key="trust_forwarded_for",
        section="Security",
        type="bool",
        hint=(
            "仅当 AstrBot 部署在可信反向代理（Nginx/Caddy/Cloudflare）之后启用，"
            "否则攻击者可伪造 IP 绕过防爆破。"
        ),
    ),
    SettingField(
        key="trust_referer_as_origin",
        section="Security",
        type="bool",
        hint=(
            "默认关闭。开启后会削弱 Origin 白名单作为 CSRF 防护的强度"
            "（隐私模式或 no-referrer 策略下可绕过），仅在你了解风险并需要"
            "兼容老式客户端时启用。"
        ),
    ),
    SettingField(
        key="allow_missing_origin",
        section="Security",
        type="bool",
        hint=(
            "若为 false（默认），未携带 Origin / Referer 头的请求被视为违规，"
            "防止 curl/服务端脚本绕过 allow_origins。"
            "仅在你明确需要让非浏览器客户端访问 API 时打开。"
        ),
    ),
    # --- Streaming ----------------------------------------------------------
    SettingField(
        key="streaming.grace_seconds",
        section="Streaming",
        type="int",
        min=5,
        max=300,
        hint=(
            "流到达终态后，缓冲保留这么久再清理，用于覆盖客户端 done 帧丢包"
            "后的迟到 reconnect。默认 30，范围 5-300。"
        ),
    ),
    SettingField(
        key="streaming.max_per_token",
        section="Streaming",
        type="int",
        min=1,
        max=10,
        hint=(
            "每个 token 同时存活的缓冲条目（含已结束 grace 期内）不超过此数。"
            "默认 3，范围 1-10。"
        ),
    ),
    SettingField(
        key="streaming.max_global",
        section="Streaming",
        type="int",
        min=10,
        max=10_000,
        hint=(
            "全平台同时存活的缓冲条目上限。超过会优先驱逐最旧的已关闭条目；"
            "若全部仍活跃则拒绝新流。默认 200，范围 10-10000。"
        ),
    ),
    # --- Uploads ------------------------------------------------------------
    SettingField(
        key="uploads.enabled",
        section="Uploads",
        type="bool",
        hint="关闭后上传接口直接拒绝，前端附件按钮也会变灰。",
    ),
    SettingField(
        key="uploads.max_file_size_mb",
        section="Uploads",
        type="int",
        min=1,
        max=200,
        hint="超过会返回 413 payload_too_large。建议 5-50，默认 20。",
    ),
    SettingField(
        key="uploads.per_token_storage_mb",
        section="Uploads",
        type="int",
        min=1,
        max=1_000_000,
        hint=(
            "已提交（committed）文件总大小达到该阈值后，新上传返回 429 "
            "storage_quota_exceeded。未提交的临时文件不计入，由 GC 在 1 小时后清理。"
        ),
    ),
    SettingField(
        key="uploads.max_attachments_per_message",
        section="Uploads",
        type="int",
        min=1,
        max=16,
        hint="超过会返回 400 too_many_attachments。建议 1-8，默认 4 与主流 LLM 对齐。",
    ),
    SettingField(
        key="uploads.allowed_mime",
        section="Uploads",
        type="csv",
        hint=(
            "默认 image/jpeg,image/png,image/webp,image/gif。"
            "不要包含 image/svg+xml（XSS）或 image/avif（解码器兼容性差）。"
        ),
    ),
    SettingField(
        key="uploads.r2.serving_mode",
        section="Uploads",
        type="options",
        options=("proxy", "direct"),
        hint=(
            "proxy（默认）= 走插件代理读 R2，下行带宽算自己服务器，但对国内"
            "用户更快；direct = 302 跳转到 R2 预签名 URL，节省带宽但走 "
            "Cloudflare 边缘。"
        ),
    ),
    SettingField(
        key="uploads.r2.direct_link_ttl_seconds",
        section="Uploads",
        type="int",
        min=30,
        max=3600,
        hint="仅当 serving_mode=direct 时生效。30-3600，默认 300（5 分钟）。",
    ),
    SettingField(
        key="uploads.r2.cache_size_mb",
        section="Uploads",
        type="int",
        min=10,
        max=100_000,
        hint=(
            "LLM 调用需要本地路径时会先把 R2 对象 fetch 到 AstrBot 临时目录。"
            "LRU 淘汰，默认 200。"
        ),
    ),
)


# Known but refused. Distinguishing this from "truly unknown" is useful
# internally (debugging) but we deliberately collapse to the same
# response code so the wire doesn't disclose which keys exist.
BLACKLIST: frozenset[str] = frozenset(
    {
        "host",
        "port",
        "endpoint_prefix",
        "admin_ui_path",
        "master_admin_key",
        "persona_id",
        "storage.driver",
        "storage.sqlite_path",
        "storage.mysql_dsn",
        "storage.mysql_pool_max",
        "streaming.redis_dsn",
        "uploads.storage_driver",
        "uploads.local_path",
        "uploads.r2.account_id",
        "uploads.r2.access_key_id",
        "uploads.r2.secret_access_key",
        "uploads.r2.bucket",
        "uploads.r2.endpoint",
    }
)


_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


@dataclass
class _Index:
    by_key: dict[str, SettingField] = field(default_factory=dict)


def _build_index() -> _Index:
    idx = _Index()
    for f in FIELDS:
        idx.by_key[f.key] = f
    return idx


_INDEX = _build_index()


def field_for_key(key: str) -> SettingField | None:
    return _INDEX.by_key.get(key)


def _walk(config: Any, path: list[str]) -> tuple[Any, str]:
    """Return (container, leaf_key) for a dotted-path write.

    Walks ``config`` along ``path[:-1]``, creating intermediate dict
    nodes lazily so a write to ``streaming.grace_seconds`` succeeds
    even when the config dict had ``streaming`` omitted. Refuses to
    descend through non-mapping nodes (lists, scalars) — the schema's
    blacklist already excludes every list/scalar parent, so this only
    fires on truly malformed configs.
    """
    node = config
    for part in path[:-1]:
        current = _read_one(node, part)
        if current is None:
            new_child: dict[str, Any] = {}
            _write_one(node, part, new_child)
            node = new_child
        elif _is_mapping(current):
            node = current
        else:
            raise SettingsError("unknown_field", 400)
    return node, path[-1]


def _is_mapping(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    # AstrBotConfig is dict-like; rely on __getitem__/__setitem__/get
    # rather than the exact base class so the schema works against any
    # mapping that exposes the standard API.
    return hasattr(value, "__getitem__") and hasattr(value, "get")


def _read_one(container: Any, key: str) -> Any:
    if container is None:
        return None
    getter = getattr(container, "get", None)
    if callable(getter):
        return getter(key)
    try:
        return container[key]
    except (KeyError, TypeError, IndexError):
        return None


def _write_one(container: Any, key: str, value: Any) -> None:
    try:
        container[key] = value
    except TypeError as exc:
        raise SettingsError("unknown_field", 400) from exc


def read_value(config: Any, key: str) -> Any:
    """Dotted-path read against an AstrBotConfig-like mapping.

    Returns ``None`` if any intermediate node is missing. Callers
    needing a default should layer it on top — this function deliberately
    does not consult the field's default so the GET response surfaces
    the live config state, including unset keys.
    """
    parts = key.split(".")
    node: Any = config
    for part in parts:
        if node is None:
            return None
        node = _read_one(node, part)
    return node


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    raise SettingsError("invalid_type", 400)


def _coerce_int(raw: Any, *, lo: int, hi: int) -> int:
    if isinstance(raw, bool):
        # `True == 1` would otherwise sneak past int() — explicit reject
        # so a checkbox submitted to an int field is a clear 400 rather
        # than a confusing "value=1".
        raise SettingsError("invalid_type", 400)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError("invalid_type", 400) from exc
    if value < lo or value > hi:
        raise SettingsError("out_of_range", 400)
    return value


def _coerce_string(raw: Any) -> str:
    if isinstance(raw, bool):
        raise SettingsError("invalid_type", 400)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SettingsError("invalid_type", 400)
    return raw.strip()


def _coerce_options(raw: Any, *, options: tuple[str, ...]) -> str:
    if not isinstance(raw, str):
        raise SettingsError("invalid_type", 400)
    value = raw.strip()
    if value not in options:
        raise SettingsError("invalid_option", 400)
    return value


def _coerce_csv(raw: Any) -> str:
    """CSV fields are stored as a single comma-joined string.

    The two existing CSV fields (`allowed_origins`, `uploads.allowed_mime`)
    are both declared as ``type: string`` in `_conf_schema.json` and
    `ConfigView.from_raw` already splits on `,` when materialising the
    runtime view — so persisting the joined form on disk preserves the
    boot-time contract. Accepting a list/tuple on the wire is a UX nicety
    so the frontend can PATCH `["a", "b"]` directly.
    """
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            if isinstance(entry, bool) or not isinstance(entry, str):
                raise SettingsError("invalid_type", 400)
            items.append(entry.strip())
    else:
        raise SettingsError("invalid_type", 400)
    cleaned = [x for x in items if x]
    return ",".join(cleaned)


def _coerce_value(spec: SettingField, raw: Any) -> Any:
    if spec.type == "bool":
        return _coerce_bool(raw)
    if spec.type == "int":
        assert spec.min is not None and spec.max is not None
        return _coerce_int(raw, lo=spec.min, hi=spec.max)
    if spec.type == "string":
        return _coerce_string(raw)
    if spec.type == "options":
        return _coerce_options(raw, options=spec.options)
    if spec.type == "csv":
        return _coerce_csv(raw)
    raise SettingsError("invalid_type", 400)


def validate(key: str, raw_value: Any) -> tuple[SettingField, Any]:
    """Look up + coerce without writing. Used by the two-pass apply path."""
    spec = field_for_key(key)
    if spec is None:
        # Blacklist and "truly unknown" intentionally collapse so the wire
        # response doesn't disclose which keys exist. The PATCH handler
        # rejects the WHOLE batch on a single bad key, so a probe attempt
        # can't bisect for membership either.
        raise SettingsError("unknown_field", 400)
    return spec, _coerce_value(spec, raw_value)


def apply_update(config: Any, key: str, raw_value: Any) -> Any:
    """Validate + write a single field. Returns the canonical normalised value.

    Callers that batch updates should use ``validate`` for the
    "validate-everything-first" pass and then call this for the writes,
    so a payload with one bad key doesn't leave the config half-written.
    """
    spec, normalised = validate(key, raw_value)
    container, leaf = _walk(config, key.split("."))
    _write_one(container, leaf, normalised)
    return normalised
