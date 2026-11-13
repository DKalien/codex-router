# Router 请求与响应转换审计

本审计对应 GitHub issue #5，覆盖 `lite` 的 Codex 主代理、子代理、Pi、手动和自动路由、
普通 Responses、compact v1 与 `compaction_trigger` v2。本地回归使用公开合成数据、假密钥和
HTTP mock server。2026-11-13 完成一轮 WLB / SuperGPT 低成本真实端点 A/B，
使用受保护凭据和短合成 fixture，结果与未完成项目见本文末尾的线上验证矩阵。

## 默认路径与兼容配置

第三方 `protocol: openai-responses` 的普通请求默认保留 JSON 字段、input 顺序、工具关联、
reasoning 和未知字段。模型按路由映射，查询参数保留。请求头只转发 `Accept`、
`OpenAI-Beta`、`User-Agent`、`X-Client-Request-Id`、`Idempotency-Key`，认证使用目标
供应商密钥。客户端的官方身份、账户、组织、项目、Cookie 和安装信息继续隔离。

供应商片段中的可选 `compatibility` 对象逐项启用兼容行为，缺省值均为 `false`。
未知配置项或非 boolean 值在加载注册表时拒绝。修改后需重启 Router。

| 配置项 | 转换及理由 | 当前启用范围 | 证据与限制 |
| --- | --- | --- | --- |
| `orphanToolOutputs` | 缺少非空字符串 `call_id` 的工具结果折算为用户消息，保留 namespace/name/output | WLB、SuperGPT | `7f8719f5` 记录严格上游的 `requires call_id` 错误，`9e634821` 固定 Pi 和配对边界。2026-11-13 两家真实端点对孤立 fixture 均返回 400，经 Router 折算后均成功。属于历史上下文降级，非空但未配对的 ID 继续保留 |
| `omitClientMetadata` | 删除 `client_metadata` | WLB、SuperGPT | `67da2a9f3` 的严格上游兼容记录。2026-11-13 两家均接受小型 metadata fixture。现有开关保留，待代表性 metadata 样本及独立 Review 后再决定是否关闭 |
| `omitReasoningParameters` | 删除顶层 `reasoning` 与 `reasoning_effort`，保留 input 中的 reasoning item | 均关闭，按供应商显式启用 | 旧 keyless 普通请求曾自动剔除，原始 `997710d9` 注释针对 LiteLLM Ollama 路径。严格本地 mock 可复现字段存在时 400、剔除后 200，实际 Ollama / llama.cpp 版本支持情况待验证 |
| `relayAgentPayloads` | 仅在识别到协作 envelope 时将 encrypted_content 转为可读 input_text，官方 token 需经官方 function-call relay | WLB、SuperGPT | 现有 `agent_message relay` 回归覆盖识别、顺序、有界并发和缓存。用于官方与第三方混合协作，未启用时原样保留供应商自己的协作项 |
| `summarizeCompaction` | compact v1/v2 调用普通 `/responses`，强制流式摘要，返回 Router 管理的替换历史 | WLB、SuperGPT | `83b340dc` 记录 SuperGPT 非流式错误及缓冲超时。2026-11-13 两家摘要 v1/v2 均通过，SuperGPT 非流式返回 200 + SSE server_error 得到复现；两家原生 compact 探针均返回 502，能力结论仍待确认 |
| `estimateZeroInputTokens` | 大请求明确报告零输入 Token 时将估算值写回 JSON/SSE usage | 均关闭 | `9478c4bc` 的原始证据针对已退役 OpenCode Go。仅显式启用时生效，`CODEX_ROUTER_ZERO_INPUT_ESTIMATE=0` 仍可强制关闭 |
| `translateErrors` | 将上游错误转换为面向 Codex 的供应商错误提示 | WLB、SuperGPT | 保留现有错误格式与额度分类回归。关闭时保留标准 JSON error 的字段结构并脱敏，非标准错误继续安全封装 |

## 全部转换点

| 位置 | 触发条件与行为 | 保留理由及关闭转换的影响 |
| --- | --- | --- |
| `decodeBody` / `parseBody` | 有界解压请求并解析 JSON 对象 | 路由选择与输入边界校验需要读取模型，输出 JSON 的空白及键顺序可能变化，JSON 语义保留 |
| `routeForMode` / 模型注册表 | 目标 URL、供应商认证、旧 slug 到 upstreamModel | 路由基本操作，原生 `gpt-*` 名称保持原样，查询参数不丢弃 |
| `routedHeaders` / `nativeHeaders` | 第三方使用协议头白名单，官方使用 Codex 头白名单 | 认证与账户隔离，禁止把官方身份转发给第三方，未知请求头不自动放行 |
| `normalizeRoutedInput` 的工具结果处理 | 仅 `orphanToolOutputs` 开启时折算 | 适用范围与配对限制见上表 |
| `normalizeRoutedInput` 的摘要处理 | 仅解开 Router 自有 `kcr1:` 标记 | 兼容既有会话。未知供应商 compaction 保留原项，上游拒绝时显式报错，避免占位文本替换有效历史 |
| 旧空消息过滤 | 已移除 | 原始注释及 `06064fc14` 指向已移除的 LiteLLM，当前两家缺少继续过滤的证据 |
| `normalizeRoutedAgentInput` | 仅供应商显式开启 `relayAgentPayloads` | 标准连续会话保留协作原项，跨供应商处理遵循明确的 envelope 与 token 形状 |
| `applyRoutedParameterCompatibility` | 普通、原生 compact 及摘要请求共用供应商级顶层参数处理 | `omitClientMetadata` 和 `omitReasoningParameters` 各自独立控制，推理参数剔除仅在显式开启时应用 |
| `normalizeNativeInput` | 回到官方时恢复 kcr1 摘要、处理外部协作项，丢弃不符合原生 token 形状的 reasoning，移除合法 reasoning 的输出专用 content | 既有原生回放契约。token 形状检查不能证明密文的真实发行来源，跨供应商不透明密文仍可能被上游拒绝 |
| `normalizeNativePromptCacheCompatibility` | 官方 GPT-5.6 删除 prompt_cache_retention | 既有客户端兼容回归，prompt_cache_options 保留 |
| Pi 的两个兼容函数 | 非 sk- 官方请求删除订阅不支持的参数，订阅或第三方请求将 configuration_update 折回 reasoning.effort | Pi 客户端契约，官方 API key 原样保留，Pi 路由状态独立 |
| `handleResponses` 的 previous_response_id | auto 请求删除该字段，普通官方重建请求也删除，手动第三方保留 | 维持现有全历史重放与自动切换契约，服务端 response ID 无法保证跨供应商复用 |
| `summarize` / `handleRoutedCompaction` | 仅显式开启摘要兼容。去掉 compaction_trigger，追加摘要提示，设置 stream=true / tools=[]，删除 previous_response_id，按配置处理 client_metadata | 成功摘要才生成替换历史。空、失败、不完整及过大响应拒绝；成功 v1 保留有限用户消息与摘要，v2 生成 kcr1 项，均会改变历史结构 |
| 原生第三方 compact | 未开启摘要兼容时保留端点、请求字段及响应字节，先有界验证结果 | v1/v2 均要求有效 compaction 输出。失败、不完整、空结果返回 502，成功 SSE 需等待验证完成后交付 |
| `ResponseUsageTransform` | 默认只观察并保持响应字节，零值补偿按供应商显式启用 | 用量统计可独立工作，缓存上限和跨 chunk 解析保留。开启补偿会改变 usage 与相关 JSON/SSE 序列化 |
| `writeRoutedError` | 非成功第三方响应限读 64 KiB，保留 Retry-After，按配置翻译或脱敏标准 error | 规范化 JSON 转义后，对本次请求使用的供应商密钥进行精确脱敏，并沿用敏感字段与 Bearer 脱敏。标准 error 的未知字段可保留，非标准错误仍封装 |
| `pipeResponse` / SSE / fetch | Hop-by-hop 头处理、拒绝重定向、闲置超时、取消、重试与输出边界检查 | 传输及安全保护。auto 每家至多一次，既有手动重试规则继续生效 |

原生 compact 支持独立于普通 Responses 支持。当前两家继续显式启用摘要兼容，原生 compact
路径由本地标准协议 fixture 验证，真实供应商能力仍待独立确认。

## Keyless 配置升级

`keyless: true` 规定回环目标与免真实密钥认证。凭据解析直接返回 `local` 占位值，
请求使用 `Bearer local`，参数能力需要另行确认。此前普通 keyless 请求会自动删除两个
顶层推理参数，摘要路径当时仍保留这些参数。透明转发改动使普通请求也默认保留参数，
因此依赖旧剔除行为的严格上游存在升级回归风险。历史代码记录的失败来自已移除的
LiteLLM 路径，当前直接 Responses 实现的能力需要按版本和端点核实。

旧自定义 keyless 配置升级前应显式选择参数策略。需要恢复旧普通请求的参数剔除时，
在该供应商片段中合并以下配置。此开关也应用于原生 compact 和 Router 的摘要请求。

```json
{
  "compatibility": {
    "omitReasoningParameters": true
  }
}
```

确认上游支持这些参数时显式设为 `false`。缺省保持透明转发，Router 启动时为尚未设置
该项的每个 keyless 供应商输出一次 `keyless reasoning migration` 提示，静默日志模式下
同样显示。提示只含供应商 ID 和迁移选项。显式选择任一 boolean 值后该提示解除。
配置由用户维护并经重启加载。该能力按参数命名，拥有凭据的供应商也可以按需显式启用。

## 本地 A/B 与回归验证

`test/fixtures/responses-transparency.json` 固定顶层未知字段、client_metadata、服务端状态 ID、
合法及未配对工具结果、孤立工具结果、空消息、reasoning、未知 input、供应商摘要与协作项。
`test/router-fixes.mjs` 将同一请求直接发给 mock server，再经 Router 发给同一 server，比较
实际收到的 URL、协议头、JSON 和客户端收到的 JSON/SSE 字节。覆盖两家配置 ID、主代理、
子代理、Pi、普通 JSON/SSE、compact v1/v2。真实供应商对各类输入的接受情况仍待在线验证。

兼容项另行验证逐项启停、无效配置拒绝、错误脱敏、零 Token 补偿开关、未知压缩项保留、
自有摘要恢复与原生压缩失败保护。原有官方回放、跨供应商协作、WLB 回退、auto 三级切换、
Pi 独立状态、凭据隔离、限读、重定向和 SSE 测试继续作为门禁。

Keyless 专项复用同一公开 fixture，使用临时注册表、临时状态目录及回环 HTTP server，
覆盖未配置、空 compatibility、显式 false/true、严格/宽松上游、普通 JSON/SSE、Pi、
原生 compact v1/v2 及摘要兼容。验证仅删除两个顶层字段、其余字段和 input 保留、
成功响应字节一致、400 单次返回、迁移提示及官方参数透传。有凭据的 WLB / SuperGPT
兼容项矩阵同时验证缺省保留与显式剔除。schema 专项拒绝新开关的字符串、数字和 null。

## 2026-11-13 真实供应商 A/B

### 隔离、凭据与预算

两家均通过现有受保护凭据接口读取 Key，目标校验匹配仓库默认 HTTPS 地址。
账号用量与模型列表各查询一次，均返回 200。WLB 额度可用，SuperGPT 余额为正。
按模型列表匹配结果，WLB 使用 `gpt-5.6-luna`，SuperGPT 使用 `gpt-5.6-sol`；
每个场景的两侧使用同一供应商、同一模型和同一份受保护凭据。

Router 测试实例直接运行当前源码，使用临时 registry、状态目录、回环端口与 caller capability。
供应商 Key 只通过子进程临时环境传给现有凭据接口。请求正文均为短公开 fixture，
客户端身份字段为合成占位值。正式状态目录的主代理、子代理、Pi、供应商选择、
service PID 和安装清单在两轮探针前后校验一致。HTTP 重定向被拒绝，自动重试关闭。
官方测试目标限制到本地，真实调用只使用所选供应商。

普通请求设置 `max_output_tokens=32`，摘要设置 128。基础成功请求均报告 5 输出 Token，
直连摘要报告 10 至 44 输出 Token。原生 compact 使用 `model`、`input`、`instructions`
构成的极小上下文，按官方字段定义构造。各请求有 25 秒总 deadline 和 64 KiB 客户端读取边界。
原生 compact 的输出 Token 上限未由请求参数控制，本轮各调用一次即返回 502。
原生请求字段依据见 [OpenAI compact API 参考](https://developers.openai.com/api/reference/resources/responses/methods/compact)。

共执行 30 次真实 Responses 类请求，另有 4 次只读账号/模型查询。原始响应只在内存解析，
日志仅包含短 fixture 名称、脱敏状态/错误字段、输出 Token 数和预期转换字段名。
供应商服务端的收包内容不可见；请求语义、头白名单、认证隔离和字节级透传的精确断言
来自本地 mock 回归。真实模型输出只比较完成状态与结构。

### 场景矩阵

表中每个格子按「直连 / 隔离 Router」列出。200 成功要求取得完成响应及有效文本、
compaction 或替换历史结构。原生 compact 的 5xx 触发供应商停止条件，后续场景记录为阻塞。

| 场景 | WLB | SuperGPT |
| --- | --- | --- |
| `base-json` | 200 JSON 完成 / 200 JSON 完成 | 200 + SSE `server_error` / 同样的 SSE 错误，`code/type=server_error` |
| `base-sse` | 200 完成 / 200 完成 | 200 完成 / 200 完成 |
| `metadata` | 200 / 200，Router 删除 client_metadata | 200 / 200，Router 删除 client_metadata |
| `orphan` | 400，`code=sub2_request_rejected`、`type=invalid_request_error` / 200 | 400，`type=invalid_request_error` / 200 |
| `paired` | 200 / 200，合法工具配对保留 | 200 / 200，合法工具配对保留 |
| `compact-v1-summary` | 200 SSE 摘要 / 200 JSON 替换历史 | 200 SSE 摘要 / 200 JSON 替换历史 |
| `compact-v2-summary` | 200 SSE 摘要 / 200 SSE kcr1 compaction | 200 SSE 摘要 / 200 SSE kcr1 compaction |
| `compact-v1-native` | 直连 502，`code=sub2_unavailable`、`type=api_error`；Router 原生侧阻塞 | 直连 502，未取得可记录的 code/type/param；Router 原生侧阻塞 |
| `compact-v2-native` | 因前项 5xx 停止条件而阻塞 | 因前项 5xx 停止条件而阻塞 |
| `compact-replay` | 成功的供应商原生 compaction 缺失，真实回放阻塞 | 成功的供应商原生 compaction 缺失，真实回放阻塞 |

`orphan` 的 `param` 未取得可记录值。摘要两行的直连侧使用普通 `/responses` 和 Router
相同的展开后压缩提示；Router 侧分别使用 compact v1 与 trigger v2 入口，验证摘要兼容协议。
原生 compact 两行对应独立能力探针，502 原因仍待供应商侧确认。

### 回归与验收判断

- `npm run check`、注册表导入校验通过。
- `npm test` 共 130 项，127 通过、2 项 win32 条件跳过、1 项 CIM 沙箱权限失败。
  失败项 `Windows stop terminates only the verified service process tree` 在沙箱外单独复验，
  1 项通过，0 失败、0 跳过。
- 本地回归覆盖主代理/子代理/Pi 独立状态、合法及孤立工具结果、未知 compaction、
  头白名单、认证隔离、JSON/SSE 原字节、错误脱敏、压缩失败保护、keyless 开关与迁移提示。
- 当前 registry 的 keyless 供应商数量为 0，PATH 中未发现 `ollama` / `llama-server`，
  默认 Ollama 端口 11434 未监听。实际部署版本及端点尚待提供和确认，keyless 真实 A/B 留待具体验证。

本轮证据支持保留孤立工具结果兼容和流式摘要兼容。小型 metadata fixture 的成功已记录，
该开关的关闭建议交由独立 Review 与代表性样本验证。两家现有兼容配置继续用于本轮验收，
新的生产代码缺陷尚无复现证据。

建议进入提交前 Review，Review 通过后可进入提交和推送阶段。原生 compact 的 502、原生 v2 与密文回放缺口，以及 keyless 实际
部署的适用范围应由审核层明确接受边界或继续验证，再决定 Issue #5 的最终关闭。
