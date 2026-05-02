# Changelog

记录本插件的可见变化。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

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
