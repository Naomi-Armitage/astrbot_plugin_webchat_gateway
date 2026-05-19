# Changelog

记录本插件的可见变化。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

## Unreleased

### ⚠️ Breaking
- **`master_admin_key` 硬下限从 16 字符上调到 24 字符**。本来认为 16 字符够（在 IP-guard + 常量时间比较的保护下），但**一旦日志泄漏，sub-24 字符的 key 是离线可破解时间数量级（数小时～数天）**，远低于密钥轮换周期。升级到本版本后：
  - 长度 ≥ 24 字符：照常工作。
  - 长度 16-23 字符：启动时一次性 ERROR 日志，**`admin_key` 被清空**，HTTP `/admin/*` 端点全部禁用（机器人内 `/webchat` 命令仍能用）。日志里会提示生成命令：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。
  - 同时，建议下限的 WARNING 阈值从 24 上调到 32，鼓励运维使用更长的随机 key。
  
  **升级建议**：在升级前用上面那条命令重新生成一个 32+ 字符的 key，更新到 `_conf_schema.json` / AstrBot 配置 UI，再重启插件。

### Added — UI 操作 / 多设备 / 流式
- 用户消息 hover **编辑** 按钮：把原文加载进底部 composer，由用户改完手动发送。原气泡不动，编辑后的内容作为新一轮在对话底部出现（用完整当前上下文）
- 用户消息 hover **再问一次** 按钮：一键把原文 + 附件作为新一轮 `/chat/stream` dispatch 到对话底部。原气泡不动；LLM 看到完整当前上下文（包含中间所有 turn），所以同一个问题在不同时刻可能得到不同答案。和 bot-side `重新生成`（截尾重生）显式区分
- 聊天页每条消息加 hover 三按钮：**复制 / 删除 / 重新生成**（最后一个仅 bot）。delete 调 `DELETE {prefix}/conversations/{sid}/messages/{idx}`，regenerate 调 `POST {prefix}/conversations/{sid}/regenerate`。移动端长按 350ms 触发同样的菜单（10px 移动容差当滚动取消）
- Markdown 代码块换成 Telegram 风：等宽（系统栈 `ui-monospace, SFMono-Regular, Menlo, Consolas`）+ 标题行 lang 标签 + 右上"复制全部"按钮（成功后 1.2s 显示"已复制"）+ 每行可单独点击复制（0.6s flash）
- 新增 `DELETE {prefix}/conversations/{sid}/messages/{idx}`：按渲染索引删单条消息，对端通过 `message_deleted` 事件同步。释放仅被该条引用的附件
- 新增 `POST {prefix}/conversations/{sid}/regenerate` body `{message_index}`：重新生成指定 assistant 回复。**会把 message_index 之后的所有 turn 一并 truncate**（对端逐条 `message_deleted` 事件），然后追加新 assistant。和 `/chat/stream` 互斥（共用 PerTokenConcurrency 锁）
- 新增 `EVENT_MESSAGE_DELETED = "message_deleted"` 事件类型，payload `{index, role}`

### Changed
- **`POST /conversations/{sid}/regenerate` 改为 SSE 流式**：以前是同步 LLM 调用 + JSON 响应，现在响应 `text/event-stream`，每个 `{"type":"chunk","delta":...}` 数据帧推一段增量文本，最终 `{"type":"done","reply":...,"remaining":...,"daily_quota":...}` 收尾。错误用 `{"type":"error","code":...}` SSE 帧告知。截断 + 持久化 + `message_deleted` / `message_added` 事件发布的语义都没变（只是 LLM 调用由 `generate_reply` 切到 `generate_reply_stream`）
- README API 参考补全 ~13 个之前未文档化的端点（`/chat/stream/*` 系列、`/conversations/*` 多设备同步、`/me`、`/title`、`/events`、admin session 等）
- `storage.mysql_pool_max` 配置项暴露（默认 5、范围 1-100），不再写死 maxsize=5
- 后台 prune 周期从 24h 缩到 6h，让 `_UPLOAD_ORPHAN_RETENTION_SECONDS=3600` 真正在 1-2 倍窗口内回收，避免孤儿上传占额度长达 25h
- regenerate 调 LlmBridge 时正确传 `image_urls`（重新解析 user 附件→storage→local path），不再把多模态轮重生成降级为纯文本
- 新 endpoint 锁竞争 429 写 `concurrent_block` audit（和 `/chat` 一致），观测性补齐
- `ChatDeps.conv_service` / `StreamRegistry.__init__` 用 `TYPE_CHECKING` 还原真实类型（之前为 `Any`），方法重命名能在 type-check 时报错

### Fixed
- **`aiobotocore` 转为必装依赖**：`uploads.storage_driver=r2` 是 schema 里 `["local", "r2"]` 仅有的两个选项之一，但 `aiobotocore` 在 `requirements.txt` 里被注释成 optional，pip 不读注释。配置了 r2 的用户启动时会看到 `R2FileStore unavailable (...); falling back to LocalFileStore` 警告并被静默降级回 local —— 一个开箱即用的支持选项不该需要用户手动 `pip install`。把 `aiobotocore>=2.13` 从注释里抬出来变真依赖（`aiomysql` 留着 optional，MySQL 是规模化场景）
- **SSE 终态帧 seq 防御性 clamp**：`/chat/stream` 在结束（成功 done / llm_timeout / empty_reply / 通用 Exception）时把 `handle_obj.next_seq` 当 terminal frame 的 `seq` 写出去。正常路径 `next_seq == last_appended_seq + 1`，但极端竞态下（close_incomplete / close_failed 与 buffer flush 并发，或 buffer driver 重置 next_seq）观测到 `next_seq == last_appended_seq`，让客户端收到一个**非单调**的终态 seq，dedup 链可能 mismatch。新增本地 `last_appended_seq` tracking，3 处 terminal write 用 `max(handle_obj.next_seq, last_appended_seq + 1)` 兜底，纯防御性 —— 正常路径行为不变
- **LLM 历史长度兜底**：`_history_text` 把所有 CM-paged 行 join 成 prompt，没有总长度上限。一个用户粘的日志/大段代码可能让单条 turn 几兆字符，挤掉系统提示和当前问题，甚至推爆上下文窗口。新增 `_MAX_HISTORY_CHARS = 8000` 兜底，超出走 tail-keep（丢最早，保最新 —— 对话连续性比保留早期片段更有用）。history_turns 已经在 CM 层窗口控制，这是字符级双层保护
- **LlmBridge fallback 丢失 persona system_prompt**：`generate_reply` 在 AstrBot 旧版本不接受 `image_urls=` 时会 TypeError fallback 到 `provider.text_chat(prompt=, image_urls=)` 直接调；fallback 路径**没传 system_prompt**，导致配置了人格的多模态请求悄悄丢失人格上下文（主路径 `llm_generate` 通过 `persona_id` 内部解析人格，fallback 没有等价机制）。在 fallback 调用里补 `system_prompt=system_prompt`（`_resolve_persona()` 返回值已在 scope 里），人格上下文恢复
- **SSE 握手 ConnectionError 误算 internal_error**：`/chat/stream` 的握手期 try/except 把 `ConnectionResetError` / `ConnectionError` 落到泛 `Exception` 分支 → `close_failed(error_code="internal_error")` → 污染异常率监控。客户端在 `: ready\n\n` 后立即 RST（快速重试 / 链路抖动）是**正常 cancelled**而非服务端故障。新增独立 except 分支：info-level 日志（不打 traceback）+ `close_failed("cancelled")` + 直接 return response（不 re-raise — 对端已断）
- **bot 重新生成时的双气泡 race**：以前点 bot 气泡的"重新生成"，POST 响应回来才调 `recordOptimistic` / `recordPendingDelete` 做去重。但 server 把 `message_added` / `message_deleted` 事件比 POST 响应更早通过 long-poll 推到 client 时，client 直接 push 一份新 bot，POST 响应再 push 一份 → 出现两个一模一样的 bot 气泡（F5 后才被 server 真实状态 1 条同步取代）。重新生成改走 SSE 流式后：(1) 立刻 pre-truncate + recordPendingDelete 防止后到的 `message_deleted(idx)` 把新 bot 误删；(2) 复用 `streamFinalizeSuppressed` 让抢先到的 `message_added` 接管 streaming 气泡；(3) SSE done 兜底再扫一次 history.tail，防止两层 race 都漏过的边界。彻底消除双气泡
- **`replayActive` 强制滑到底**：以前任何 replay 都无条件 `scrollToEnd()`，mid-history 删除 / 重新生成 / 编辑都会把用户从他正在读的位置弹到底部。改为：渲染前记录滚动位置，仅当用户已经"接近底部"（≤80px）时才 scrollToEnd；否则用 height delta 校正后还原原滚动位置
- **消息气泡的异常换行**：两部分修复 ——
  - **断词算法**：`.msg` 用 `word-break: break-word` 在所有气泡（user 和 bot 都中招）按字符切，长 URL / 长词被切得稀碎。改为 `overflow-wrap: break-word; word-break: normal;` —— 词边界优先，纯长串才退化到字符级断行，CJK 文本的自然断行也保留
  - **宽度循环依赖**：`.msg max-width: 78%` 的百分比相对的是 `.msg-row` 的 shrink-to-fit 宽度，而后者本身依赖 `.msg` 的 max-content（循环）。浏览器解析时先测得 row = `.msg` 的 max-content，再用这个值算出 `.msg max-width = 0.78 × max-content` —— 比内容本身还窄，强制触发不必要的换行。表现是"怎么正确 vibe coding"在宽屏上也被切成两行（气泡被压到自己 max-content 的 78%）。把 `max-width: 78%` 上移到 `.msg-row` 直接挂 `#messages`（定值百分比），`.msg` 不再 max-width，循环消除
- **失败消息的编辑按钮重复**：失败的 user 气泡同时挂"hover 编辑"和"气泡下方编辑铅笔图标"两个入口，且都执行同样的"加载到 composer"，UI 上还会互相重叠。失败气泡的 `.msg-actions` 整组隐藏，单留下方的常驻铅笔
- **R2 NoSuchKey 误判**：部分 botocore 版本下 NoSuchKey 通过 `ClientError` + `response["Error"]["Code"]` 暴露，原先只用 `type(exc).__name__` 子串匹配，漏判后 `R2FileStore.read` / `.delete` 会把"对象不存在"这种预期路径打成 `logger.exception` 完整 traceback，污染日志并干扰运维区分真实 R2 故障。新增 `_is_no_such_key(exc)` 辅助同时覆盖两种 shape（类名子串 + 结构化 Error.Code）
- **PIL 解码阻塞事件循环**：`/upload` 在 handler 协程里同步调 `detect_image_mime`，一张 20MB 图片的 PIL `verify()` + 格式探测能占用事件循环数百毫秒到数秒，期间所有 SSE 心跳、长轮询、LLM 流式输出都被挂起 —— 单进程网关的隐性 P0。新增 `detect_image_mime_async` 包装走 `asyncio.to_thread`，CPU 重活转默认线程池；同步实现保留供测试与其他工作线程上下文复用
- **数据目录违规 AstrBot 规范**：SQLite DB 与本地上传根目录默认值原本写死 `data/webchat_gateway.db` / `data/webchat_uploads`（裸落在 AstrBot 工作目录），违反 AstrBot 框架规范，AstrBot 重装/迁移时数据"消失"且不会随 `data/plugin_data/` 一起迁移。改为 `StarTools.get_data_dir("astrbot_plugin_webchat_gateway")` 下的 `webchat_gateway.db` / `webchat_uploads`；`_conf_schema.json` 默认值改空串由代码侧填默认，自定义路径仍可手动指定。`SqliteStorage.initialize()` 启动时若检测到旧路径有遗留 DB 而新路径为空会打印迁移指引（不自动移动，避免多实例环境下的误操作）
- **schema-version 降级 bug**：SQLite + MySQL migration ladder 之前无条件 `UPDATE _schema_meta SET value = CURRENT`，older binary 启动 newer DB 时会悄悄降级。改成 `if stored == CURRENT_SCHEMA_VERSION` 才写
- **plugin 静默启动失败**：`_start` 在 mysql 缺 DSN / storage init 失败时只 log + return，AstrBot 误以为 ready 后所有命令返回"插件未就绪"。改成 raise → AstrBot 显示"plugin failed to load"
- **logout endpoint 是 timing oracle**：cookie 在 HMAC 验证前就用 `peek_name` 查 DB，timing 差异可枚举 token name。新增 IP guard + `record_failure` 让探测在 brute-force 阈值后被锁
- **shutdown race**：`_stop` 关 storage 前没 cancel stream driver tasks，长轮询/流式 mid-LLM-call 撞 storage close 返回 500 而不是干净下线。新增 `StreamRegistry.cancel_all_drivers()` + 10s drain timeout
- **regenerate 中间 bot 让对端永久残留**：之前只 emit 一个 `message_deleted(target)` 但服务端 truncate 整个 tail，对端 splice 后多余消息永远不消失直到 cold refetch。改为逐条逆序 emit
- **regenerate 在 quota check 前 truncate**：429 quota_exceeded 时 CM 已 truncate、附件已释放、无事件发出，所有设备永久 desync。改为 quota check 优先
- **markdown 渲染允许注入假按钮**：DOMPurify 默认 allow list 含 `<button>` + `data-action`，恶意/幻觉 bot 回复可塞 `<button class="msg-action-btn" data-action="delete">领奖</button>` 触发真删除。改为 FORBID button + data-action，marked code renderer 输出 sentinel 由 JS post-sanitize 构造真按钮
- `make_me_handler` 和 admin `/me` 漏 `allow_missing=deps.allow_missing_origin` kwarg，导致 `allow_missing_origin=false` 时仍接受无 Origin 请求
- `regenerate_assistant_message` 取附件没 re-verify `row.token_name == token_name && row.session_id == session_id`，每个其他 call site 都有
- `release_files_safely` 的 `file_store=None` test fallback 改为 `raise` 而非静默删 DB 行（违反 storage-first 不变量）
- 长轮询事件批处理 `applyEvents` 包 try/finally，单事件抛错时仍 saveStore + renderSessionList
- streaming bubble 在 visibility 切换/SSE done 帧节流时和长轮询的 `message_added` 竞态，对端可能出现重复气泡。新增 `streamFinalizeSuppressed` 接力 + history-tail fallback dedup
- pendingLocals dedup 增加 history-tail 兜底（60s 窗口），抵御 stray double-delivery 已消费 dedup entry 的边界情形
- delete/regenerate 客户端补 `429 ip_blocked` + `403 forbidden_origin` 专门分支
- `_prune_task` cancel 时 `except (CancelledError, Exception): pass` 改为只静默 cancellation，真异常 `logger.exception`

### Internal — bounded data structures
- `CookieLogoutTracker._thresholds`、`EventBus._conds`、`R2FileStore._key_locks` 都新增 `prune_*` 方法在每轮 prune loop 里调，避免 token 删除后内存条目永久留存
- `ip_failures` 表加 `prune_ip_failures(before_ts)`，每轮 prune 删 24h 以上无失败的行

### Refactor
- 抽 `core.llm_bridge.map_llm_error(exc) → (code, status, audit_event)` 收敛 LLM 错误 → HTTP 映射（chat 非流式 + regenerate 共用）
- 抽 `core.file_lifecycle.commit_attachments_or_release` 收敛 `mark_files_committed → 失败 release` 信封
- 抽 `ConversationService._with_concurrency` `asynccontextmanager` 收敛 `acquire → 429 / inner` + `concurrent_block` audit（clear_history / delete / regenerate 三处共用）

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
- 非流式 `POST /chat` 在 LLM 失败（timeout / empty / error）时未释放已 commit 的附件，导致 token 配额永久泄漏。改为 try/finally 包住 LLM 调用，失败时通过统一 `release_files_safely` 助手释放
- prune loop、`StreamRegistry._release_attached_files`、`ConversationService.clear_history` 之前都是先删 DB 行再删存储对象，进程崩溃可能留 R2 / 磁盘 orphan 永远无法找回。三处统一改为先删存储后删 DB（删存储成功的才删 DB 行），失败的下一轮 prune 自动重发现重试
- 90 天 session 物理删除时同步清 AstrBot CM 历史（`update_conversation(history=[])`），防止用户复用同名 session 时 CM 残留过期 `ImageURLPart` 渲染破图。CM clear 失败的 session 跳过本轮 meta 删除，下轮重试
- 用户登出现在服务端真正失效 cookie：进程内 `CookieLogoutTracker` 记录每个 token 的登出时间戳；验证 cookie 时签发时间在登出前的一律拒绝。Cookie 路径仍是 `{prefix}/files`，logout 端点同步移到 `{prefix}/files/logout` 让浏览器自动带上 cookie。前端 `sendBeacon` 优先、`fetch(keepalive)` 兜底
- 流式中 `emit_stream_started` 已经把用户气泡（含附件 file_id）推给对端设备后，紧接着的 `close_failed`（如 empty_reply / llm_timeout 零 chunk）会释放文件 → 对端 `<img>` 集体 404 显示破图。修复：`StreamHandle.user_message_emitted` 标志，已发就跳过 `_release_attached_files`，附件留到 session 被 clear 或 90 天 cascade
- `mark_files_committed` 失败之前被 `logger.exception` 静默吞掉，下游 `record_chat_pair` 会把 `ImageURLPart` 写入 CM 指向不存在的文件，导致历史渲染破图。改为 fatal：非流式触发 release + 500，流式让异常传播到 outer except → `close_failed` 释放
- `GET /files/{file_id}` 在"无 bearer + 无 cookie"分支之前直接返回 401 不调 IP brute-force 计数 → 匿名探测无成本。补齐 `ip_guard.record_failure` + `auth_fail` audit，和 `/chat` 行为对齐
- `GET /files/{file_id}` 响应补 `X-Content-Type-Options: nosniff`、`Content-Disposition: inline; filename=...`、`Referrer-Policy: no-referrer` 三个安全头（defense in depth — 当前 MIME 白名单已经够严，这是为将来 allowlist 放宽留余地）
- R2 LRU 缓存的 `_trim_cache_dir_sync` 之前跨 key 并发可能误删别的 coroutine 刚下载的文件（`protect_path` 只保护本地调用刚写的）。增加 store-级 `_trim_lock` 串行所有 trim 调用

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
