# Changelog

记录本插件的可见变化。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

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
