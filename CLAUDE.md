# Codex Router Lite — 代理说明

这是一个精简的个人分支。`main` 保持为未修改的上游快照；所有开发都在 `lite`
分支进行。

## 范围

只保留两个第三方服务商。它们都原生支持 Responses API，并由 `src/router.mjs`
直接转发（没有 LiteLLM、转发器或 Python）：

- `mimo-token-plan` — `config/mimo/` — `https://token-plan-cn.xiaomimimo.com/v1`
- `wlb-relay` — `config/wlb/` — `https://codex.wlbclub.com`

不得重新引入网关/转发层、服务商预设、OAuth 流程、托盘应用或自动更新器。

## 目录结构

- `src/router.mjs` — 完整请求路径（原生请求透传，以及注入各服务商凭据后的直接
  路由转发）
- `src/catalog.mjs` — 根据原生捕获和 `config/` 构建 `merged-models.json`；只
  输出官方 GPT-5.6 原生 slug，WLB 精确复制对应原生条目，MiMo 则输出明确指定的
  Xiaomi 元数据
- `src/model-registry.mjs` — 加载并验证 `config/**`；已列出模型的字段可选，但
  提供时必须通过验证
- `src/response-usage.mjs` — 从 JSON/SSE 响应提取 Token 用量；上游省略
  `content-type` 时也必须保持字节级透传和正确统计
- `src/start.mjs` — 监督唯一的路由器子进程并注入代理环境（`NODE_USE_ENV_PROXY=1`，
  `HTTPS_PROXY` 默认为 `http://127.0.0.1:7897`）
- `codex-router.ps1` / `install.ps1` — Windows 入口；`bin/` — POSIX 入口

## 规则

- 合并目录必须固定包含 8 个模型：原生 `gpt-5.6-sol/terra/luna`、MiMo
  `mimo-v2.5-pro` 和 `mimo-v2.5`，以及 WLB `gpt-5.6-sol/terra/luna`。其中
  5 个模型必须位于两个服务商的命名空间下并由路由器转发。
- 每个改动过的 `src/*.mjs` 都必须运行 `node --check`；整个源码树的相对导入必须
  保持完整（对照 `grep -h 'from "./' src/*.mjs` 与实际文件）。
- 修改 `config/` 或模型目录代码后，运行 `node src/catalog.mjs`，确认 8 个模型
  slug 和 5 个路由 slug 全部存在；重载路由器进程，如果模型选择器尚未刷新，再
  重启 Codex。
- 修改模型目录或源码后，运行 `node test/catalog-metadata.mjs` 和
  `node scripts-check.mjs`。
- 修改 `src/router.mjs` 或 `src/response-usage.mjs` 后，运行
  `node --test test/router-fixes.mjs`。
- 修改注册表时必须保留 slug 命名空间格式（`<provider>/<model>`），并通过
  `src/model-registry.mjs` 的验证器。
- 绝不能记录或提交凭据。持久化的服务商密钥只能通过 `provider-key set` 或
  `writeProviderCredential` 写入 `~/.codex/codex-router/*.secret`；不得记录临时
  环境变量中的密钥。
- Node 的 `fetch` 不使用 Windows 系统代理；任何在 `src/start.mjs` 环境之外
  发起 fetch 的进程也必须携带代理环境变量。
