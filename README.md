<a id="astrbot_plugin_webchat_gateway"></a>
# astrbot_plugin_webchat_gateway

> 受控版 WebChat 网关 —— 把 AstrBot 的 LLM 能力以受控方式暴露给若干个朋友。
> **每人一个 Token、每日配额、单飞并发、IP 防爆破、CORS、SQLite/MySQL 双后端、独立管理 API + 配套示例面板。**

适用场景：你想给几个朋友共享 AstrBot 的对话能力，既不想让 token 一旦泄露就全员失守，也不想被脚本刷掉 LLM 配额。

## 目录

- [核心特性](#features)
- [安装](#install)
- [快速开始](#quickstart)
- [配置项](#configuration)
- [API 参考](#api)
- [AstrBot 内置指令](#bot-commands)
- [数据存储](#storage)
- [反向代理](#proxy)
- [安全注意事项与已知限制](#security)
- [示例页](#examples)
- [审计事件类型](#audit)
- [故障排查](#troubleshooting)
- [致谢与许可证](#license)

---

<a id="features"></a>
## 核心特性

| 维度 | 措施 |
| --- | --- |
| 鉴权 | 每好友独立 Token，SHA-256 哈希存储，签发后**仅显示一次** |
| 防滥用 | 每 Token 每日配额 + 同 Token 同时只允许 1 个请求 |
| 防爆破 | 同 IP 连续 N 次鉴权失败 → 临时封禁（带 `Retry-After`） |
| 跨域 | Origin 白名单（CORS） |
| 限流 | 请求体大小上限 + LLM 调用超时 |
| 数据隔离 | 历史按 `(token, session_id)` 隔离，两个 token 用同一个 sessionId 不会互相看到对话 |
| 存储 | SQLite（默认，零配置）或 MySQL（生产环境） |
| 管理 | 独立管理 API + 示例 HTML 面板；同时保留 AstrBot 内置 `/webchat` 指令组 |
| 审计 | 所有签发/撤销/聊天/配额耗尽/封禁事件都写入 `audit_log` |
| 兼容 | 复用 AstrBot 现有的 LLM / persona / conversation 管线 |

---

<a id="install"></a>
## 安装

### AstrBot 版本要求

需要 **AstrBot >= 4.17.0**。

### 依赖

`aiosqlite` 是必需的（SQLite 后端默认开启）：

```bash
pip install aiosqlite
```

如果计划使用 MySQL，请额外安装：

```bash
pip install aiomysql
```

> AstrBot 自身已经提供 `aiohttp`，本插件不会重复声明。`requirements.txt` 仅列出 `aiosqlite`；MySQL 驱动按需手动安装。

### 放入插件目录

把整个 `astrbot_plugin_webchat_gateway/` 目录放到 AstrBot 的 `data/plugins/` 下，或者通过 AstrBot Dashboard 的"插件市场"安装（如已发布）。重启 AstrBot 后在 Dashboard 中启用本插件，并完成下方[配置项](#configuration)。

---

<a id="quickstart"></a>
<a id="快速开始"></a>
## 快速开始

整个流程分三步：管理员先在 Dashboard 里完成最少配置 → 给朋友签发 Token → 朋友用 Token 调用 `/chat`。

### 1. 配置（Dashboard → 插件配置）

最少需要填写：

- `master_admin_key` — **必填**。32+ 字符强随机字符串，用于管理 API。留空将禁用所有 `/admin/*`。
  生成方式：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。
- `allowed_origins` — 生产环境务必填具体来源，如 `https://chat.example.com`，不要保留 `*`。
- `storage.driver` — `sqlite`（默认）或 `mysql`。
- 选 `mysql` 时 `storage.mysql_dsn` 必填，格式 `mysql://user:pass@host:3306/dbname`。

启动后日志应包含：

```
[WebChatGateway] HTTP server started at http://0.0.0.0:6186
[WebChatGateway] chat=/api/webchat/chat admin=/api/webchat/admin/tokens storage=sqlite ...
```

如果看到 `master_admin_key is empty; admin endpoints disabled` 的 WARNING，说明你还没填管理密钥；这种状态下机器人内的 `/webchat` 指令仍然可用，但 HTTP 管理 API 会一律返回 `403 admin_disabled`。

### 2. 给朋友签发 Token

签发是一次性操作 —— **明文 Token 仅在签发返回时出现一次**，服务端只保留 SHA-256 哈希，找不回明文。Token 一旦丢失，只能撤销后重新签发。

**方式 A — 私聊 bot（最简单）：**

```
/webchat issue alice 200
```

bot 会返回 token 明文（仅显示一次，务必保存）。出于安全考虑此命令仅在私聊中可用；群聊调用会被拒绝。

**方式 B — HTTP 管理 API：**

```bash
curl -X POST http://127.0.0.1:6186/api/webchat/admin/tokens \
  -H "Authorization: Bearer $MASTER_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"alice","daily_quota":200,"note":"alice@example.com"}'
```

**方式 C — 浏览器管理面板：**

打开 `examples/admin_panel/index.html`，填入 API Base 和 Master Admin Key，在"签发"标签里填表单。

### 3. 朋友使用 Token

朋友收到 token 后可以：

```bash
curl -X POST http://127.0.0.1:6186/api/webchat/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"alice-laptop","username":"Alice","message":"你好"}'
```

返回：

```json
{"reply":"...","remaining":199,"daily_quota":200}
```

或者直接打开示例聊天页 `examples/chat_client/index.html`，把 token 粘贴进去（会缓存到 localStorage）。

---

<a id="configuration"></a>
## 配置项

> 所有键名与 `_conf_schema.json` 一一对应；可在 AstrBot Dashboard 中可视化编辑。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `0.0.0.0` | HTTP 监听地址 |
| `port` | `6186` | HTTP 端口 |
| `endpoint_prefix` | `/api/webchat` | 路径前缀；聊天 = `{prefix}/chat`，管理 = `{prefix}/admin/...` |
| `allowed_origins` | `*` | 浏览器 CORS 来源白名单，逗号分隔。生产环境务必收紧。 |
| `max_message_length` | `4000` | 单条消息字符上限，防止 prompt 炸弹。同时**间接**决定整个请求体大小上限：`client_max_size = max(64 KB, max_message_length × 4)`，超过时由 aiohttp 直接返回 `413 payload_too_large`。无独立的 `max_request_body_bytes` 配置项。 |
| `history_turns` | `8` | 携带历史轮数（建议 4-12） |
| `llm_timeout_seconds` | `60` | 单次 LLM 调用的超时秒数（范围 5-600，建议 30-120）；超时返回 `504 llm_timeout`，并写入审计事件 `llm_timeout` |
| `persona_id` | `""` | 使用的人格（下拉框可选）；找不到时回退到"无人格"，并在日志里 WARNING |
| `default_daily_quota` | `200` | 签发新 Token 时的默认日配额（范围 1-1,000,000） |
| `ip_brute_force_max_fails` | `10` | 同 IP 连续鉴权失败多少次后封禁，`0` 表示禁用 |
| `ip_brute_force_block_seconds` | `900` | 封禁时长（秒，默认 15 分钟） |
| `trust_forwarded_for` | `false` | **仅在可信反代后启用**；详见[安全注意事项](#security) |
| `trust_referer_as_origin` | `false` | 浏览器请求缺少 `Origin` 时是否回退到 `Referer`。默认关；开启会削弱 CSRF 防御（详见[安全注意事项](#security)）。 |
| `master_admin_key` | `""` | 管理 API 主密钥；为空则禁用所有 `/admin/*`。建议 32+ 字符强随机。 |
| `storage.driver` | `sqlite` | `sqlite` 或 `mysql` |
| `storage.sqlite_path` | `data/webchat_gateway.db` | SQLite 文件路径，相对路径以 AstrBot 工作目录为基准 |
| `storage.mysql_dsn` | `""` | MySQL DSN，如 `mysql://user:pass@host:3306/dbname`（也支持 `mariadb://`）；`driver=mysql` 时必填 |

---

<a id="api"></a>
## API 参考

所有路径以 `endpoint_prefix` 为前缀，本节示例使用默认值 `/api/webchat`。所有响应均带有 CORS 头；浏览器侧 `OPTIONS` 预检由插件自动处理。

### `POST {prefix}/chat`

朋友调用的主入口。

**请求头：** `Authorization: Bearer <token>` 或 `X-API-Key: <token>`（两者任选其一，`X-API-Key` 优先级更高 —— 如果两者都给，Authorization 会被忽略）。

**请求体：**

```json
{
  "session_id": "可选,会话隔离用,默认 'webchat'",
  "user_id": "可选",
  "username": "可选,显示名,默认 'WebUser'",
  "message": "必填"
}
```

> 字段也接受驼峰别名 `sessionId` / `userId`。`session_id` 和 `user_id` 会被截断到 128 字符，`username` 截断到 64。

**成功 200：**

```json
{
  "reply": "模型回复",
  "remaining": 199,
  "daily_quota": 200
}
```

**错误码：**

| 状态 | `error` 字段 | 含义 |
| --- | --- | --- |
| 400 | `invalid_json` / `invalid_payload` | 请求格式不是合法 JSON 对象 |
| 400 | `message_too_long` | 单条消息超过 `max_message_length` |
| 401 | `unauthorized` | token 无效或已撤销 |
| 403 | `forbidden_origin` | 浏览器 Origin 不在白名单 |
| 413 | `payload_too_large` | 整个请求体超过 `max(64 KB, max_message_length × 4)` 自动派生的上限 |
| 429 | `ip_blocked` | IP 因鉴权失败被临时封禁，附 `Retry-After` 响应头 |
| 429 | `concurrent_request` | 同一 token 已有进行中的请求 |
| 429 | `quota_exceeded` | 今日配额已用完 |
| 500 | `llm_call_failed` | LLM 服务异常 |
| 504 | `llm_timeout` | LLM 调用超出 `llm_timeout_seconds` |

### 管理 API（需 `master_admin_key`）

所有管理端点都需要 `Authorization: Bearer <master_admin_key>`。`master_admin_key` 为空时所有 `/admin/*` 返回 `403 admin_disabled`。

管理端点也受 IP 防爆破保护：连续提交错误的 `master_admin_key` 同样会触发 `429 ip_blocked`（带 `Retry-After`），并写入 `admin_auth_fail` 审计事件。

管理接口收到超过请求体上限的 JSON 时会返回 `413 payload_too_large`，上限同样由 `max(64 KB, max_message_length × 4)` 自动派生。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/webchat/admin/tokens` | 签发：`{name, daily_quota?, note?}` → `{token, ...}`（一次性，201） |
| DELETE | `/api/webchat/admin/tokens/{name}` | 撤销 |
| GET | `/api/webchat/admin/tokens?include_revoked=` | 列出所有 token + 今日用量 |
| GET | `/api/webchat/admin/stats?name=&days=7` | 用量历史（`days` 1-90） |
| GET | `/api/webchat/admin/audit?limit=100` | 审计日志（`limit` 1-500） |

`stats` 返回字段：`name`, `daily_quota`, `revoked`, `created_at`, `revoked_at`, `history[]`（每项 `{day, count}`）。

`name` 必须匹配 `[A-Za-z0-9_.\-]{1,64}`；不允许复用已撤销的名字 —— 一旦签发再撤销，那个 name 就被审计历史"占住"了，请改用新 name 重发。

### 端到端验证（curl）

下面这段脚本依次走完"签发 → 聊天 → 触发 IP 封禁 → 撤销 → 列表"五个动作；可作为部署后烟囱测试。

```bash
ADMIN_KEY="your-strong-master-key"
BASE="http://127.0.0.1:6186"

# 1. 签发
TOKEN=$(curl -s -X POST $BASE/api/webchat/admin/tokens \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"alice","daily_quota":5}' | python -c "import sys,json;print(json.load(sys.stdin)['token'])")
echo "TOKEN=$TOKEN"

# 2. 聊天
curl -X POST $BASE/api/webchat/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s1","username":"Alice","message":"你好"}'

# 3. 用错 token 触发 IP 封禁（重复 11 次）
for i in $(seq 1 11); do
  curl -s -X POST $BASE/api/webchat/chat \
    -H "Authorization: Bearer wrong" -H "Content-Type: application/json" \
    -d '{"message":"x"}'
  echo
done

# 4. 撤销
curl -X DELETE $BASE/api/webchat/admin/tokens/alice \
  -H "Authorization: Bearer $ADMIN_KEY"

# 5. 列表
curl "$BASE/api/webchat/admin/tokens?include_revoked=true" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

---

<a id="bot-commands"></a>
## AstrBot 内置指令

仅 AstrBot 管理员可执行：

| 指令 | 说明 |
| --- | --- |
| `/webchat issue <name> [daily_quota]` | 签发 token（**仅在私聊中可用**，防止泄露） |
| `/webchat revoke <name>` | 撤销 |
| `/webchat list` | 列出所有 token + 今日用量 |
| `/webchat stats <name> [days]` | 查询用量（默认 7 天） |

`/webchat issue` 与 HTTP `POST /admin/tokens` 共享同一段业务逻辑（`TokenService`），因此 name 校验、配额范围、签发后的审计事件三者完全一致。

---

<a id="storage"></a>
## 数据存储

插件首次启动时会自动建表（`CREATE TABLE IF NOT EXISTS`），无需手动跑 DDL。表结构定义在 [`storage/ddl.py`](storage/ddl.py)：

- `tokens` — Token 元数据（仅存 SHA-256 哈希）
- `daily_usage` — 每日用量计数 `(name, day)`
- `ip_failures` — IP 失败/封禁状态
- `audit_log` — 审计事件流

迁移策略：DDL 是**累加式**的 —— 新版本只会新增字段或新表，不会改既有列。换句话说，从旧版本升级到新版本是热升级；但**降级不被支持**，回退老版本前请自行备份并 `DROP` 不认识的列/表。

### SQLite（默认）

零配置。数据库文件默认在 `data/webchat_gateway.db`（相对 AstrBot 工作目录）。

**备份**：插件运行时直接复制 `.db` 文件可能拿到不一致的快照（WAL 在途）。建议使用 SQLite 自带的 `.backup`：

```bash
sqlite3 data/webchat_gateway.db ".backup data/webchat_gateway.backup.db"
```

或者先停 AstrBot 再 `cp`。

### MySQL（推荐用于生产）

为 AstrBot 单独建库 + 单独账号；不要让插件直接写其它业务库。

```sql
CREATE DATABASE webchat_gateway DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'webchat'@'%' IDENTIFIED BY '<strong-password>';
GRANT ALL ON webchat_gateway.* TO 'webchat'@'%';
FLUSH PRIVILEGES;
```

> 上面把整个库的所有权限授给 `webchat` 用户是为了让插件能自动建表。如果你倾向最小权限，可以先用 root 账号跑一次让插件初始化，再把权限收紧到 `SELECT, INSERT, UPDATE, DELETE`。

DSN 格式：

```
mysql://webchat:<password>@db.internal:3306/webchat_gateway
```

填入插件配置 `storage.mysql_dsn`，并把 `storage.driver` 切到 `mysql`。重启后插件会自动建表。

`utf8mb4` 是必须的 —— 审计 `detail` 与 `note` 都可能含 emoji 与多字节字符；用 `utf8`（实为 utf8mb3）会在写入时报错。

---

<a id="proxy"></a>
## 反向代理

如果你希望通过 HTTPS / 同域访问聊天 API（也是浏览器侧最常见的部署方式），把 AstrBot 放到 Nginx 或 Caddy 后面，并搭配启用 `trust_forwarded_for`，让插件能正确读到客户端真实 IP（用于 IP 防爆破）。

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;

    # ssl_certificate / ssl_certificate_key ...

    location /api/webchat/ {
        proxy_pass         http://127.0.0.1:6186;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        # 用 $remote_addr（覆盖式），不要用 $proxy_add_x_forwarded_for —— 后者会
        # 保留客户端传入的 X-Forwarded-For 并追加真实 IP，攻击者可借此污染头部。
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Origin            $http_origin;
        proxy_read_timeout 120s;
    }
}
```

### Caddy

```caddyfile
chat.example.com {
    handle_path /api/webchat/* {
        reverse_proxy 127.0.0.1:6186 {
            header_up X-Forwarded-For {remote_host}
        }
    }
}
```

### 部署后 checklist

1. 把 `trust_forwarded_for` 设为 `true`，否则插件看到的全是反代的 `127.0.0.1`，IP 防爆破会失效。
2. 让 AstrBot 监听 `127.0.0.1:6186`（修改 `host`），不再直接暴露公网 —— 只信任反代过来的连接。
3. 把前端域名加入 `allowed_origins`。
4. 反代的 `proxy_read_timeout` 要 ≥ `llm_timeout_seconds + 一点余量`，否则反代会先超时切断，插件返回的 504 就传不出去。

---

<a id="security"></a>
## 安全注意事项与已知限制

### 1. `master_admin_key` 强度

- 至少 32 字符，完全随机（建议用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 或 `openssl rand -base64 36`）
- **永远不要写进前端代码或 commit 进 git**
- 一旦泄露，**立即在配置里更换并重启插件**。已签发的好友 token 不受影响（独立签发），但攻击者在更换前可任意签发/撤销。

### 2. `trust_forwarded_for` 必须谨慎开启

只有当 AstrBot 部署在**可信反向代理**（Nginx / Caddy / Cloudflare）之后，且代理已正确设置 `X-Forwarded-For` 时才启用。否则攻击者可以伪造请求头，使 IP 防爆破计数器指向任意 IP，从而完全绕过防爆破。

如果 AstrBot 直接监听公网（不走反代），**保持 `trust_forwarded_for=false`**。

### 3. `allowed_origins`

生产环境**不要**保留 `*`。填入真实的前端域名，逗号分隔，如：

```
https://chat.example.com,http://localhost:5173
```

浏览器收到不在白名单的 Origin 时会返回 `403 forbidden_origin`。

非浏览器客户端（curl、服务器端调用）通常不带 `Origin` 头，会直接放行；这是有意为之 —— Origin 白名单是 CSRF 防御，不是 API 鉴权，鉴权由 Bearer Token 负责。

### 4. `trust_referer_as_origin` 是 last resort

某些前端框架在特定配置下不发 `Origin` 头（典型场景：通过 service worker 反代）。这种情况下 Origin 白名单看到的是 `None`，会被无脑放行。开 `trust_referer_as_origin=true` 后，插件会回退去解析 `Referer` 的 `scheme://host` 部分作为 Origin。

⚠ Referer 比 Origin 更容易被剥离（隐私模式、`Referrer-Policy: no-referrer`），所以**默认关**；只在你确知自己的前端不会发 Origin 时再开。

### 5. Token 一旦泄露

- 朋友自己泄露 → `/webchat revoke <name>`，**用一个新 name** 重新签发（旧 name 无法复用，参见 API 章节）。
- 日配额是兜底：即使 token 泄露，攻击者一天最多消耗一份配额。
- 服务端只存 SHA-256 哈希，明文 token 仅在签发时返回一次。

### 6. 数据隔离

`history` / `conversation` 是按 `(token_name, session_id)` 双重隔离的；具体做法是把每个会话拼成 `unified_msg_origin = "webchat_gateway:{token_name}:{session_id}"`，再交给 AstrBot `conversation_manager`。两个朋友即使提交同一个 `sessionId`，也看不到对方的对话内容；本插件的 `webchat_gateway:` 前缀也避免了与其它插件的会话命名空间冲突。

### 7. 请求体大小 / LLM 超时

- **请求体大小上限是自动派生的**：`client_max_size = max(64 KB, max_message_length × 4)`，由 aiohttp 在请求进入业务逻辑之前直接拦截。**没有**独立的 `max_request_body_bytes` 配置项；调大 `max_message_length` 会同时放宽 body 上限。超过时返回 `413 payload_too_large`。
- `llm_timeout_seconds` 防止单次 LLM 调用挂起。超过返回 `504 llm_timeout`。范围 5-600 秒。

### 8. 已知限制

- **单进程并发锁**：每 token 单飞（concurrency=1）的限制由进程内 `asyncio.Lock` 实现。如果你把 AstrBot 跑成多 worker / 多副本，每个进程各自计数，并发限制会按副本数放大。本插件预期单进程部署；多进程部署不在 v0.1.0 范围内。
- **每日配额无 race-free 保证**：`daily_usage` 是"读 + 增"两步；但因为同 token 并发=1，单 token 维度上不会超额。跨 token 不共享配额。
- **Origin 白名单不防 API 直连**：curl / Postman / 服务端代理一律视为非浏览器请求放行。鉴权完全靠 Token。
- **审计是 best-effort**：写 `audit_log` 失败只会打 ERROR 日志，不会让请求失败。生产环境强烈建议把 AstrBot 日志接到集中收集（Loki / ELK），而不是只看 `audit_log` 表。
- **审计查询无过滤 / 无游标分页**（v0.2 候选）：`GET {prefix}/admin/audit` 仅支持 `?limit=N`（1-500），暂未提供 `event=`、`before_id=` 或时间范围过滤。朋友规模下 500 条上限基本够用；需要更复杂检索时建议直接 SQL 查 `audit_log` 表。
- **MySQL `audit_log` 索引方向与 SQLite 不一致**（v0.2 候选）：SQLite 用 `(ts DESC, id DESC)`，MySQL `idx_audit_ts_id` 用 `(ts ASC, id ASC)`；热路径的 `ORDER BY ts DESC, id DESC LIMIT N` 在 InnoDB 上是反向扫描，朋友规模下性能无感。如果迁移到大流量场景，可手动 `ALTER TABLE` 加一个降序索引。
- **MySQL 连接池大小不可配置**（v0.2 候选）：硬编码 `pool_min=1 / pool_max=5 / pool_recycle=3600`（见 `storage/mysql_backend.py`）。朋友规模下 5 条够用；如果实际打满需要扩容，目前需要改源码后重启。

---

<a id="examples"></a>
## 示例页

`examples/` 下提供三个单文件 HTML，无构建依赖：

- `examples/landing/index.html` — 项目主页（介绍 + 跳转到下面两个）
- `examples/admin_panel/index.html` — 管理面板（签发 / 撤销 / 查看用量 / 审计日志），需要填入 Master Admin Key
- `examples/chat_client/index.html` — 聊天客户端，朋友粘贴 token 即可使用，token 缓存在 localStorage

`admin_panel` 会把 API Base 和 Master Admin Key 缓存在浏览器 localStorage 中，便于重复管理。只建议在可信域名或本机环境使用；更谨慎时可用隐身窗口打开，或用完后清理浏览器站点数据。

部署方式任选：

- **本地开发**：直接 `file://` 打开（需要把 `allowed_origins` 暂时设为 `*`，**仅限本地调试**）。
- **静态文件服务**：`python -m http.server -d examples/chat_client 5173`。
- **静态托管**：上传到任何 Nginx / Cloudflare Pages / GitHub Pages。

部署后把对应域名加入 `allowed_origins`。三页之间的跳转通过相对路径，整体放到同一个静态站点下即可。

---

<a id="audit"></a>
## 审计事件类型

`audit_log` 表每行一条事件，字段 `id, ts, name, ip, event, detail`。`detail` 是 JSON 字符串，长度上限 1024 字符，超出会截断。

Token 生命周期（管理路径）：

- `issue` — 管理员签发新 token；`detail = {daily_quota, note_len}`
- `revoke` — 管理员撤销已存在 token；`detail = {revoked: true}`
- `revoke_miss` — 管理员撤销了一个不存在或已撤销的 token；`detail = {revoked: false}`

管理读操作（仅审计，不影响业务）：

- `admin_list` — 管理员列出 tokens（HTTP 或 `/webchat list`）；`detail = {include_revoked, count}`
- `admin_stats` — 管理员查询某 token 用量；`detail = {days}`
- `admin_audit` — 管理员拉取审计日志；`detail = {limit, count}`
- `admin_auth_fail` — 管理鉴权在 gate 处失败；`detail = {reason: 'no_token' | 'invalid_key' | 'admin_disabled' | 'ip_blocked', retry_after?}`

聊天路径（每请求一条）：

- `auth_fail` — bearer 缺失/无效/已撤销；`detail = {reason: 'no_token' | 'invalid' | 'revoked'}`
- `concurrent_block` — 同 token 并发被拦；`detail` 为空
- `quota_exceeded` — 当日配额耗尽；`detail = {today_count, quota}`
- `llm_timeout` — provider 调用超出 `llm_timeout_seconds`；`detail = {msg_len}`
- `chat_error` — provider 调用失败（非超时）；`detail = {error: <截断>}`
- `chat_ok` — 聊天成功；`detail = {msg_len, reply_len, remaining}`

可通过 `GET /api/webchat/admin/audit?limit=...` 查询；按 `ts DESC, id DESC` 排序，所以同秒并发写入也会有稳定顺序。

---

<a id="troubleshooting"></a>
## 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 启动日志无 `HTTP server started` | 端口被占用 / `host` 写错 | `lsof -i :6186` 或 `netstat -ano \| findstr 6186` 排查；改 `port` |
| 启动日志 `mysql driver requires mysql_dsn` | 选了 mysql 但没填 DSN | 填 `storage.mysql_dsn` 或切回 `sqlite` |
| 启动日志 `MySQL backend requires aiomysql` | 没装 aiomysql | `pip install aiomysql` 后重启 |
| 启动 WARNING `persona_id does not exist` | `persona_id` 配的人格已删除 | 在 Dashboard 重选已存在的人格，或留空使用默认 |
| 聊天接口返回 `chat_provider_not_configured` | AstrBot 还没配置 LLM Provider | 在 AstrBot Dashboard 配置一个聊天 Provider |
| 浏览器请求一律 `403 forbidden_origin` | `allowed_origins` 没含前端域名 | 加入对应 Origin 后重启插件；或确认前端真的在发 `Origin` 头 |
| 浏览器请求 `Origin: null` 被拒 | `file://` 打开示例页 / sandbox iframe | 走静态服务器（`python -m http.server`），别 `file://` 跑 |
| 防爆破始终拦不到 | 走了反代但 `trust_forwarded_for=false` | 把 `trust_forwarded_for` 设为 `true` |
| 防爆破拦错 IP | **没走反代但开了 `trust_forwarded_for=true`**（任何人都能伪造 `X-Forwarded-For`） | 关掉 `trust_forwarded_for` |
| `403 admin_disabled` | `master_admin_key` 为空 | 填入 32+ 字符强随机字符串 |
| `429 ip_blocked` 怎么解 | 对应 IP 被防爆破封禁了 | 等 `Retry-After` 秒数，或在 `ip_failures` 表 `DELETE FROM ip_failures WHERE ip = ?` |
| `429 concurrent_request` 持续出现 | 客户端同一 token 并发请求 | 串行化客户端，或为每个并发用户签发独立 token |
| `409 name_exists` 签发新 token | 这个 name 之前签过（即使已撤销） | 改用新 name；旧 name 不允许复用以保留审计连续性 |
| 怀疑数据损坏 | SQLite 在崩溃后被截断 | 停插件 → `sqlite3 db.sqlite "PRAGMA integrity_check"` |

> 没有 `/healthz` 探活端点；用进程日志 `[WebChatGateway] HTTP server started` 作为存活信号即可。

---

<a id="license"></a>
## 致谢与许可证

LLM 调用 / 人格 / 会话持久化的实现模式参考了 [astrbot_plugin_webchat](https://github.com/Dmt3tianOVO/astrbot_plugin_webchat)。

本插件以 **GNU AGPLv3** 协议开源，详见 [`LICENSE`](LICENSE)。
