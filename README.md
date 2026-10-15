# Codex Router Lite

这是 [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router)
的精简分支：Codex 保持使用官方模型目录，本地路由器只负责把请求转发到官方
Codex 后端或已配置的第三方服务商。

默认路由是 `official`。对官方目录中的 GPT 模型，主代理遵循全局 `official`/`wlb`/`supergpt`/`auto`
开关；已识别的原生 `gpt-*` 子代理默认走官方，也可通过独立的子代理供应商开关选择
`official`/`wlb`/`supergpt`。选择器选中的 `gpt-*` 模型名原样传给
WLB 或 SuperGPT，新增模型无需修改本地名单，实际可用性取决于上游及 API key 分组支持。全局设置不绑定
线程，从下一次非子代理的原生 GPT 请求起对所有任务共享；同一任务中途切换服务商可能带来
历史上下文兼容风险。MiMo 和 WLB 的旧命名空间 slug 仍保留给旧目录或直接请求
使用，但不会显示在官方模型选择器中。

默认安装不写入 `model_catalog_json`，也不构建静态模型目录；Codex 的官方目录由
Codex 自己更新。本项目已移除模型元数据更新及静态目录生成入口。

## 从上游移除了什么

已移除这些服务商不需要的全部组件：LiteLLM 及其他网关或转发层、Python、OAuth
流程、服务商预设、托盘/桌面应用和自动更新器。服务仅依赖 Node：`src/start.mjs`
监督一个 `src/router.mjs` 子进程，不再启动额外的网关或转发进程。收到重启信号时，
路由器先停止接收新请求，在途请求最多 drain 2 秒；流式请求收到 terminal error 后
clean EOF，未发出响应头的请求返回 503。可用 `MODEL_ROUTER_SHUTDOWN_DRAIN_MS`
调整 drain 时长。仓库保留的检查脚本是 `test/official-catalog.mjs`、
`test/router-fixes.mjs`、`test/response-usage-hardening.mjs`、
`test/http-health-bounds.mjs`、`test/credential-file-security.mjs`、
`test/graceful-shutdown.mjs`、`test/upstream-hardening.mjs`、
`test/windows-service-process.mjs`、`test/windows-hidden-consoles.mjs`、
`test/start-proxy.mjs`、`test/service-command.mjs`、`test/gpt-route-async.mjs`、
`test/auto-route.mjs`
和 `scripts-check.mjs`。

## 架构

三个第三方服务商都原生支持 Responses API，因此请求链路只有：

```
Codex ──(官方模型目录 + config.toml: openai_base_url)──▶ router.mjs
    ├─ 已识别的 GPT 子代理 + subagent official → ChatGPT Codex 后端（透传 Codex 身份验证）
    ├─ 已识别的 GPT 子代理 + subagent wlb/supergpt → 对应第三方服务商
    ├─ GPT 主代理 + route auto     → 按额度选择 WLB → 官方 GPT → SuperGPT
    ├─ GPT 主代理 + route official → ChatGPT Codex 后端（透传 Codex 身份验证）
    ├─ GPT 主代理 + route wlb      → WLB Relay（注入 WLB_API_KEY）
    ├─ GPT 主代理 + route supergpt → SuperGPT（注入 SUPERGPT_API_KEY）
    ├─ mimo-token-plan/*         → MiMo Token Plan（旧命名空间，注入 MIMO_API_KEY）
    └─ wlb-relay/*               → WLB Relay（旧命名空间，注入 WLB_API_KEY）
```

- **官方目录**：默认安装会移除本安装维护的 `model_catalog_json`，让 Codex 使用
  官方目录并自行更新；设置或删除服务商密钥也不会重建本地目录。
- **GPT 手动路由开关**：`route official|wlb|supergpt|status` 写入或读取全局路由状态，切换从下
  一次非子代理的原生 GPT 请求生效，首次新增服务商配置需重启 Router；之后热切换不需要重启服务或 Codex。
  `wlb` 和 `supergpt` 按请求中的 GPT 模型名直接转发，
  不依赖本地模型名单；上游不支持时仍返回其错误。仅 WLB 明确报告额度耗尽时，路由器自动保存为
  `official`，默认立即用原始 Codex 身份继续当前请求，后续 GPT 主代理请求也走官方。
  普通限流、认证失败、套餐不包含 API 和已开始输出的流不会触发切换；缺少官方身份或
  无法保存路由状态时不会回退。切换覆盖普通请求和压缩请求，显式 `wlb-relay/*` 保持原语义。
  自动回退仅在请求发出后的路由状态未变时写入；期间手动切至 official，旧请求沿用
  当前官方选择重发，不改写 revision；切至 SuperGPT 或重选 WLB 时保留新状态并返回原错误。
  SuperGPT 上游错误直接返回，不触发该回退或改变全局路由。
  额度恢复后需手动运行 `route wlb` 切回。`route status` 只读；只有总状态命令支持
  `--json`（`status --json`）。该开关不绑定线程，同一任务中途切换服务商可能带来
  历史上下文兼容风险。
- **GPT 自动路由**：主代理可用 `route auto` 启用自动模式，按 **WLB → 官方 GPT → SuperGPT** 的顺序选择；
  额度恢复后，后续请求自动回到最高可用档，配置模式始终保持 `auto`。WLB 读取原始 `1d` / `7d`
  窗口，任一额度耗尽即跳过。GPT 同时监控 5 小时和周窗口：5 小时剩余 **≤5%** 或
  周剩余 **≤2%** 时跳过；所有实际存在的窗口均高于阈值时才恢复，缺失窗口不参与判断。
  Node Router 在请求到来时刷新额度，默认缓存 30 秒；没有整机常驻定时器，悬浮窗也不参与额度决策。
  同一请求至多尝试三家、每家一次，只有明确额度失败且尚未输出时才继续降级，SuperGPT 为终点。
  自动模式的官方错误体限读 64 KiB；超限明确返回 502 且不继续降级，限内的原生非额度错误保留原始字节。
  查询失败时保留已有档位；首次无法可靠判断则明确报错，不凭网络、认证或普通限流错误猜测额度。
  独立子代理不支持 `auto`，默认官方且继续遵循自己的开关；`codex-auto-review` 特殊模型继续使用官方。
- **GPT 子代理**：已识别的原生 `gpt-*` 子代理默认走官方，普通请求和压缩请求
  共用规则，且不读取主代理的全局路由状态。可运行 `route subagent official|wlb|supergpt|status`
  独立切换，选择对所有任务的后续子代理请求共享。供应商未启用、缺少凭据或子代理路由状态
  损坏时请求失败关闭。子代理上游错误直接返回，不触发 WLB 自动回退，也不改写主代理路由。
  识别复用 `x-openai-subagent`、有效的 `x-codex-parent-thread-id`，或线程 rollout 的
  `source.subagent.thread_spawn`；明确的 `false`/`0` 子代理头不作为证据。rollout 缺失
  或首行未写完时会在下次请求重试；没有可识别证据的请求仍按主代理请求遵循全局开关。
  此规则只适用于原生 `gpt-*`，显式 `wlb-relay/*` 和 MiMo 路由继续遵循原命名空间。
- **旧命名空间路由**：WLB 的 `wlb-relay/gpt-5.6-sol`、`terra`、`luna` 与官方
  GPT slug 一一对应；MiMo 的两个命名空间 slug 继续由原路由处理，但两者都不进入
  官方选择器。
- **官方模型列表**：`/models` 和 `/v1/models` 复用原生转发并保留查询参数及官方
  完整响应；WLB 和 SuperGPT 路由也不会把模型列表替换成本地简化目录。没有当前 Codex 登录身份
  时返回 `401`。
- **原生请求**：独立的 `/alpha/search` Web Search 和图片请求只转发给原生
  Codex 后端；上游省略 `content-type` 时，即使 SSE 字段被拆到多个 chunk，或前面
  带 BOM、注释和空行，路由器也能识别并统计 Token，同时保持原始字节不变。
  原生流在 `response.completed` 后才断开时仍按 upstream status/usage 记录，不再误记
  为取消或 `0`；原生 GPT-5.6 请求会移除旧兼容字段 `prompt_cache_retention`，保留
  `prompt_cache_options`。SSE 支持多行 `data:` 和 CR/LF/CRLF 换行；单事件解析缓存
  上限为 8 MiB，超限时仍原样透传，仅跳过该事件的用量统计。
- **MiMo 长会话兼容**：Codex 的 custom tool 会在请求侧桥接为普通 function tool，
  JSON/SSE 响应再还原成 custom tool 事件。跨模型续接会保留已经验收的
  `FINAL_ANSWER`；标准协作 envelope 中的旧 `MESSAGE` 进度不发送给 MiMo，
  `NEW_TASK` 和 `FOLLOWUP_TASK` 仍保留。第三方路由必须解析原生加密协作载荷时，
  会按原顺序最多 4 路并发；解密结果保存在进程内 LRU 缓存，逻辑 TTL 为 24 小时，
  最多 512 条或 8 MiB。路由器估算第三方输入 Token 时不再把不会发送给模型的
  `encrypted_content` 密文计入提示词大小。切回原生 GPT 时，没有原生加密上下文的
  第三方 reasoning 会被丢弃，不会作为未持久化的 `rs_*` item 继续回放。MiMo 响应
  转换的单个 SSE 事件、JSON 响应和累计未完成工具状态各限 8 MiB，超限明确报错。
- **压缩历史**：第三方摘要必须完整且非空；HTTP 200 中的失败、不完整或空摘要
  返回 502，不生成替换历史，避免丢失续接上下文。省略 status 的有效摘要仍兼容。
- **错误和日志安全**：请求日志默认关闭；启用后会自动遮盖 HTTP/WS caller URL
  中的 capability。普通及压缩请求的第三方上游错误体最多读取 64 KiB，返回前会遮盖 Bearer、token、
  key、secret、caller capability、查询参数和控制字符；可解析的 quoted JSON 字段仍保留
  合法的 JSON 结构。请求正文超限后会停止缓存、排空余流，并允许客户端断开信号
  终止读取；协作载荷 relay 和 compact 响应分别最多缓冲 4 MiB 与 32 MiB。
- **凭据文件**：持久化密钥只从普通文件读取，符号链接和目录会被忽略。Windows
  写入时会用仅包含当前用户 `FullControl` 的非继承 DACL 替换旧 ACL。
- **代理**：默认通过 `http://127.0.0.1:7897` 转发官方请求，不预先探测端口是否监听。
  显式代理按 `HTTPS_PROXY`、`https_proxy`、`HTTP_PROXY`、`http_proxy` 的优先级覆盖，
  再经 `NODE_USE_ENV_PROXY` 注入路由器。`NO_PROXY` 保留自定义值，并补入
  `127.0.0.1`、`localhost`、`codex.wlbclub.com`、`token-plan-cn.xiaomimimo.com`
  和 `api.supergpt.dev`，三个第三方服务商默认 DIRECT（直连）。自定义
  `SUPERGPT_BASE_URL` 的其他域名不会自动直连，需要时将该域名加入 `NO_PROXY`。
  `CODEX_ROUTER_PROXY_PORTS` 已退役，旧配置请改用 `HTTPS_PROXY`；不再定时探测
  或因代理端口变化自动重启。代理配置变更后需重启 Router；代理未运行时，官方请求
  会连接失败，不会自动改为直连。代理恢复监听同一端口后无需重启 Router。

## 安装与日常使用（Windows）

```powershell
.\codex-router.ps1 install                    # 安装依赖、密钥、路由器和服务；默认使用官方目录
                                                # 仅使用官方路由时无需第三方密钥
.\codex-router.ps1 provider-key mimo-token-plan set  # 使用 MiMo 时再配置
.\codex-router.ps1 provider-key wlb-relay set        # 使用 WLB 时再配置
.\codex-router.ps1 provider-key supergpt-relay set   # 使用 SuperGPT 时再配置
.\codex-router.ps1 route official             # 全局：GPT 主代理请求走官方（默认）
.\codex-router.ps1 route wlb                  # 全局：GPT 主代理请求走 WLB；子代理供应商另行设置
.\codex-router.ps1 route supergpt             # 全局：GPT 主代理请求走 SuperGPT
.\codex-router.ps1 route auto                 # 主代理：按额度自动选择并在恢复后回升
.\codex-router.ps1 route status               # 查看当前 GPT 路由
.\codex-router.ps1 route subagent wlb         # 子代理请求走 WLB（独立于主代理）
.\codex-router.ps1 route subagent status      # 查看子代理供应商
.\codex-router.ps1 enable
.\codex-router.ps1 start
.\codex-router.ps1 disable
.\codex-router.ps1 uninstall
.\codex-router.ps1 status                     # 只读检查路由器、配置、Provider 和服务
```

POSIX：`bin/install`（兼容入口 `install.sh`）、`bin/provider-key`、`bin/enable`、`bin/disable`、
`bin/uninstall`、`bin/start`。路由切换使用
`bin/model-router codex route official|wlb|supergpt|auto|status`；子代理使用
`bin/model-router codex route subagent official|wlb|supergpt|status`；状态检查使用
`bin/model-router codex status`。这些命令只读取或更新本地状态，不会请求上游服务；
官方模式的状态检查不依赖静态 8 模型目录。返回码为 0 表示已就绪，1 表示需要处理，
2 表示参数错误；只有总状态命令支持 `--json`，即 `status --json`。`route status`
为只读文本查询，不接受 `--json`。自动模式下 `status --json` 的 `gptRoute.mode` 为
`auto`，`effectiveMode` 是已确认的当前供应商（首次请求前可为 `null`），`auto` 提供
`checkedAt`、`degraded`、`errors` 及脱敏额度诊断，不包含凭据。

SuperGPT 默认基础 URL 是 `https://api.supergpt.dev/v1`，可用 `SUPERGPT_BASE_URL` 覆盖；
前台可用 `SUPERGPT_API_KEY`，后台应通过 `provider-key supergpt-relay set` 安全保存并启用。
密钥文件为 `supergpt-api-key.secret`，macOS Keychain 服务名为 `codex-router-supergpt`。
余额查询使用 `https://api.supergpt.dev/v1/usage` 的账户 `balance` 和 `unit`；
推理使用 `/v1/responses`，不把控制台域名作为推理或余额 API 地址。
首次更新到包含 SuperGPT 的配置后需运行 `node src/service.mjs restart` 重启 Router，
之后 `route supergpt` 热切换无需重启。
使用 API key，网页控制台 token 不能替代；具体模型可用性由 key 的分组决定，参见
[SuperGPT 客户端说明](https://console.supergpt.dev/help/clients)和
[Codex 接入说明](https://console.supergpt.dev/help/codex)。

状态保存在 `~/.codex/codex-router/`（路由状态、密钥和日志；旧目录为可选文件）。Windows 通过名为
“Codex Router”的计划任务在登录时自动启动服务。后台监督进程会登记
`service.pid`；停止或重启时会按已登记启动器校验 Node 路径和完整的 `src/start.mjs` 命令行后再
结束该进程树，避免旧路由器继续占用 4102 端口。Windows 下停止先写入
`shutdown.request` 请求路由器排空退出（在途请求按重启 drain 规则收尾），超时才强杀；
`start`/`restart` 会等待 `/health` 通过后才返回。CLI 与后台使用不同 Node 安装时
仍能识别服务；`status` 不会清理 PID 文件。进程查询优先使用 PowerShell Core，计划任务
状态使用 Task Scheduler COM 查询；计划任务停止查询及 PowerShell 回退共用 10 秒
截止时间，状态未知或超时即返回错误，不继续启动新实例；单次外部命令最多 15 秒。
Windows 的 `start`/`restart` 由平台管理器执行最多 60 秒的就绪检查，通用入口不重复等待；
`install` 和 POSIX 的 `start`/`restart` 由通用入口执行最多 60 秒的就绪检查。
监督器 `src/start.mjs` 仍单独执行子进程启动健康检查，启动预算为 30 秒；最后一次
探针可能越过预算，单次探针最多 10 秒。通用入口异步等待平台子进程，使服务操作锁持续续期。

另一台设备不应复制上述状态目录。拉取 `lite` 后，仅使用官方路由可直接从当前
checkout 运行 `.\codex-router.ps1 install`；需要第三方路由时，再先运行
`.\codex-router.ps1 provider-key <provider> set` 配置要使用的服务商。安装器会确保本机 caller secret、配置
Codex endpoint，并登记 Windows 计划任务。Windows 目前不支持从
另一 checkout 无缝接管已有 state owner；请继续从原 checkout 更新，或先人工解决
owner 冲突。安装完成后完全退出并重新打开 Codex，使新的本地 endpoint 生效；之后
`route` 切换不需要重启。
`node src/service.mjs restart` 只能重启已经登记的任务；如果 `schtasks /Run` 报错，
请重新执行 install，Windows 策略拒绝任务登记时改用管理员 PowerShell。

原生 Web Search 和图片请求必须携带当前 ChatGPT/Codex 会话的 `Authorization`；缺少
该请求头时路由器直接返回 `401`，不会访问原生上游。

## 维护

- 更新官方模型：无需重建本地目录，Codex 会自行更新官方模型目录。
- 迁移旧目录：若仍配置了本安装维护的 `model_catalog_json`，重新执行
  `.\codex-router.ps1 enable`（POSIX：`bin/enable`），再完全重启 Codex。旧目录文件
  不会自动删除；用户自有目录配置仍会拒绝覆盖。
- 切换 GPT 路由：运行 `route official`、`route wlb` 或 `route supergpt`；设置从下一次非子代理的原生 GPT 请求生效，
  对所有任务共享且无需重启。子代理供应商使用独立的 `route subagent official|wlb|supergpt|status` 开关，
  默认官方，同样无需重启；同一任务中途
  切换服务商可能带来历史上下文兼容风险。
- 服务商协议只接受 `openai-responses`，旧网关协作转换已删除。自定义注册表遗漏
  `protocol` 时，仅在上游原生支持 Responses API 的前提下补入该字段；旧 Chat/Anthropic
  协议配置需要迁移至兼容的 Responses 服务，不能只改协议名。
- 检查：`node --test test/official-catalog.mjs`、
  `node --test test/router-fixes.mjs test/response-usage-hardening.mjs test/http-health-bounds.mjs test/credential-file-security.mjs test/graceful-shutdown.mjs test/upstream-hardening.mjs test/windows-service-process.mjs test/service-command.mjs test/gpt-route-async.mjs test/windows-hidden-consoles.mjs test/start-proxy.mjs test/auto-route.mjs` 和
  `node scripts-check.mjs`。
- 旧聊天仍报 WLB：若会话绑定第三方 provider，或错误 URL 直接指向第三方，切换 GPT
  模型或 Router 的 `route` 不会迁移该会话的 provider。请使用官方 OpenAI provider
  新建通过本地 Router 的聊天，再切换路由；直接请求第三方的会话不经过 Router 回退。
- 请求级调试：使用 `CODEX_ROUTER_REQUEST_LOG=1` 启动；HTTP/WS caller capability
  会自动脱敏，仍不要分享包含私有路径的日志。
- WLB 额度耗尽后默认立即转官方；可通过 `CODEX_ROUTER_WLB_FALLBACK_DELAY_MS`
  设置等待时间（0 至 30000 毫秒，非整数或越界值默认 0）。客户端取消会终止等待；
  路由器关闭后不会再向官方重发。这是路由器内的切换等待，Codex 客户端自身的重连
  间隔保持原设置。修改源码后需重启路由服务才能启用新行为。
- 响应流及错误体、压缩、协作 relay 的缓冲读取默认允许 300 秒无数据；可用
  `CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS` 调整（10 毫秒至 15 分钟）。持续有数据会重新计时。

## 分支

- `main` — 未修改的上游快照
- `lite` — 当前精简版本
