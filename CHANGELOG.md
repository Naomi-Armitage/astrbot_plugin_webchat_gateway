# Changelog

记录本插件的可见变化。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

## v0.3.0 — 2026-05-11

### Added — 图片上传 / 多模态附件
- 新增 `POST {prefix}/upload` 多部分上传端点：单次最多 20 MB（默认）、按 token 总配额 500 MB（默认）、每条消息最多 4 张图。仅接受 `image/jpeg|png|webp|gif`，通过 Pillow `verify()` 抗解压炸弹（`MAX_IMAGE_PIXELS=50M`）
- 新增 `GET {prefix}/files/{file_id}` 私有图床端点：`<img src>` 渲染走 HMAC 签名的 HttpOnly cookie（Path 限 `{prefix}/files`，SameSite=Lax），bearer header 仍可用（CLI / 服务端）。Cookie 签名折入 `token_hash`，admin `regenerate_token` 立即作废所有旧 cookie
- 新增 `POST {prefix}/files/logout`：受 cookie 鉴权，登出时记录"该 token 此刻之前签发的所有 cookie 一律失效"，同时清空浏览器 cookie。前端 `sendBeacon` 优先、`fetch(keepalive)` 兜底
- 新增 FileStore 抽象 + LocalFileStore（默认）+ R2FileStore（可选，opt-in `aiobotocore>=2.13`）；R2 支持 proxy 模式（透传）和 direct 模式（302 → 预签名 URL，TTL 30-3600s）+ 200MB 本地 LRU 缓存
- 新增数据库表 `webchat_files`（schema v4→v5 自动迁移）追踪每个 file_id 的所有权、提交状态、存储 key。Orphan GC（committed=0，1 小时窗口）+ session cascade（committed=1 跟随 90 天软删 session 一起清掉）
- 前端 chat 页支持多图缩略图气泡（grid 1/2/3/4 layout）、灯箱浏览（Esc / 左右方向键 / 滚轮缩放）、composer 多附件 chip 条、Canvas 自动缩到 2048px 长边（GIF 跳过）

### Added — 配置
- 新增 `uploads.*` 配置组（enabled / storage_driver / local_path / max_file_size_mb / per_token_storage_mb / max_attachments_per_message / allowed_mime + 嵌套 `r2.{account_id, access_key_id, secret_access_key, bucket, endpoint, serving_mode, direct_link_ttl_seconds, cache_size_mb}`）
- 新增 Pillow 硬依赖（`requirements.txt`）；`aiobotocore` 仍为 R2 可选 opt-in

### Changed
- `prune_chat_sync` 现在只返回 `(events_pruned, meta_pruned)` —— 文件清理由 `main.py` 编排：列举 → 存储删除 → DB 删除（先存储后 DB，避免崩溃留 R2 orphan）。`session_meta` 物理删除前增加 `NOT EXISTS (file)` 保护，存储删除失败时 `session_meta` 留待下轮重试
- 90 天 session 物理删除时同步清 AstrBot CM 历史（`update_conversation(history=[])`），防止用户重新使用同名 session 时 CM 残留过期 `ImageURLPart` 显示破图
- 启动后 60-120s 跑首次 prune（之前要等满一个间隔 24 小时），让上次进程崩溃留下的 orphan 不必占着 quota 一整天
- `clear_history` 现在阻塞获取 PerTokenConcurrency 锁，避免和正在 stream 的 `/chat/stream` 并发删除其附件

### Fixed
- **B1** 非流式 `/chat` 在 LLM 失败时未释放已 commit 的附件 → 配额泄漏。改为 try/except 包住 LLM 调用，失败时调用统一的 `release_files_safely` 助手
- **B2 + H2/H3/H4** prune loop / `_release_attached_files` / `clear_history` 之前都是先删 DB 再删存储，崩溃可能留 R2 orphan 永远找不回。统一改为先删存储后删 DB（删存储成功的才删 DB 行）
- **H4 / 安全** logout 现在服务端真正失效 cookie：`CookieLogoutTracker` 记录每个 token 的登出时间戳，验证 cookie 时若签发时间早于 logout 则拒绝。Cookie 路径仍是 `{prefix}/files`，logout 路径同步移到 `{prefix}/files/logout` 让浏览器自动带上 cookie
- **H5** stream 中 `emit_stream_started` 已经把用户气泡（含附件 file_id）推给对端设备，紧接着 `close_failed`（如 empty_reply / llm_timeout 零 chunk）会释放文件 → 对端显示破图。修复：`StreamHandle.user_message_emitted` 标志，已发就跳过 `_release_attached_files`
- **#24** `mark_files_committed` 失败之前被 `logger.exception` 静默吞掉，下游 CM 会持久化指向不存在文件的 ImageURLPart。改为 fatal：触发 `close_failed("commit_failed")` + 500
- **#27 安全** `GET /files/{id}` 在"无 bearer + 无 cookie"分支之前直接返回 401 不调 `ip_guard.record_failure` → 匿名探测无成本。补齐 IP-guard 计数 + audit `auth_fail`，和 `/chat` 行为对齐
- **#28 安全** `/files/{id}` 响应补 `X-Content-Type-Options: nosniff` 和 `Content-Disposition: inline; filename=...` —— 当前 MIME 白名单很严已经够安全，这是 defense-in-depth
- **#33 并发** R2 LRU `_trim_cache_dir_sync` 之前跨 key 并发可能误删别的 coroutine 刚下载的文件（`protect_path` 只保护自己写的那个）。增加 store-级 `_trim_lock` 串行所有 trim 调用

### Internal / Schema
- 数据库 schema v4 → v5（webchat_files 表 + 索引）；新装直接 v5
- `storage/base.py` `prune_chat_sync` 签名变更：移除 `uncommitted_files_before_ts` 参数，移除返回值里的 `files_to_delete`。新增 `list_files_to_prune` + `list_sessions_to_purge` 助手让 main.py 编排 storage-first 删除流程

## v0.2.1 — 2026-05-02

### Added
- chat 页左侧多会话 sidebar：每个会话独立保留消息历史，标题自动从首条用户消息截前 25 字，按 `lastActiveAt` 倒序排
- 桌面 ≥720px 固定 240px 列；移动 <720px 改抽屉式 drawer，汉堡按钮触发，backdrop / ESC / 外点击都可关闭
- 鼠标 hover 显删除（X）按钮，触屏设备恒显（`@media (hover: none)`）

### Changed
- chat 页 localStorage 模式合并：原 `wcg.sessionId`（localStorage）+ `wcg.history`（sessionStorage）两套键统一为单个 `wcg.chat.sessions` JSON，结构 `{ activeId, sessions: { [id]: { id, title, lastActiveAt, history } } }`。**首次启动自动迁移**——旧的两个键内容会被组装成一个 session 写入新 store，旧键保留作为手动回滚路径
- 顶部 "新会话" 按钮被 sidebar 的 "+ 新会话" 取代，从 header 移除
- "清空" 按钮现在表示"清空当前 session 的消息"

### Fixed (defense in depth)
- chat sidebar 解析 `wcg.chat.sessions` 时按 schema 验证每条 session：损坏的 JSON / 数组以外形状 / 字段缺失 / role 不在枚举内的项目都跳过；store 完全损坏时 fall back 到一个空白 session，永不让坏数据 crash 登录后页面

### Known follow-ups (不阻塞 v0.2.1)
- localStorage 没设容量上限——长期累积多会话长对话可能撞 5–10MB 浏览器配额，目前 `QuotaExceededError` 静默吞掉
- 移动端 drawer 没做 focus trap——Tab 键能跳到背景按钮（ESC / backdrop 关闭都正常）

## v0.2.0 — 2026-05-02

### ⚠️ Breaking
- 默认拒绝缺少 `Origin`/`Referer` 头的写入类请求（POST `/chat`、admin POST/DELETE）。运营者升级后若仍依赖 curl / 服务端脚本访问写接口，需在配置加 `allow_missing_origin: true` 显式打开。GET 类只读接口（`/me`、`/site`、admin list/stats/audit）行为不变。

### Added
- `web/` Vite + TypeScript 构建管线：landing / login / chat 三个 end-user 页面从 `web/src/<page>/{index.html, main.ts, styles.css}` 编译，`vite-plugin-singlefile` 把 CSS+JS 全部 inline，产物覆写到 `examples/<page>/index.html`，后端路由 0 改动
- `web/src/shared/` 共享模块：`SiteConfig` 接口、localStorage 键常量、`$()`、`HREF_OK` 白名单、`resolveTheme()`、`setupThemeToggle()`、`loadSite()` —— 三页共用同一份 TypeScript runtime
- 内置 Vite 插件 `web/scripts/theme-init-plugin.mjs`：把头部 sync 主题脚本通过 `<!-- THEME_INIT -->` 占位符注入到产物，单一来源避免 FOUC
- GitHub Actions 工作流 `.github/workflows/web.yml`：每次 PR / push 跑 `npm ci` + `typecheck` + `build`，并断言 `examples/<page>/index.html` 与 `web/src/<page>/` 同步（用 `git diff --exit-code`），防止"忘了 rebuild"
- 新插件配置 `allow_missing_origin: bool`（默认 false）：把上面的 Breaking 变更做成可回退开关

### Changed
- 4 主题（paper / midnight / classic-light / classic-dark）统一使用 Comic Sans 展示字体：h1/h2/h3、品牌名、消息气泡、hero pill 等。Body / 表单 / 按钮仍走 `system-ui` 保读性
- chat 页消息历史从 `sessionStorage` 还原时做 shape 校验：损坏的 JSON / 数组以外的形状 / 不在 role 枚举内的项目都跳过，永远不让坏数据 crash 登录后页面

### Fixed
- **M1 (security)** `handlers/common.py`：`is_origin_allowed` 在 `Origin` 缺失时无条件返回 `True`，使 allow-list 对 curl / 服务端脚本失效。新增 `allow_missing` kwarg，写入类端点显式 opt-in 严格模式
- **LOW-1 (security)** `web/src/chat_client/main.ts`：chat POST 的 `fetch` 显式声明 `credentials: "same-origin"`，与同模块其他 fetch 对齐（与浏览器默认一致，但 audit 要求显式）
- **LOW-2 (security)** `examples/admin_panel/index.html`：`escape()` 现在也替换 `'` → `&#39;`，给将来可能引入的单引号属性场景做 defense-in-depth

### Internal
- landing / login / chat_client 全部迁移到 TypeScript 源（`web/src/<page>/`）
- admin_panel 故意**不迁移**——保留手写 HTML，作为运营者后台维持冷静专业的视觉

## v0.1.1 — 2026-05-02

### Added
- 4 主题系统（paper / midnight / classic-light / classic-dark），nav 加 sun/moon 浅深切换按钮，状态写入 `localStorage["wcg.theme.mode"]`
- 新增 `theme_family` 插件配置项（notebook / classic，默认 classic），通过 `/api/webchat/site` 暴露给前端，运营者一处设定全站生效
- chat 页：空状态占位、打字指示器（三点跳动）、纸飞机发送按钮图标
- 移动端适配：header 自适应换行、`env(safe-area-inset-bottom)` 底部安全区、textarea 强制 16px 防 iOS 自动缩放

### Changed
- 三个用户页（landing / login / chat）按 ui-ux-pro-max 的 "Soft UI Evolution" 重塑：统一 8-12px 圆角、多层柔和阴影、1px 细线边框
- Comic Sans MS 字体收紧到展示元素（h1/h2/h3、品牌名、消息气泡、hero pill 等），body / 按钮 / 表单走 system-ui 保证可读
- notebook 家族换钢笔墨蓝调（米白底 + 蓝墨水 + 浅蓝气泡 / 纯黑 #000 + 极光蓝紫双色径向晕）
- 默认 `theme_family` 改为 `classic`（沿用经典 GitHub 紫色），notebook 改为 opt-in
- 聊天输入框：`Enter` 直接发送，`Shift+Enter` 换行（原 `Ctrl/Cmd+Enter`）

### Removed
- 临时实现的 4 主题下拉选择器（被 sun/moon 切换 + 运营侧 `theme_family` 配置取代）
- 早期手绘风格遗留（非对称有机圆角、硬印章阴影、全局 Comic Sans body、微旋转、横线纸纹路）

## v0.1.0 — Initial release

首发版本：受控版 WebChat 网关、Token + 配额 + IP 防爆破、SQLite/MySQL 双后端、管理面板与聊天示例页。
