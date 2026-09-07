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

- `src/router.mjs` — 完整请求路径（官方请求透传、全局 GPT 路由，以及注入各服务商
  凭据后的旧命名空间直接转发）
- `src/gpt-route.mjs` — 读写全局 GPT 路由状态，默认使用官方路由
- `src/catalog.mjs` — 仅供手动旧目录流程根据官方捕获和 `config/` 构建
  `merged-models.json`；WLB 以对应官方条目为元数据基线，MiMo 输出明确指定的
  Xiaomi 元数据
- `src/model-registry.mjs` — 加载并验证 `config/**`；已列出模型的字段可选，但
  提供时必须通过验证
- `src/response-usage.mjs` — 从 JSON/SSE 响应提取 Token 用量；上游省略
  `content-type` 时也必须保持字节级透传和正确统计
- `src/sse-prefix.mjs` — 有界识别省略 `content-type` 的 SSE 前缀，并按原顺序交还
  探测期间缓存的字节
- `src/mimo-custom-tools.mjs` — 只处理 MiMo 的 custom tool/agent message 兼容、
  旧 `MESSAGE` 进度裁剪和 JSON/SSE 响应还原
- `src/start.mjs` — 监督唯一的路由器子进程并注入代理环境（`NODE_USE_ENV_PROXY=1`，
  `HTTPS_PROXY` 默认为 `http://127.0.0.1:7897`）；Windows 后台服务原子登记
  `service.pid`，退出时只清理仍属于自己的 PID
- `codex-router.ps1` / `install.ps1` — Windows 入口；`bin/` — POSIX 入口

## 规则

- 默认安装不写入 `model_catalog_json`，也不构建静态模型目录，官方目录由 Codex
  自己更新。只有手动旧目录构建时才要求固定 8 个模型：原生
  `gpt-5.6-sol/terra/luna`、MiMo `mimo-v2.5-pro` 和 `mimo-v2.5`，以及 WLB
  `gpt-5.6-sol/terra/luna`；其中 5 个模型位于两个服务商的命名空间下并由路由器转发。
- `route official|wlb|status` 管理或读取全局 GPT 路由；缺少状态文件时默认为
  `official`。设置从下一次 GPT 请求起对所有任务共享，不绑定线程；同一任务中途切换
  服务商可能带来历史上下文兼容风险。`wlb` 对未注册的同名模型直接返回 `409`，不
  回退官方；`route status` 只读且不支持 `--json`，只有总状态命令支持 `status --json`。
- `/models` 和 `/v1/models` 始终使用官方身份透传官方查询参数和完整元数据；WLB
  路由不替换官方模型列表。
- 每个改动过的 `src/*.mjs` 都必须运行 `node --check`；整个源码树的相对导入必须
  保持完整（对照 `grep -h 'from "./' src/*.mjs` 与实际文件）。
- 修改 `config/` 或手动旧目录代码后，运行 `node src/catalog.mjs`，确认旧目录的
  8 个模型 slug 和 5 个路由 slug 全部存在；重载路由器进程，如果模型选择器仍显示
  手动旧目录，再重启 Codex。默认官方目录状态无需运行该命令。
- 修改旧目录代码或源码后，运行 `node test/catalog-metadata.mjs` 和
  `node scripts-check.mjs`。
- 修改 `src/router.mjs` 或 `src/response-usage.mjs` 后，运行
  `node --test test/router-fixes.mjs`。
- 配置管理必须保留用户已有的 `[features.multi_agent_v2]` 子表（包括带引号的表名），
  不得重复注入同名内联表；启用、重复启用和关闭均需保持 TOML 有效。
- 修改 GPT 路由开关后运行 `node --test test/router-fixes.mjs`；修改配置管理、目录
  刷新入口或状态检查后运行 `node --test test/official-catalog.mjs`。
- 修改 `src/response-usage.mjs`、`src/sse-prefix.mjs` 或
  `src/mimo-custom-tools.mjs` 后，运行
  `node --test test/response-usage-hardening.mjs`。
- 修改 `src/http-utils.mjs`、`src/router.mjs` 或 `src/router-health.mjs` 的请求/响应
  有界读取或健康 deadline 路径后，运行 `node --test test/http-health-bounds.mjs`。
- 修改 `src/file-security.mjs` 或 `src/provider-credentials.mjs` 后，运行
  `node --test test/credential-file-security.mjs`。
- MiMo 兼容必须保持服务商隔离：标准 envelope 的旧 `MESSAGE` 进度不回放，
  `FINAL_ANSWER`、`NEW_TASK`、`FOLLOWUP_TASK` 仍保留；原生 GPT 和 WLB 不得应用
  MiMo 专属的历史裁剪或 custom-tool 映射。
- 切回原生 GPT 时，只能回放 `encrypted_content` 符合原生 `gAAAAA...` token
  形状的 `reasoning`；其他服务商的 reasoning 必须整项丢弃，合法项也必须移除仅
  用于输出的 `content`，避免 `store=false` 下引用未持久化的外部 item。
- 修改 `src/start.mjs` 或 `src/service-windows.mjs` 后，运行
  `node --test test/windows-service-process.mjs`。
- 修改 `src/http-utils.mjs` 的 graceful shutdown 路径后，运行
  `node --test test/graceful-shutdown.mjs`。
- 修改注册表时必须保留 slug 命名空间格式（`<provider>/<model>`），并通过
  `src/model-registry.mjs` 的验证器。
- 绝不能记录或提交凭据。持久化的服务商密钥只能通过 `provider-key set` 或
  `writeProviderCredential` 写入 `~/.codex/codex-router/*.secret`；不得记录临时
  环境变量中的密钥。
- Node 的 `fetch` 不使用 Windows 系统代理；任何在 `src/start.mjs` 环境之外
  发起 fetch 的进程也必须携带代理环境变量。
