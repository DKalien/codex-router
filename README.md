# Codex Router Lite

这是 [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router)
的精简分支：Codex 保持使用官方模型目录，本地路由器只负责把请求转发到官方
Codex 后端或已配置的第三方服务商。

默认路由是 `official`。对官方目录中的 GPT 模型，主代理遵循全局 `official`/`wlb`
开关，已识别的原生 `gpt-*` 子代理固定走官方。选择器选中的 `gpt-*` 模型名原样传给
WLB，新增模型无需修改本地名单，实际可用性取决于 WLB 上游支持。全局设置不绑定
线程，从下一次非子代理的原生 GPT 请求起对所有任务共享；同一任务中途切换服务商可能带来
历史上下文兼容风险。MiMo 和 WLB 的旧命名空间 slug 仍保留给旧目录或直接请求
使用，但不会显示在官方模型选择器中。

默认安装不写入 `model_catalog_json`，也不构建静态模型目录；Codex 的官方目录由
Codex 自己更新。`src/catalog.mjs` 仍保留为需要旧命名空间目录时的手动工具。

## 从上游移除了什么

已移除两个服务商不需要的全部组件：LiteLLM 及其他网关或转发层、Python、OAuth
流程、服务商预设、托盘/桌面应用和自动更新器。服务仅依赖 Node：`src/start.mjs`
监督一个 `src/router.mjs` 子进程，不再启动额外的网关或转发进程。收到重启信号时，
路由器先停止接收新请求，在途请求最多 drain 2 秒；流式请求收到 terminal error 后
clean EOF，未发出响应头的请求返回 503。可用 `MODEL_ROUTER_SHUTDOWN_DRAIN_MS`
调整 drain 时长。仓库保留的检查脚本是 `test/catalog-metadata.mjs`、
`test/official-catalog.mjs`、
`test/router-fixes.mjs`、`test/response-usage-hardening.mjs`、
`test/http-health-bounds.mjs`、`test/credential-file-security.mjs`、
`test/graceful-shutdown.mjs`、`test/upstream-hardening.mjs`、
`test/windows-service-process.mjs` 和 `scripts-check.mjs`。

## 架构

两个第三方服务商都原生支持 Responses API，因此请求链路只有：

```
Codex ──(官方模型目录 + config.toml: openai_base_url)──▶ router.mjs
    ├─ 已识别的 GPT 子代理      → ChatGPT Codex 后端（透传 Codex 身份验证）
    ├─ GPT 主代理 + route official → ChatGPT Codex 后端（透传 Codex 身份验证）
    ├─ GPT 主代理 + route wlb      → WLB Relay（注入 WLB_API_KEY）
    ├─ mimo-token-plan/*         → MiMo Token Plan（旧命名空间，注入 MIMO_API_KEY）
    └─ wlb-relay/*               → WLB Relay（旧命名空间，注入 WLB_API_KEY）
```

- **官方目录**：默认安装会移除本安装维护的 `model_catalog_json`，让 Codex 使用
  官方目录并自行更新。需要旧目录时可手动运行 `node src/catalog.mjs`；这不会改变
  默认安装路径。
- **GPT 路由开关**：`route official|wlb|status` 写入或读取全局路由状态，切换从下
  一次非子代理的原生 GPT 请求生效，不需要重启服务或 Codex。`wlb` 按请求中的 GPT 模型名直接转发，
  不依赖本地模型名单；上游不支持时仍返回其错误。明确报告额度耗尽时，路由器自动保存为
  `official`，默认等待 5 秒后用原始 Codex 身份继续当前请求，后续 GPT 主代理请求也走官方。
  普通限流、认证失败、套餐不包含 API 和已开始输出的流不会触发切换；缺少官方身份或
  无法保存路由状态时不会回退。切换覆盖普通请求和压缩请求，显式 `wlb-relay/*` 保持原语义。
  额度恢复后需手动运行 `route wlb` 切回。`route status` 只读；只有总状态命令支持
  `--json`（`status --json`）。该开关不绑定线程，同一任务中途切换服务商可能带来
  历史上下文兼容风险。
- **GPT 子代理**：已识别的原生 `gpt-*` 子代理固定走官方，普通请求和压缩请求
  共用规则，且不读取全局路由状态，因此损坏的路由状态也不会阻断已识别的子代理。
  识别复用 `x-openai-subagent`、有效的 `x-codex-parent-thread-id`，或线程 rollout 的
  `source.subagent.thread_spawn`；明确的 `false`/`0` 子代理头不作为证据。rollout 缺失
  或首行未写完时会在下次请求重试；没有可识别证据的请求仍按主代理请求遵循全局开关。
  此规则只适用于原生 `gpt-*`，显式 `wlb-relay/*` 和 MiMo 路由继续遵循原命名空间。
- **旧命名空间路由**：WLB 的 `wlb-relay/gpt-5.6-sol`、`terra`、`luna` 与官方
  GPT slug 一一对应；MiMo 的两个命名空间 slug 继续由原路由处理，但两者都不进入
  官方选择器。
- **旧目录元数据**：手动构建旧目录时，WLB 条目以捕获的官方目录中其
  `upstreamModel` 对应条目为元数据基线，保持字段和能力描述一致；这不保证 WLB
  上游实际支持官方目录声明的全部能力。找不到对应官方条目时构建会失败。MiMo
  条目只使用 `src/catalog.mjs` 中明确列出的 Xiaomi 字段。
- **官方模型列表**：`/models` 和 `/v1/models` 复用原生转发并保留查询参数及官方
  完整响应；WLB 路由也不会把模型列表替换成本地简化目录。没有当前 Codex 登录身份
  时返回 `401`。
- **原生请求**：独立的 `/alpha/search` Web Search 和图片请求只转发给原生
  Codex 后端；上游省略 `content-type` 时，即使 SSE 字段被拆到多个 chunk，或前面
  带 BOM、注释和空行，路由器也能识别并统计 Token，同时保持原始字节不变。
  原生流在 `response.completed` 后才断开时仍按 upstream status/usage 记录，不再误记
  为取消或 `0`；原生 GPT-5.6 请求会移除旧兼容字段 `prompt_cache_retention`，保留
  `prompt_cache_options`。
- **MiMo 长会话兼容**：Codex 的 custom tool 会在请求侧桥接为普通 function tool，
  JSON/SSE 响应再还原成 custom tool 事件。跨模型续接会保留已经验收的
  `FINAL_ANSWER`；标准协作 envelope 中的旧 `MESSAGE` 进度不发送给 MiMo，
  `NEW_TASK` 和 `FOLLOWUP_TASK` 仍保留。第三方路由必须解析原生加密协作载荷时，
  会按原顺序最多 4 路并发；解密结果保存在进程内 LRU 缓存，逻辑 TTL 为 24 小时，
  最多 512 条或 8 MiB。路由器估算第三方输入 Token 时不再把不会发送给模型的
  `encrypted_content` 密文计入提示词大小。切回原生 GPT 时，没有原生加密上下文的
  第三方 reasoning 会被丢弃，不会作为未持久化的 `rs_*` item 继续回放。
- **错误和日志安全**：请求日志默认关闭；启用后会自动遮盖 HTTP/WS caller URL
  中的 capability。普通路由请求的第三方上游错误体最多读取 64 KiB，返回前会遮盖 Bearer、token、
  key、secret、caller capability、查询参数和控制字符；可解析的 quoted JSON 字段仍保留
  合法的 JSON 结构。请求正文超限后会停止缓存、排空余流，并允许客户端断开信号
  终止读取；协作载荷 relay 和 compact 响应分别最多缓冲 4 MiB 与 32 MiB。
- **凭据文件**：持久化密钥只从普通文件读取，符号链接和目录会被忽略。Windows
  写入时会用仅包含当前用户 `FullControl` 的非继承 DACL 替换旧 ACL。
- **代理**：监督进程启动时探测本机代理端口（v2rayN `10808`/`10809` 优先，兼容
  Clash `7897`/`7890`，`CODEX_ROUTER_PROXY_PORTS` 可覆盖，显式 `HTTPS_PROXY` 优先；
  探测不到则原生路由直连），再经 `NODE_USE_ENV_PROXY`/`HTTPS_PROXY` 注入路由器。
  WLB 和 MiMo 域名固定加入 `NO_PROXY` 并保持 DIRECT，不依赖代理是否在线。监督
  进程内的看门狗每 12 秒复测：代理在启动后才出现或换到其他候选端口时，先写
  `shutdown.request` 优雅排空再带新代理环境重启路由器子进程；代理消失不触发
  重启；显式设置 `HTTPS_PROXY` 时看门狗停用。

## 安装与日常使用（Windows）

```powershell
.\codex-router.ps1 install                    # 安装依赖、密钥、路由器和服务；默认使用官方目录
                                                # 仅使用官方路由时无需第三方密钥
.\codex-router.ps1 provider-key mimo-token-plan set  # 使用 MiMo 时再配置
.\codex-router.ps1 provider-key wlb-relay set        # 使用 WLB 时再配置
.\codex-router.ps1 route official             # 全局：GPT 主代理请求走官方（默认）
.\codex-router.ps1 route wlb                  # 全局：GPT 主代理请求走 WLB；原生子代理固定官方
.\codex-router.ps1 route status               # 查看当前 GPT 路由
.\codex-router.ps1 enable
.\codex-router.ps1 start
.\codex-router.ps1 disable
.\codex-router.ps1 uninstall
.\codex-router.ps1 status                     # 只读检查路由器、配置、Provider 和服务
```

POSIX：`bin/install`（兼容入口 `install.sh`）、`bin/provider-key`、`bin/enable`、`bin/disable`、
`bin/uninstall`、`bin/start`。路由切换使用
`bin/model-router codex route official|wlb|status`；状态检查使用
`bin/model-router codex status`。这些命令只读取或更新本地状态，不会请求上游服务；
官方模式的状态检查不依赖静态 8 模型目录。返回码为 0 表示已就绪，1 表示需要处理，
2 表示参数错误；只有总状态命令支持 `--json`，即 `status --json`。`route status`
为只读文本查询，不接受 `--json`。

状态保存在 `~/.codex/codex-router/`（路由状态、密钥和日志；旧目录为可选文件）。Windows 通过名为
“Codex Router”的计划任务在登录时自动启动服务。后台监督进程会登记
`service.pid`；停止或重启时会校验 Node 路径和完整的 `src/start.mjs` 命令行后再
结束该进程树，避免旧路由器继续占用 4102 端口。Windows 下停止先写入
`shutdown.request` 请求路由器排空退出（在途请求按重启 drain 规则收尾），超时才强杀；
`start`/`restart` 会等待 `/health` 通过后才返回。

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
- 维护旧命名空间目录：手动运行 `node src/catalog.mjs`；如修改了
  `config/<provider>/models.json`，再运行同一命令并按需要重载路由器。旧目录构建
  失败时，WLB 模型必须能精确找到对应的官方 `upstreamModel`。
- 切换 GPT 路由：运行 `route official` 或 `route wlb`；设置从下一次非子代理的原生 GPT 请求生效，
  对所有任务共享且无需重启；已识别的原生 GPT 子代理固定走官方。运行 `route status` 可只读查看当前设置；同一任务中途
  切换服务商可能带来历史上下文兼容风险。
- 检查：`node test/catalog-metadata.mjs`、
  `node --test test/official-catalog.mjs`、
  `node --test test/router-fixes.mjs test/response-usage-hardening.mjs test/http-health-bounds.mjs test/credential-file-security.mjs test/graceful-shutdown.mjs test/upstream-hardening.mjs test/windows-service-process.mjs` 和
  `node scripts-check.mjs`。
- 请求级调试：使用 `CODEX_ROUTER_REQUEST_LOG=1` 启动；HTTP/WS caller capability
  会自动脱敏，仍不要分享包含私有路径的日志。
- WLB 额度耗尽后转官方的等待默认是 5 秒；可通过 `CODEX_ROUTER_WLB_FALLBACK_DELAY_MS`
  调整（0 至 30000 毫秒，非整数或越界值使用默认）。客户端取消会终止等待；
  路由器关闭后不会再向官方重发。这是路由器内的切换等待，Codex 客户端自身的重连
  间隔保持原设置。修改源码后需重启路由服务才能启用新行为。
- 流式响应默认允许 300 秒无数据；可用 `CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS` 调整（10 毫秒至 15 分钟）。

## 分支

- `main` — 未修改的上游快照
- `lite` — 当前精简版本
