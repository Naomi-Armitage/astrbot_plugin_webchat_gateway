# astrbot_plugin_webchat_gateway

受控版 WebChat 网关。把 AstrBot 的 LLM 能力以受控方式暴露给若干个朋友:**每人一个独立 Token、每日配额、单飞并发、IP 防爆破、SQLite/MySQL 双后端、独立管理 API + 配套示例面板**。

适用场景: 你想给几个朋友共享 AstrBot 的对话能力,既不想让 token 一旦泄露就全员失守,也不想被脚本刷掉 LLM 配额。

---

## 核心特性

| 维度 | 措施 |
| --- | --- |
| 鉴权 | 每好友独立 Token,SHA-256 哈希存储,签发后**仅显示一次** |
| 防滥用 | 每 Token 每日配额 + 同 Token 同时只允许 1 个请求 |
| 防爆破 | 同 IP 连续 N 次鉴权失败 → 临时封禁 |
| 跨域 | Origin 白名单 (CORS) |
| 存储 | SQLite (默认,零配置) 或 MySQL (生产环境) |
| 管理 | 独立管理 API + 示例 HTML 面板;同时保留 AstrBot 内置 `/webchat` 指令组 |
| 审计 | 所有签发/撤销/聊天/配额耗尽/封禁事件都写入 audit_log |
| 兼容 | 复用现有 webchat 插件的 LLM/persona/conversation 调用模式 |

---

## 快速开始

### 1. 安装依赖

`aiosqlite` 是必需的:

```bash
pip install aiosqlite
```

如果用 MySQL:

```bash
pip install aiomysql
```

### 2. 配置

在 AstrBot Dashboard 中配置插件:

- `host` / `port` / `endpoint_prefix` — 默认 `0.0.0.0:6186/api/webchat`
- `allowed_origins` — 生产环境务必填具体来源,如 `https://chat.example.com`
- `master_admin_key` — **必须**填写一段 32+ 字符强随机字符串 (用于管理 API)
- `default_daily_quota` — 默认 200 条/天
- `storage.driver` — `sqlite` 或 `mysql`
- 如选 `mysql`: `storage.mysql_dsn = mysql://user:pass@host:3306/dbname`

启动后日志应包含:

```
[WebChatGateway] HTTP server started at http://0.0.0.0:6186
[WebChatGateway] chat=/api/webchat/chat admin=/api/webchat/admin/tokens storage=sqlite ...
```

### 3. 给朋友签发 Token

**方式 A — 私聊 bot (最简单):**

```
/webchat issue alice 200
```

bot 会返回 token 明文 (仅显示一次,务必保存)。

**方式 B — HTTP 管理 API:**

```bash
curl -X POST http://127.0.0.1:6186/api/webchat/admin/tokens \
  -H "Authorization: Bearer $MASTER_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"alice","daily_quota":200,"note":"alice@example.com"}'
```

**方式 C — 浏览器管理面板:**

打开 `examples/admin_panel/index.html`,填入 API Base 和 Master Admin Key,在"签发"标签里填表单。

### 4. 朋友使用

朋友收到 token 后,可以:

- 直接调用 API:
  ```bash
  curl -X POST http://127.0.0.1:6186/api/webchat/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"session_id":"alice-laptop","username":"Alice","message":"你好"}'
  ```
  返回 `{"reply":"...","remaining":199,"daily_quota":200}`

- 或者用示例聊天页 `examples/chat_client/index.html`,把 token 粘贴进去,token 会存到 localStorage,下次打开自动加载。

---

## API 参考

### `POST {prefix}/chat`

朋友调用的主入口。

**请求头:** `Authorization: Bearer <token>` 或 `X-API-Key: <token>`

**请求体:**
```json
{
  "session_id": "可选,会话隔离用",
  "user_id": "可选",
  "username": "可选,显示名",
  "message": "必填"
}
```

**成功 200:**
```json
{
  "reply": "模型回复",
  "remaining": 199,
  "daily_quota": 200
}
```

**错误码:**

| 状态 | error 字段 | 含义 |
| --- | --- | --- |
| 400 | `invalid_json` / `invalid_payload` / `message_too_long` | 请求格式错误 |
| 401 | `unauthorized` | token 无效或已撤销 |
| 403 | `forbidden_origin` | 浏览器 Origin 不在白名单 |
| 429 | `ip_blocked` | IP 因鉴权失败被临时封禁 (响应头 `Retry-After`) |
| 429 | `concurrent_request` | 同一 token 已有进行中的请求 |
| 429 | `quota_exceeded` | 今日配额已用完 |
| 500 | `llm_call_failed` | LLM 服务异常 |

### 管理 API (需 `master_admin_key`)

所有管理端点都需要 `Authorization: Bearer <master_admin_key>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/webchat/admin/tokens` | 签发 `{name, daily_quota?, note?}` → `{token, ...}` (一次性) |
| DELETE | `/api/webchat/admin/tokens/{name}` | 撤销 |
| GET | `/api/webchat/admin/tokens?include_revoked=` | 列表 + 今日用量 |
| GET | `/api/webchat/admin/stats?name=&days=7` | 用量历史 (days 1-90) |
| GET | `/api/webchat/admin/audit?limit=100` | 审计日志 (limit 1-500) |

`master_admin_key` 为空时,所有 `/admin/*` 返回 `403 admin_disabled`。

### AstrBot 内置指令 (admin only)

| 指令 | 说明 |
| --- | --- |
| `/webchat issue <name> [daily_quota]` | 签发 token (仅在私聊中可用,防止泄露) |
| `/webchat revoke <name>` | 撤销 |
| `/webchat list` | 列出所有 token |
| `/webchat stats <name> [days]` | 查询用量 |

---

## 配置项

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `0.0.0.0` | HTTP 监听地址 |
| `port` | `6186` | HTTP 端口 |
| `endpoint_prefix` | `/api/webchat` | 路径前缀 |
| `allowed_origins` | `*` | 浏览器 CORS 来源白名单,逗号分隔。生产环境务必收紧。 |
| `max_message_length` | `4000` | 单条消息字符上限,防止 prompt 炸弹 |
| `history_turns` | `8` | 携带历史轮数 |
| `persona_id` | `""` | 使用的人格 (下拉框可选) |
| `default_daily_quota` | `200` | 签发新 token 的默认日配额 |
| `ip_brute_force_max_fails` | `10` | 同 IP 连续鉴权失败多少次后封禁,0 表示禁用 |
| `ip_brute_force_block_seconds` | `900` | 封禁时长 (秒) |
| `trust_forwarded_for` | `false` | **仅在可信反代后启用**,详见下方安全注意 |
| `master_admin_key` | `""` | 管理 API 主密钥,为空则禁用 admin |
| `storage.driver` | `sqlite` | `sqlite` / `mysql` |
| `storage.sqlite_path` | `data/webchat_gateway.db` | SQLite 文件路径 |
| `storage.mysql_dsn` | `""` | MySQL DSN,如 `mysql://user:pass@host:3306/dbname` |

---

## 安全注意事项

### 1. `trust_forwarded_for` 必须谨慎开启

只有当 AstrBot 部署在可信的反向代理 (Nginx / Caddy / Cloudflare) 之后,且代理已正确设置 `X-Forwarded-For` 时才启用。否则攻击者可以伪造请求头,使 IP 防爆破计数器指向任意 IP,导致防爆破完全失效。

如果 AstrBot 直接监听公网,**保持 `trust_forwarded_for=false`**。

### 2. `master_admin_key` 强度

- 至少 32 字符,完全随机
- 永远不要写进前端代码或 commit
- 一旦泄露,**立即在配置里更换并重启插件** (现存 token 不受影响,因为它们独立签发;但攻击者可以在更换前签发或撤销 token)

### 3. `allowed_origins`

生产环境**不要**保留 `*`。填入真实的前端域名,如 `https://chat.example.com,http://localhost:5173`。

### 4. Token 一旦泄露

- 朋友自己泄露 → `/webchat revoke <name>`,重新签发
- 日配额是兜底:即使 token 泄露,攻击者一天最多消耗一份配额

---

## MySQL 部署

```sql
CREATE DATABASE webchat_gateway DEFAULT CHARACTER SET utf8mb4;
CREATE USER 'webchat'@'%' IDENTIFIED BY '<strong-password>';
GRANT ALL ON webchat_gateway.* TO 'webchat'@'%';
FLUSH PRIVILEGES;
```

DSN 填入插件配置:

```
mysql://webchat:<password>@db.internal:3306/webchat_gateway
```

插件首次启动会自动建表 (`CREATE TABLE IF NOT EXISTS`),无需手动跑 DDL。

---

## 前端示例部署

`examples/chat_client/index.html` 和 `examples/admin_panel/index.html` 都是单文件 HTML,无构建依赖。

部署方式任选:
- 直接 `file://` 打开 (开发环境,需 `allowed_origins=*`)
- 静态文件服务: `python -m http.server -d examples/chat_client 5173`
- 上传到任何静态托管 (Nginx, Cloudflare Pages, GitHub Pages)

部署后把对应域名加入 `allowed_origins`。

---

## 端到端验证 (curl)

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

# 3. 用错 token 触发 IP 封禁 (重复 11 次)
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
curl $BASE/api/webchat/admin/tokens?include_revoked=true \
  -H "Authorization: Bearer $ADMIN_KEY"
```

---

## 致谢

LLM 调用 / 人格 / 会话持久化模式参考了 [astrbot_plugin_webchat](https://github.com/Dmt3tianOVO/astrbot_plugin_webchat)。
