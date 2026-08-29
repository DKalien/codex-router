# 安全指南

Codex Router Lite 是一个本地 Node 服务。它只有一个路由器进程，将 Responses API
请求直接转发给选定的上游，不包含 LiteLLM、转发器、Python、OAuth 或托盘应用。

## 凭据

- 只路由 `mimo-token-plan` 和 `wlb-relay` 两个服务商。分别使用
  `provider-key mimo-token-plan set` 和 `provider-key wlb-relay set` 设置密钥。
- 受保护的密钥文件默认位于
  `~/.codex/codex-router/mimo-api-key.secret` 和
  `~/.codex/codex-router/wlb-api-key.secret`。可以通过
  `CODEX_ROUTER_STATE_DIR` 或 `MODEL_ROUTER_STATE_DIR` 指定其他状态目录。
- 只从普通文件读取持久化密钥；符号链接、目录和读取失败的候选会被忽略。文件权限
  仅授予当前用户：POSIX 使用 `600`，Windows 使用非继承 DACL，且只保留当前用户的
  `FullControl` Allow 规则。前台运行可以使用环境变量；后台服务应使用受保护的密钥文件。
- 密钥只用于创建上游 `Authorization` 请求头，不会写入模型目录、Codex 配置或
  日志。绝不能提交或粘贴密钥、完整的 caller URL 或真实状态目录中的文件。
- 路由请求不会向第三方服务商转发 Codex 传入的账户或身份验证请求头。原生 GPT
  请求只使用原生后端所需的 Codex 白名单请求头。

MiMo 长会话中的 custom tool 和协作历史会先在本地转换为其支持的 Responses 项。
标准协作 envelope 中的旧 `MESSAGE` 进度不会发送给 MiMo；保留的任务/最终协作载荷若是原生
`encrypted_content`，会先通过原生 Codex 后端的受控 function call 解密。明文仅在
当前路由器进程的 LRU 缓存中使用（逻辑 TTL 24 小时，最多 512 条或 8 MiB），不会
主动持久化到磁盘或日志；选择 MiMo 仍表示保留的提示词和协作结果会发送给该第三方
服务商。

状态目录还包含 `caller-secret`，用于验证 Codex 访问本地回环地址的权限，与服务商
密钥相互独立，也必须视为敏感信息。同一操作系统用户下运行的进程通常可以读取该用户
的 Codex 配置和状态，因此这些措施不能防御同用户权限下的恶意程序。

## 网络和代理边界

路由器默认只监听回环地址（除非另行配置，端口为 `4102`）。不得改为公开监听地址、
建立隧道或暴露到共享网络。读取或转发模型请求前，路由器会先验证 caller 凭据。

配置中的服务商 URL 使用 HTTPS，路由器直接调用各服务商的 `/responses` 端点。
应保持正常的 TLS 验证，并且只信任自己管理的代理。`src/start.mjs` 设置
`NODE_USE_ENV_PROXY=1` 并传递 `HTTP_PROXY`/`HTTPS_PROXY`；代理默认是
`http://127.0.0.1:7897`，`NO_PROXY` 则包含回环地址。如果代理终止 TLS，它可以
观察或修改流量，因此应检查代理配置和日志。

请求日志默认关闭。`CODEX_ROUTER_REQUEST_LOG=1` 会记录请求方法、URL、状态码和
耗时，用于排查问题；HTTP 和 WebSocket URL 中的 caller capability 会自动替换为
`[REDACTED]`，分享日志前仍应检查私有路径。路由器错误不会记录请求正文、响应正文
或服务商密钥，但日志仍可能包含模型名称和路径。

普通路由请求的第三方上游错误体最多读取 64 KiB，且只在返回前处理；Bearer、token、key、secret、
caller capability、查询参数和控制字符会被遮盖或清理，quoted JSON 字段会保留合法
结构，错误详情还会限制长度。这样既避免巨型错误体占用内存，也避免上游凭据进入
Codex 响应。

请求正文达到配置上限后不再保留后续字节，但会排空请求以保持 HTTP framing，并可由
客户端断开信号终止。协作载荷 relay 和 compact 响应分别最多缓冲 4 MiB 与 32 MiB；
超限时立即取消上游流，避免由上游控制的响应无界占用内存。

## 支持的运行环境和更新

按照 `package.json` 的声明使用 Node.js `>=22.19.0`，并保持锁文件同步。修改路由
模型后，运行 `node src/catalog.mjs`，重载路由器进程；如果模型选择器尚未刷新，再
重启 Codex。分发变更前，必须运行仓库内的元数据和语法检查。

## 报告安全漏洞

请使用 [GitHub 私密漏洞报告](https://github.com/duolahypercho/codex-router/security/advisories/new)。
报告应包含路由器修订版本、操作系统、Node.js 版本和 Codex CLI 版本，以及最小复现
步骤和已脱敏日志。不要在 issue 中提供 API 密钥、caller URL、凭据文件、提示词、
响应正文或未经脱敏的状态目录内容。
