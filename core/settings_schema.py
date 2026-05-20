"""Declarative whitelist of plugin config fields editable via /admin/settings.

The HTTP layer (``handlers/admin_settings``) consults this module to
validate PATCH payloads and project GET responses. Out-of-whitelist
fields stay editable through the AstrBot plugin sidebar only.

Two flavors of refusal collapse to the same wire code so the admin UI
doesn't need branching logic AND so the blacklist isn't leaked: both
"never heard of this key" and "known but sensitive" return 400
``unknown_field``.

Every `SettingField` carries a human-readable Chinese ``label`` so the
admin UI shows "站点名称" instead of "site_name". Boolean ``secret``
marks credentials (R2 keys, etc.) so the frontend can render
``<input type="password">`` and not leave the value sitting in the DOM
as plain text. The actual value is still returned in the GET payload —
operators on this endpoint are already authenticated.
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
    label: str = ""  # Human-readable Chinese label rendered by the UI
    restart_required: bool = True
    min: int | None = None
    max: int | None = None
    options: tuple[str, ...] = ()
    hint: str = ""
    secret: bool = False  # Render as password input in the UI


# Whitelist. Sections in stable order; fields in spec-table order
# within each section. Hint strings mirror `_conf_schema.json` so the
# operator sees the same copy in admin UI as in the AstrBot sidebar.
FIELDS: tuple[SettingField, ...] = (
    # --- 站点信息 -----------------------------------------------------------
    SettingField(
        key="site_name",
        section="站点信息",
        type="string",
        label="站点名称",
        hint="留空则使用默认 WebChat Gateway。",
    ),
    SettingField(
        key="site_icon_url",
        section="站点信息",
        type="string",
        label="站点图标 / Favicon",
        hint=(
            "落地页、聊天页、管理页 favicon 与右上角图标。支持 http(s):// 完整 URL "
            "或 /path 形式的同源路径；留空则使用默认气泡 SVG。"
        ),
    ),
    SettingField(
        key="welcome_message",
        section="站点信息",
        type="string",
        label="落地页欢迎语",
        hint="可选；空则隐藏。建议 1-2 行简短文本，会显示在首页 hero 区。",
    ),
    SettingField(
        key="show_github_link",
        section="站点信息",
        type="bool",
        label="显示 GitHub 链接",
        hint="私密部署可关闭，避免暴露项目来源。",
    ),
    SettingField(
        key="privacy_url",
        section="站点信息",
        type="string",
        label="隐私声明链接",
        hint="落地页页脚显示；空则隐藏。支持 http(s):// 完整 URL 或 /path。",
    ),
    SettingField(
        key="theme_family",
        section="站点信息",
        type="options",
        label="视觉主题",
        options=("notebook", "classic"),
        hint="classic = 经典 GitHub 风（默认），notebook = 手写笔记本风。",
    ),
    # --- 对话行为 -----------------------------------------------------------
    SettingField(
        key="persona_id",
        section="对话行为",
        type="string",
        label="LLM 人格 ID",
        hint=(
            "WebChat 使用的人格。粘贴已在 AstrBot 中配置的人格 ID；留空则使用"
            "默认人格。修改后需重启服务才会读取新的人格定义。"
        ),
    ),
    SettingField(
        key="chat_provider_id",
        section="对话行为",
        type="string",
        label="对话模型 (Provider ID)",
        hint=(
            "WebChat 使用的对话模型。留空则跟随 AstrBot 全局当前对话模型；"
            "填入已在 AstrBot 中配置的 provider id 即可在本插件单独切换模型。"
        ),
    ),
    SettingField(
        key="chat_fallback_provider_id",
        section="对话行为",
        type="string",
        label="对话模型降级备选 (Fallback Provider ID)",
        hint=(
            "当主对话模型在 AstrBot 中被删除 / 禁用 / 改名后，自动降级使用此 "
            "provider；备选也不可用时再回退到全局默认。留空表示不配置中间一级。"
        ),
    ),
    SettingField(
        key="max_message_length",
        section="对话行为",
        type="int",
        label="单条消息最大字符数",
        min=16,
        max=200_000,
        hint="超过此长度返回 400 message_too_long，防止 prompt 炸弹。",
    ),
    SettingField(
        key="history_turns",
        section="对话行为",
        type="int",
        label="上下文保留轮数",
        min=0,
        max=50,
        hint="每次请求携带最近 N 轮历史，建议 4-12。",
    ),
    SettingField(
        key="auto_title_enabled",
        section="对话行为",
        type="bool",
        label="启用自动生成会话标题",
        hint=(
            "若关闭，POST /api/webchat/title 直接返回 503 title_disabled。"
            "前端会保持“新会话”标题，用户仍可手动重命名。"
        ),
    ),
    SettingField(
        key="llm_timeout_seconds",
        section="对话行为",
        type="int",
        label="LLM 调用超时(秒)",
        min=5,
        max=600,
        hint="范围 5-600；超时返回错误并记录 audit 事件 llm_timeout。建议 30-120。",
    ),
    SettingField(
        key="llm_stream_total_timeout_seconds",
        section="对话行为",
        type="int",
        label="流式总超时(秒)",
        min=0,
        max=7200,
        hint=(
            "流式回复的总 wall-clock 上限。0 表示使用默认"
            "（min(8 × LLM 调用超时, 600)），范围 0-7200。"
        ),
    ),
    SettingField(
        key="default_daily_quota",
        section="对话行为",
        type="int",
        label="默认每日配额",
        min=1,
        max=1_000_000,
        hint="签发新 Token 时默认每日配额。范围 1-1000000。",
    ),
    SettingField(
        key="audit_retention_days",
        section="对话行为",
        type="int",
        label="审计日志保留天数",
        restart_required=False,
        min=1,
        max=3650,
        hint=(
            "超过该天数的审计日志会被后台清理任务删除。"
            "范围 1-3650，默认 7 天。修改后下一次清理迭代生效，无需重启。"
        ),
    ),
    # --- 安全 ---------------------------------------------------------------
    SettingField(
        key="allowed_origins",
        section="安全",
        type="csv",
        label="允许的网页来源(CORS)",
        hint="* 允许任意；或逗号分隔列表，如 http://localhost:1234,https://chat.example.com",
    ),
    SettingField(
        key="ip_brute_force_max_fails",
        section="安全",
        type="int",
        label="同 IP 失败封禁阈值",
        min=0,
        max=10_000,
        hint="防爆破阈值，0 表示禁用。",
    ),
    SettingField(
        key="ip_brute_force_block_seconds",
        section="安全",
        type="int",
        label="封禁时长(秒)",
        min=1,
        max=2_592_000,
        hint="触发封禁阈值后封禁该 IP 的持续时长，单位秒（默认 900 = 15 分钟）。",
    ),
    SettingField(
        key="trust_forwarded_for",
        section="安全",
        type="bool",
        label="信任 X-Forwarded-For",
        hint=(
            "仅当 AstrBot 部署在可信反向代理（Nginx/Caddy/Cloudflare）之后启用，"
            "否则攻击者可伪造 IP 绕过防爆破。"
        ),
    ),
    SettingField(
        key="trust_referer_as_origin",
        section="安全",
        type="bool",
        label="缺 Origin 时回退 Referer",
        hint=(
            "默认关闭。开启后会削弱 Origin 白名单作为 CSRF 防护的强度"
            "（隐私模式或 no-referrer 策略下可绕过），仅在你了解风险并需要"
            "兼容老式客户端时启用。"
        ),
    ),
    SettingField(
        key="allow_missing_origin",
        section="安全",
        type="bool",
        label="允许缺 Origin / Referer",
        hint=(
            "若为 false（默认），未携带 Origin / Referer 头的请求被视为违规，"
            "防止 curl/服务端脚本绕过 allow_origins。"
            "仅在你明确需要让非浏览器客户端访问 API 时打开。"
        ),
    ),
    # --- 流式缓冲 -----------------------------------------------------------
    SettingField(
        key="streaming.redis_dsn",
        section="流式缓冲",
        type="string",
        label="Redis 连接串(可选)",
        hint=(
            "格式 redis://:password@host:port/db 或 rediss://...（TLS）；留空使用"
            "内存缓冲。AstrBot 单进程部署留空即可，多实例或希望跨插件重启保留"
            "in-flight 缓冲再配置。"
        ),
    ),
    SettingField(
        key="streaming.grace_seconds",
        section="流式缓冲",
        type="int",
        label="流终态缓冲秒数",
        min=5,
        max=300,
        hint=(
            "流到达终态后，缓冲保留这么久再清理，用于覆盖客户端 done 帧丢包"
            "后的迟到 reconnect。默认 30，范围 5-300。"
        ),
    ),
    SettingField(
        key="streaming.max_per_token",
        section="流式缓冲",
        type="int",
        label="单 Token 缓冲上限",
        min=1,
        max=10,
        hint=(
            "每个 token 同时存活的缓冲条目（含已结束 grace 期内）不超过此数。"
            "默认 3，范围 1-10。"
        ),
    ),
    SettingField(
        key="streaming.max_global",
        section="流式缓冲",
        type="int",
        label="全局缓冲上限",
        min=10,
        max=10_000,
        hint=(
            "全平台同时存活的缓冲条目上限。超过会优先驱逐最旧的已关闭条目；"
            "若全部仍活跃则拒绝新流。默认 200，范围 10-10000。"
        ),
    ),
    # --- 图片上传 -----------------------------------------------------------
    SettingField(
        key="uploads.enabled",
        section="图片上传",
        type="bool",
        label="启用图片上传",
        hint="关闭后上传接口直接拒绝，前端附件按钮也会变灰。",
    ),
    SettingField(
        key="uploads.storage_driver",
        section="图片上传",
        type="options",
        label="存储驱动",
        options=("local", "r2"),
        hint=(
            "local 写入本地磁盘（默认，零配置）；r2 写入 Cloudflare R2，"
            "需要安装 aiobotocore 并填写下面的 R2 凭证。切换后重启即可。"
        ),
    ),
    SettingField(
        key="uploads.local_path",
        section="图片上传",
        type="string",
        label="本地存储根目录",
        hint=(
            "留空则使用 AstrBot 标准插件数据目录 data/plugin_data/"
            "astrbot_plugin_webchat_gateway/webchat_uploads；填写绝对路径或"
            "以 AstrBot 工作目录为基准的相对路径可自定义位置。"
        ),
    ),
    SettingField(
        key="uploads.max_file_size_mb",
        section="图片上传",
        type="int",
        label="单文件最大 MB",
        min=1,
        max=200,
        hint="超过会返回 413 payload_too_large。建议 5-50，默认 20。",
    ),
    SettingField(
        key="uploads.per_token_storage_mb",
        section="图片上传",
        type="int",
        label="单 Token 累计存储 MB",
        min=1,
        max=1_000_000,
        hint=(
            "已提交文件总大小达到该阈值后，新上传返回 429 "
            "storage_quota_exceeded。未提交临时文件不计入。"
        ),
    ),
    SettingField(
        key="uploads.max_attachments_per_message",
        section="图片上传",
        type="int",
        label="单条消息最多附件数",
        min=1,
        max=16,
        hint="超过会返回 400 too_many_attachments。建议 1-8，默认 4 与主流 LLM 对齐。",
    ),
    SettingField(
        key="uploads.allowed_mime",
        section="图片上传",
        type="csv",
        label="允许的 MIME 类型",
        hint=(
            "默认 image/jpeg,image/png,image/webp,image/gif。"
            "不要包含 image/svg+xml（XSS）或 image/avif（解码器兼容性差）。"
        ),
    ),
    SettingField(
        key="uploads.r2.serving_mode",
        section="图片上传",
        type="options",
        label="R2 下发模式",
        options=("proxy", "direct"),
        hint=(
            "proxy（默认）= 走插件代理读 R2；direct = 302 跳转到 R2 预签名 URL，"
            "节省带宽但走 Cloudflare 边缘。"
        ),
    ),
    SettingField(
        key="uploads.r2.direct_link_ttl_seconds",
        section="图片上传",
        type="int",
        label="R2 direct 链接有效期(秒)",
        min=30,
        max=3600,
        hint="仅当 serving_mode=direct 时生效。30-3600，默认 300（5 分钟）。",
    ),
    SettingField(
        key="uploads.r2.cache_size_mb",
        section="图片上传",
        type="int",
        label="R2 本地缓存 MB",
        min=10,
        max=100_000,
        hint=(
            "LLM 调用需要本地路径时会先把 R2 对象 fetch 到 AstrBot 临时目录。"
            "LRU 淘汰，默认 200。"
        ),
    ),
    SettingField(
        key="uploads.r2.account_id",
        section="图片上传",
        type="string",
        label="R2 账户 ID",
        hint="Cloudflare dashboard → R2 → Overview 顶部可见。仅当 storage_driver=r2 必填。",
    ),
    SettingField(
        key="uploads.r2.bucket",
        section="图片上传",
        type="string",
        label="R2 存储桶名称",
        hint="建议为本插件单独建一个 bucket，与其他业务隔离。",
    ),
    SettingField(
        key="uploads.r2.endpoint",
        section="图片上传",
        type="string",
        label="R2 端点 URL",
        hint="形如 https://<account_id>.r2.cloudflarestorage.com，可在 bucket settings 查看。",
    ),
    SettingField(
        key="uploads.r2.access_key_id",
        section="图片上传",
        type="string",
        label="R2 访问密钥 ID",
        secret=True,
        hint="R2 → Manage R2 API Tokens 创建后获取。仅授予该 bucket 的 Read & Write 权限。",
    ),
    SettingField(
        key="uploads.r2.secret_access_key",
        section="图片上传",
        type="string",
        label="R2 访问密钥 Secret",
        secret=True,
        hint="创建 token 时一次性显示，请妥善保存。请勿提交到代码仓库。",
    ),
    # --- 生图 ---------------------------------------------------------------
    # All image_gen.* fields hot-reload: main.py's `_reload_cfg`
    # rebuilds the ImageBridge from the live ConfigView and swaps it
    # into ChatDeps, so an operator who saves a fresh API key sees the
    # /image button stop returning ``image_disabled`` immediately —
    # no restart round-trip required.
    SettingField(
        key="image_gen.enabled",
        section="生图",
        type="bool",
        label="启用生图",
        restart_required=False,
        hint=(
            "关闭后聊天里的 /image 命令直接返回 image_disabled，"
            "composer 生图按钮变灰。"
        ),
    ),
    SettingField(
        key="image_gen.endpoint",
        section="生图",
        type="string",
        label="兼容 OpenAI 的 base URL",
        restart_required=False,
        hint=(
            "形如 https://api.openai.com/v1，或自建兼容网关。"
            "POST {endpoint}/images/generations。"
        ),
    ),
    SettingField(
        key="image_gen.api_key",
        section="生图",
        type="string",
        label="API 密钥",
        secret=True,
        restart_required=False,
        hint="Bearer token；留空视为未启用，请勿提交到代码仓库。",
    ),
    SettingField(
        key="image_gen.model",
        section="生图",
        type="string",
        label="生图模型",
        restart_required=False,
        hint=(
            "dall-e-3 / gpt-image-1 / gpt-image-2 / 或网关支持的其它模型。"
            "检测到 gpt-image-* 系列时会自动跳过 response_format 参数 "
            "(该系列不支持该字段，会报 400)。"
        ),
    ),
    SettingField(
        key="image_gen.size",
        section="生图",
        type="string",
        label="图片尺寸",
        restart_required=False,
        hint="DALL-E 3 支持 1024x1024 / 1024x1792 / 1792x1024；其它模型按各家文档。",
    ),
    SettingField(
        key="image_gen.timeout_seconds",
        section="生图",
        type="int",
        label="请求总超时(秒)",
        restart_required=False,
        min=5,
        max=1800,
        hint=(
            "默认 180 秒。生图通常 30-180 秒，gpt-image-1 / 高画质场景"
            "可能 180-300 秒，自建中转网关可能更长。范围 5-1800。"
        ),
    ),
)


# Known but refused. Distinguishing this from "truly unknown" is useful
# internally (debugging) but we deliberately collapse to the same
# response code so the wire doesn't disclose which keys exist.
#
# Shrunk to bare-minimum boot-time essentials:
#   * host/port — network binding, changing them would invalidate the
#     very endpoint accepting the change.
#   * endpoint_prefix / admin_ui_path — URL routing for the admin
#     panel itself; circular dependency if changed via the panel.
#   * master_admin_key — auth credential for the panel itself; changing
#     it via the panel would lock the current session out and is best
#     done via the AstrBot sidebar with the legacy key visible.
#   * storage.* — schema migration risks (switching driver, moving
#     sqlite_path) need an offline tool, not a live config swap.
BLACKLIST: frozenset[str] = frozenset(
    {
        "host",
        "port",
        "endpoint_prefix",
        "admin_ui_path",
        "master_admin_key",
        "storage.driver",
        "storage.sqlite_path",
        "storage.mysql_dsn",
        "storage.mysql_pool_max",
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
