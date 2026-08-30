# Codex Router Lite

这是 [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router)
的精简分支，只做一件事：把原生 GPT 和两个兼容 Responses API 的第三方服务商加入
Codex 的模型选择器，并在本地路由它们的请求。

合并后的模型目录固定包含 8 个条目：

- 原生 GPT-5.6（ChatGPT 登录）— `gpt-5.6-sol`、`gpt-5.6-terra`、
  `gpt-5.6-luna`
- **MiMo** — `mimo-token-plan/mimo-v2.5-pro`、
  `mimo-token-plan/mimo-v2.5`
- **WLB 中继** — `wlb-relay/gpt-5.6-sol`、`wlb-relay/gpt-5.6-terra`、
  `wlb-relay/gpt-5.6-luna`

其中 5 个带命名空间的条目由路由器转发（2 个 MiMo、3 个 WLB）；3 个原生条目
继续使用 Codex 的 ChatGPT 后端。

## 从上游移除了什么

已移除两个服务商不需要的全部组件：LiteLLM 及其他网关或转发层、Python、OAuth
流程、服务商预设、托盘/桌面应用和自动更新器。服务仅依赖 Node：`src/start.mjs`
监督一个 `src/router.mjs` 子进程，不再启动额外的网关或转发进程。收到重启信号时，
路由器先停止接收新请求，在途请求最多 drain 2 秒；流式请求收到 terminal error 后
clean EOF，未发出响应头的请求返回 503。可用 `MODEL_ROUTER_SHUTDOWN_DRAIN_MS`
调整 drain 时长。仓库保留的检查脚本是 `test/catalog-metadata.mjs`、
`test/router-fixes.mjs`、`test/response-usage-hardening.mjs`、
`test/http-health-bounds.mjs`、`test/credential-file-security.mjs`、
`test/graceful-shutdown.mjs`、`test/upstream-hardening.mjs`、
`test/windows-service-process.mjs` 和 `scripts-check.mjs`。

## 架构

两个第三方服务商都原生支持 Responses API，因此请求链路只有：

```
Codex ──(config.toml: openai_base_url + model_catalog_json)──▶ router.mjs
    ├─ 原生 slug / 搜索 / 图片 → ChatGPT Codex 后端（透传 Codex 身份验证）
    ├─ mimo-token-plan/*    → MiMo Token Plan（注入 MIMO_API_KEY）
    └─ wlb-relay/*          → WLB Relay（注入 WLB_API_KEY）
```

- **注入模型目录**：`src/catalog.mjs` 捕获 Codex 原生模型目录，只输出 3 个官方
  GPT-5.6 原生 slug，再与 `config/` 合并，并写入
  `~/.codex/codex-router/merged-models.json`。完整的原生捕获仍保留，供 WLB 精确
  查找模板。
- **模型元数据**：每个 WLB 条目精确复制其 `upstreamModel` 指定的原生条目，只修改
  带命名空间的 slug 和 WLB 显示名；找不到对应原生条目时构建会失败。MiMo 条目只
  使用 `src/catalog.mjs` 中明确列出的 Xiaomi 字段，不继承 GPT 元数据。
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
- **代理**：原生上游请求通过 `NODE_USE_ENV_PROXY` 使用 `HTTPS_PROXY`（默认
  `http://127.0.0.1:7897`，即 Clash 混合端口）；WLB 和 MiMo 域名默认加入
  `NO_PROXY` 并保持 DIRECT。

## 安装与日常使用（Windows）

```powershell
.\codex-router.ps1 install                    # 安装依赖、密钥、目录和服务
.\codex-router.ps1 provider-key mimo-token-plan set
.\codex-router.ps1 provider-key wlb-relay set
.\codex-router.ps1 enable
.\codex-router.ps1 start
.\codex-router.ps1 disable
.\codex-router.ps1 uninstall
.\codex-router.ps1 status                       # 只读检查路由器、配置、目录和服务
```

POSIX：`bin/install`（兼容入口 `install.sh`）、`bin/provider-key`、`bin/enable`、`bin/disable`、
`bin/uninstall`、`bin/start`。状态检查使用
`bin/model-router codex status`。状态命令只读取本地健康端点和配置，不会自动修复
或请求上游服务；它只有在路由器健康、配置由本安装管理、目录精确包含 8 个模型
（其中 5 个为路由模型）、选定 Provider 未降级且凭据已配置时才报告 ready。返回码为
0 表示已就绪，1 表示需要处理，2 表示参数错误；需要脚本处理时可追加 `--json`。

状态保存在 `~/.codex/codex-router/`（模型目录、密钥和日志）。Windows 通过名为
“Codex Router”的计划任务在登录时自动启动服务。后台监督进程会登记
`service.pid`；停止或重启时会校验 Node 路径和完整的 `src/start.mjs` 命令行后再
结束该进程树，避免旧路由器继续占用 4102 端口。

另一台设备不应复制上述状态目录。拉取 `lite` 后，先在该设备分别运行
`.\codex-router.ps1 provider-key <provider> set` 配置要使用的服务商，再从当前
checkout 运行 `.\codex-router.ps1 install`；安装器会确保本机 caller secret、重建
模型目录、刷新 Codex endpoint，并登记 Windows 计划任务。Windows 目前不支持从
另一 checkout 无缝接管已有 state owner；请继续从原 checkout 更新，或先人工解决
owner 冲突。安装完成后完全退出并重新打开 Codex，使新的本地 endpoint 生效。
`node src/service.mjs restart` 只能重启已经登记的任务；如果 `schtasks /Run` 报错，
请重新执行 install，Windows 策略拒绝任务登记时改用管理员 PowerShell。

原生 Web Search 和图片请求必须携带当前 ChatGPT/Codex 会话的 `Authorization`；缺少
该请求头时路由器直接返回 `401`，不会访问原生上游。

## 维护

- 更新官方模型：运行 `node src/catalog.mjs`（Codex CLI 版本变化时会自动重新
  捕获），重载路由器进程；如果模型选择器仍显示旧目录，再重启 Codex。
- 添加或修改路由模型：编辑 `config/<provider>/models.json`，运行
  `node src/catalog.mjs`，再用 `node src/service.mjs restart` 重载路由器；模型
  选择器需要重新载入时再重启 Codex。
- 检查：`node test/catalog-metadata.mjs`、
  `node --test test/router-fixes.mjs test/response-usage-hardening.mjs test/http-health-bounds.mjs test/credential-file-security.mjs test/graceful-shutdown.mjs test/upstream-hardening.mjs test/windows-service-process.mjs` 和
  `node scripts-check.mjs`。
- 请求级调试：使用 `CODEX_ROUTER_REQUEST_LOG=1` 启动；HTTP/WS caller capability
  会自动脱敏，仍不要分享包含私有路径的日志。
- 流式响应默认允许 300 秒无数据；可用 `CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS` 调整（10 毫秒至 15 分钟）。

## 分支

- `main` — 未修改的上游快照
- `lite` — 当前精简版本
