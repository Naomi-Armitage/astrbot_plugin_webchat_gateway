# web/

前端源码。每个页面在这里以 TypeScript + Vite 维护，生产构建产物是单文件
`index.html`，写回到仓库根的 `examples/<page>/index.html`。Python 后端的
`_file_handler` 仍然按原路径直接读这些文件，**不要动后端**。

> ⚠️ 不要直接编辑 `examples/<page>/index.html`。它是 `npm run build`
> 的产物，每次重新构建都会被覆盖。改前端请改 `web/src/<page>/`，然后
> 跑一次 `npm run build`。产物头部有 `<!-- AUTO-GENERATED ... -->` 注释
> 提醒。

## 目录约定

```
web/
  src/
    landing/
      index.html
      main.ts
      styles.css
    <new-page>/
      ...
  scripts/build.mjs   # 多页构建驱动
  vite.config.ts      # dev 用 + build.mjs 共享的基础配置
```

每个页面是一个独立的 Vite root，互不影响。

## 安装

```sh
npm install
```

不提交 `package-lock.json`；本地装完即可。

## 构建

```sh
npm run build
```

会遍历 `scripts/build.mjs` 里的 `PAGES`，每一项产出
`../examples/<page>/index.html`（完全自包含，CSS/JS 内联）。

## 开发

```sh
npm run dev
```

默认进 `web/` 根；切到具体页面时直接指定 root：

```sh
npx vite src/landing
```

## 类型检查

```sh
npm run typecheck
```

走 `tsc --noEmit`，strict + `noUncheckedIndexedAccess` 等都开着。

## 新增页面

1. 在 `src/<name>/` 下新建 `index.html`、`main.ts`、`styles.css`。
2. 把 `<name>` 加到 `scripts/build.mjs` 的 `PAGES` 数组。
3. `npm run build` 验证产物。

## 注意：主题初始化脚本

`<head>` 里那个负责读 `localStorage` 设置 `data-theme` 的 `<script>` 必须
保持**字面量内联脚本**（不要 `type="module"`，不要 import）。
`vite-plugin-singlefile` 会内联模块脚本，但模块脚本是 defer 的，会在样式
应用之后才跑，导致 FOUC。原生 `<script>` 块同步执行，首帧主题就对。
