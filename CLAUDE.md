# Codex Router Lite — 代理说明

这是一个精简的个人分支。`main` 保持为未修改的上游快照；所有开发都在 `lite`
分支进行。

## 范围

只保留三个第三方服务商。它们都原生支持 Responses API，并由 `src/router.mjs`
直接转发（没有 LiteLLM、转发器或 Python）：

- `mimo-token-plan` — `config/mimo/` — `https://token-plan-cn.xiaomimimo.com/v1`
- `wlb-relay` — `config/wlb/` — `https://codex.wlbclub.com`
- `supergpt-relay` — `config/supergpt/` — `https://api.supergpt.dev/v1`

不得重新引入网关/转发层、服务商预设、OAuth 流程、托盘应用或自动更新器。

## 目录结构

- `src/router.mjs` — 完整请求路径（官方请求透传、全局 GPT 路由，以及注入各服务商
  凭据后的旧命名空间直接转发）
- `src/gpt-route.mjs` — 读写主代理和子代理各自的原生 GPT 路由状态，默认均使用官方路由
- `src/auto-route.mjs` — 请求触发的自动额度查询、默认 30 秒缓存和三档选择
- `src/auto-route-state.mjs` — 带路由 revision 校验的脱敏有效档位快照；状态查询不联网
- `src/model-registry.mjs` — 加载并验证 `config/**`；已列出模型的字段可选，但
  提供时必须通过验证；服务商必须显式声明 `protocol: openai-responses`，不保留旧网关协议转换
- `src/response-usage.mjs` — 从 JSON/SSE 响应提取 Token 用量；上游省略
  `content-type` 时也必须保持字节级透传和正确统计
- `src/sse-prefix.mjs` — 有界识别省略 `content-type` 的 SSE 前缀，并按原顺序交还
  探测期间缓存的字节
- `src/sse-events.mjs` — 有界解析 SSE 事件，支持多行 `data:` 与 CR/LF/CRLF；
  空行 CR 到达即结束事件，不等待后续字节
- `src/mimo-custom-tools.mjs` — 只处理 MiMo 的 custom tool/agent message 兼容、
  旧 `MESSAGE` 进度裁剪和 JSON/SSE 响应还原
- `src/start.mjs` — 监督唯一的路由器子进程并注入代理环境：官方请求默认走固定
  `http://127.0.0.1:7897`，不预先探测监听；显式 `HTTPS_PROXY`、`https_proxy`、
  `HTTP_PROXY`、`http_proxy` 按此优先级覆盖。`NO_PROXY` 保留自定义值并补入回环
  地址及 WLB/MiMo/SuperGPT 默认域名，三个第三方默认 DIRECT（直连）。
  `CODEX_ROUTER_PROXY_PORTS` 已退役，旧配置改用 `HTTPS_PROXY`；不定时探测或因
  端口变化自动重启。保留启动健康检查、进程退出及信号清理。Windows 后台服务
  原子登记 `service.pid`，退出时只清理仍属于自己的 PID；服务管理按已登记启动器的
  Node 路径验证进程，不依赖当前 CLI 的 Node；`status` 不删除 PID 文件。Windows
  进程查询优先 PowerShell Core，任务状态使用 Task Scheduler COM；停止查询共用
  10 秒 deadline，状态未知或超时必须停止后续启动，单次外部命令最多 15 秒。
  通用入口异步等待以保持操作锁续期；Windows start/restart 只由平台管理器检查
  就绪，install 及 POSIX start/restart 由通用入口检查，均最多 60 秒。监督器自身
  启动预算为 30 秒，最后一次探针可能越过预算，单次探针最多 10 秒。
- `codex-router.ps1` / `install.ps1` — Windows 入口；`bin/` — POSIX 入口

## 规则

- 不提供模型元数据更新或静态目录生成入口，也不写入 `model_catalog_json`；官方
  目录由 Codex 自己更新。服务商密钥操作不得触发目录重建。保留旧目录状态读取和
  配置迁移兼容；MiMo 的两个及 WLB 的三个命名空间 slug 继续由路由器转发。
- `route official|wlb|supergpt|auto|status` 管理或读取主代理全局 GPT 路由；`route subagent
  official|wlb|supergpt|status` 管理独立的子代理供应商路由。缺少状态文件时均默认为
  `official`。主代理设置从下一次非子代理的原生 GPT 请求起对所有任务共享，不绑定线程；
  子代理设置对所有任务的已识别原生 `gpt-*` 子代理共享，不读取主代理路由状态，
  上游错误直接返回，不触发 WLB 自动回退。识别复用子代理/父线程请求头和
  rollout 的 `source.subagent.thread_spawn`；缺失或未写完的 rollout 不缓存负结果，
  没有可识别证据时仍遵循主代理全局路由。显式 `wlb-relay/*` 与 MiMo 路由保持原语义。
  同一任务中途切换服务商可能带来历史上下文兼容风险。`wlb` 原样转发请求中的 `gpt-*` 模型名，不依赖旧目录注册，
  上游不支持时返回错误；仅手动 WLB 模式的主代理明确额度耗尽且尚未输出时自动保存为 `official`，默认立即
  使用原始 Codex 身份继续普通或压缩请求。手动设置和回退共用跨进程锁，
  回退在锁内核对请求发出时的 route/revision；期间手动切至 official，旧请求沿用官方
  重发且不改写 revision；切至 SuperGPT 或重选 WLB 时保留新状态并返回原错误。Windows
  路由临时文件的 ACL 异步设置优先 pwsh，失败时回退 powershell，两次尝试共用 10 秒预算；取消或 ACL 失败不替换状态并清理临时文件。
  每次写入更新 revision，兼容旧状态文件缺少该字段。
  普通限流、认证失败、套餐不包含 API
  和已输出的流不切换；缺少官方身份、路由状态损坏或状态写入失败时不回退。显式
  `wlb-relay/*` 和 MiMo 保持原语义。等待复用可取消的 `sleep`，由
  `CODEX_ROUTER_WLB_FALLBACK_DELAY_MS` 调整（0 至 30000 毫秒整数，其他值默认 0），
  不修改 Codex 客户端的重连间隔；切换后需手动 `route wlb` 才恢复 WLB。
  `supergpt` 同样按原始 `gpt-*` 模型名转发，不新增旧命名空间模型条目或生成目录；
  上游错误直接返回，不应用 WLB 专属的额度耗尽回退。首次新增服务商配置需重启
  Router，之后 `route` 热切换无需重启。SuperGPT 默认域名固定 DIRECT；自定义
  `SUPERGPT_BASE_URL` 的其他域名如需直连，应显式加入 `NO_PROXY`。
  `route status` 只读且不支持 `--json`，只有总状态命令支持 `status --json`。
- 自动模式：主代理可用 `route auto` 启用自动模式，按 **WLB → 官方 GPT → SuperGPT** 的顺序选择；
  额度恢复后，后续请求自动回到最高可用档，配置模式始终保持 `auto`。WLB 读取原始 `1d` / `7d`
  窗口，任一额度耗尽即跳过。GPT 同时监控 5 小时和周窗口：5 小时剩余 **≤5%** 或
  周剩余 **≤2%** 时跳过；所有实际存在的窗口均高于阈值时才恢复，缺失窗口不参与判断。
  Node Router 在请求到来时刷新额度，默认缓存 30 秒；没有整机常驻定时器，悬浮窗也不参与额度决策。
  同一请求至多尝试三家、每家一次，只有明确额度失败且尚未输出时才继续降级，SuperGPT 为终点。
  自动模式的官方错误体限读 64 KiB；超限明确返回 502 且不继续降级，限内的原生非额度错误保留原始字节。
  查询失败时保留已有档位；首次无法可靠判断则明确报错，不凭网络、认证或普通限流错误猜测额度。
  独立子代理不支持 `auto`，默认官方且继续遵循自己的开关；`codex-auto-review` 特殊模型继续使用官方。
  `status --json` 使用 `gptRoute.mode=auto`、可空的 `effectiveMode` 和无凭据 `auto` 诊断。
- `/models` 和 `/v1/models` 始终使用官方身份透传官方查询参数和完整元数据；WLB 和 SuperGPT
  路由不替换官方模型列表。
- 每个改动过的 `src/*.mjs` 都必须运行 `node --check`；整个源码树的相对导入必须
  保持完整（对照 `grep -h 'from "./' src/*.mjs` 与实际文件）。
- 修改 `config/` 后，通过 `src/model-registry.mjs` 的验证器并运行
  `node --test test/router-fixes.mjs`；运行中的路由器需重启才能加载配置变更。
- 修改源码后，运行 `node scripts-check.mjs`。
- 修改 `src/router.mjs` 或 `src/response-usage.mjs` 后，运行
  `node --test test/router-fixes.mjs`。
- 配置管理必须识别裸名、双引号和单引号的 `features` 表、`multi_agent_v2` 键及
  `[features.multi_agent_v2]` 子表；保留已有配置，不重复注入表或内联键，
  启用、重复启用和关闭均需保持 TOML 有效。
- 修改 GPT 路由开关后运行 `node --test test/router-fixes.mjs test/gpt-route-async.mjs`；修改配置管理、服务商
  密钥操作或状态检查后运行 `node --test test/official-catalog.mjs`。
- 修改 `src/response-usage.mjs`、`src/sse-prefix.mjs`、`src/sse-events.mjs` 或
  `src/mimo-custom-tools.mjs` 后，运行
  `node --test test/response-usage-hardening.mjs`。
- 修改 `src/http-utils.mjs`、`src/router.mjs` 或 `src/router-health.mjs` 的请求/响应
  有界读取或健康 deadline 路径后，运行 `node --test test/http-health-bounds.mjs`。
- 修改 `src/file-security.mjs` 或 `src/provider-credentials.mjs` 后，运行
  `node --test test/credential-file-security.mjs`。
- 修改 Windows 后台子进程调用后，运行 `node --test test/windows-hidden-consoles.mjs`。
- MiMo 兼容必须保持服务商隔离：标准 envelope 的旧 `MESSAGE` 进度不回放，
  `FINAL_ANSWER`、`NEW_TASK`、`FOLLOWUP_TASK` 仍保留；原生 GPT、WLB 和 SuperGPT 不得应用
  MiMo 专属的历史裁剪或 custom-tool 映射。
- SSE 用量解析缓存单事件最多 8 MiB，超限仍按字节透传并跳过该事件解析；MiMo
  转换的单个事件、JSON 响应和累计未完成工具状态各限 8 MiB，超限必须明确报错。
- 第三方压缩必须拒绝失败、不完整或空摘要，不得返回替换历史；普通及压缩请求的
  上游错误统一限读 64 KiB 并脱敏。响应流及缓冲读取均遵循闲置超时和请求取消。
- 切回原生 GPT 时，只能回放 `encrypted_content` 符合原生 `gAAAAA...` token
  形状的 `reasoning`；其他服务商的 reasoning 必须整项丢弃，合法项也必须移除仅
  用于输出的 `content`，避免 `store=false` 下引用未持久化的外部 item。
- 修改 `src/service.mjs` 后，运行 `node --test test/service-command.mjs`。
- 修改 `src/start.mjs` 或 `src/service-windows.mjs` 后，运行
  `node --test test/windows-service-process.mjs`；修改 `src/start.mjs` 还需运行
  `node --test test/start-proxy.mjs`。
- 修改 `src/http-utils.mjs` 的 graceful shutdown 路径后，运行
  `node --test test/graceful-shutdown.mjs`。
- 修改注册表时必须保留 slug 命名空间格式（`<provider>/<model>`），并通过
  `src/model-registry.mjs` 的验证器。
- 绝不能记录或提交凭据。持久化的服务商密钥只能通过 `provider-key set` 或
  `writeProviderCredential` 写入 `~/.codex/codex-router/*.secret`；不得记录临时
  环境变量中的密钥。
- Node 的 `fetch` 不使用 Windows 系统代理；任何在 `src/start.mjs` 环境之外
  发起 fetch 的进程也必须携带代理环境变量。
