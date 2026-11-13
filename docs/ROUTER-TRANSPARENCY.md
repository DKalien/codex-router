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

## 2026-11-13 后续原生 compact 能力验证

本轮基于 `082c11efb2d185d565be52fb632071a8cf882415` 的干净 `lite` 工作区执行。
上一节保留首轮 A/B 的历史结果，本节更新独立的 v2 能力及回放结论。生产逻辑与供应商
兼容配置沿用已审核版本。

### 文档与原生 v1 的可归因边界

OpenAI Responses 参考明确声明 `compaction_trigger` 为最后一个 input item，
`max_output_tokens` 为响应生成 Token 上限，涵盖可见输出及 reasoning。v2 复用普通
`/responses`，因此按独立能力验证。该规范给出的参数语义作为探针预算依据，
实际供应商的计量结果另行记录。[OpenAI Responses 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)

SuperGPT 公开文档列出 `/v1/responses` 和 `stream=true` 示例，并说明能力取决于模型、
分组和客户端。已查阅的文档未明确承诺原生 compact v1/v2 或 Responses 非流式能力。
其故障排查将 5xx 归入服务异常/通道状态，要求结合正文判断原因。
[接口示例](https://console.supergpt.dev/help/api-examples)、[地址与能力说明](https://console.supergpt.dev/help/clients)、[故障排查](https://console.supergpt.dev/help/troubleshooting)

WLB 公开页面在本轮文档访问工具中不可用，公开搜索未取得 `sub2_unavailable` 的供应商
错误码定义。因此该代码只作为原样记录的错误分类，不用于断言供应商内部组件及根因。

| 归因层次 | 已有证据 | 当前结论 |
| --- | --- | --- |
| 本地 Router 或 URL 拼接 | 首轮 v1 为直接调用已校验的供应商 HTTPS 地址；该调用绕过 Router，收到完整 HTTP 502 | 已定位到直连供应商 HTTP 响应层，Router 转换逻辑未参与这次失败 |
| 端点缺失或方法错误 | 观察到 502 JSON，未取得 404/405 或明确的 unsupported-endpoint 错误 | 端点是否缺失尚未证实，502 可能掩盖内部转发错误 |
| WLB 网关/上游转发 | `code=sub2_unavailable`、`type=api_error` | 可记录为供应商服务错误 envelope，生成错误的具体 hop、上游状态及是否临时故障均待确认 |
| SuperGPT 网关/上游转发 | 502 JSON，未取得可记录 code/type/param | 服务侧失败已观测，内部转发层次与原因待确认 |
| 模型或账户普遍不可用 | 相同模型的首轮普通 SSE 成功；本轮额度查询均为 200 且可用，v2 均完成 | 普通及本次 v2 请求可用。v1 专属模型、套餐或通道约束仍待供应商证据 |
| v1 payload/schema | 首轮使用 model/input/instructions 的最小文档字段，未取得字段级 param 错误 | 未发现明确 schema 拒绝证据，供应商的端点专用字段或最短历史要求仍可能不同 |

本轮复用首轮 v1 证据。相同探针不足以进一步定位，且 standalone compact 缺少已确认的
输出硬上限，本轮 v1 真实调用次数为 0。下一步归因需要供应商公开错误码定义、端点能力
声明或其服务端日志。当前结论为「上游 502 原因未确认」。

SuperGPT 非流式问题同样复用首轮两侧的 `200 + SSE server_error` 结果。当前 Router
将普通 HTTP 200 响应中的 SSE 错误事件继续交给客户端，HTTP 错误翻译处理非成功
HTTP 响应，摘要路径固定流式。本轮针对 `stream=false` 的真实重复调用次数为 0。

### 原生 v2 独立单次探针与回放

每家先经受保护凭据接口读取持久 Key、校验默认 HTTPS 目标，再各查询一次额度。
两家额度查询均为 200 且可用，沿用已成功调用的 WLB `gpt-5.6-luna` 和 SuperGPT
`gpt-5.6-sol`。没有通过官方身份、真实 Cookie 或私人对话发起请求。

每家仅调用一次直接 `/responses`，schema 为 `final-trigger-stream-storefalse-max64`。
请求 input 仅有一条合成事实消息及末尾 `{ "type": "compaction_trigger" }`，
设置 `stream=true`、`store=false`、`max_output_tokens=64`，完整请求体小于 512 字节。
总 deadline 为 20 秒，读取闲置上限 10 秒，响应限读 64 KiB，拒绝重定向，无自动重试
或供应商切换。公开文档给出生成上限语义，实际响应 usage 继续用于预算复核。

| 供应商/模型 | endpoint 与 schema | HTTP/内容 | 终态与错误 | compaction | 后续回放 |
| --- | --- | --- | --- | --- | --- |
| WLB / `gpt-5.6-luna` | `/responses`，末尾 trigger，stream=true，max64 | 200 / SSE | `response.compaction.compacting`、`response.completed`；无 failed/incomplete/error，type/code/param 均无 | 生成，报告 output_tokens=55 | 同供应商一次最小回放成功 |
| SuperGPT / `gpt-5.6-sol` | `/responses`，末尾 trigger，stream=true，max64 | 200 / SSE | `response.compaction.compacting`、`response.completed`；无 failed/incomplete/error，type/code/param 均无 | 生成，报告 output_tokens=67 | 67 超过请求上限 64，触发停止条件并跳过回放 |

WLB 回放只在内存中保留其成功 v2 的完整 output，作为下次 input 的前缀原样传回同一
供应商、同一模型和同一临时探针流程，再追加最小合成追问。返回 output 先限制在 8 KiB
以内，回放设置 `stream=true`、`store=false`、`max_output_tokens=32`，沿用相同超时和
限读边界。唯一一次回放返回 200 SSE `response.completed`，报告 5 输出 Token，
合成事实回答断言正确。compaction 密文及原始响应均未落地、打印或发送给其他供应商。

SuperGPT 的本次 `output_tokens=67` 大于请求的 64。已有证据无法区分压缩计量口径与
生成上限实现差异，也无法确认该参数对压缩生成形成严格费用上限。按停止条件结束该
供应商后续请求，密文仅在内存使用后释放。继续真实验证前需先明确压缩 Token 的计量
和预算约束。该差异属于供应商能力/计量边界，尚无 Router 代码缺陷的本地复现。

本轮真实请求共 5 次，包含 2 次只读额度查询、2 次 v2 单次探针和 1 次 WLB 回放。
三次生成/回放响应报告的输出 Token 分别为 55、67、5。正式主/子代理与 Pi 路由、
供应商选择、服务 PID、安装清单、caller secret、Codex config 和持久凭据候选文件在
探针前后校验一致。没有启动 Router 测试实例，直接供应商请求以现有代理环境规则运行。

### Keyless 环境与兼容开关建议

本轮只读识别结果为 registry 中 keyless 数量 0，PATH 未发现 `ollama`、`llama-server`、
`llama-cli`、`llama.cpp`。已知候选进程名未检出，回环端口 11434、8080、1234 均未监听。
当前已检查范围内没有可验证的本地 Responses 部署及版本，实际 Keyless 验证状态为
「待具体环境」。本轮没有安装程序、下载模型或启用服务。

`summarizeCompaction` 建议保留。v2 单次成功更新了能力证据，v1 502 仍待归因，
SuperGPT 压缩预算存在未决计量问题，长历史及跨版本行为尚未覆盖。当前一个开关同时
控制两条压缩路径，关闭会同时改变 v1 行为，尚缺单独关闭条件。

`orphanToolOutputs` 建议保留，首轮两家的 400/200 对照继续作为直接证据。
`omitClientMetadata` 建议保留待独立 Review，首轮仅验证小型 metadata 被接受，
本轮没有增加该项覆盖。各项建议以现有模型和配置范围为限，Issue #5 继续 open。

本轮专项回归使用 `node --test --test-name-pattern="Keyless|透明转发|审计：第三方压缩始终|审计：SSE 压缩|失败或空摘要" test/router-fixes.mjs`，
9 项通过，0 失败、0 跳过。文档示例、相对链接与 `git diff --check` 通过。
本轮仅修订本审计文档，完整回归沿用已提交版本的记录。

## 2026-11-13 Token 计量调查与 Router 原生 v2 验收边界

本轮从同一 HEAD 继续，开始时唯一未提交文件为本审计文档，已有后续能力记录共新增
90 行。以下内容增量补充计量证据及 Router E2E 的执行条件，上轮直连结果保留。

### 规范、供应商说明与实测的区别

| 来源 | 可核实内容 | 尚未解决的边界 |
| --- | --- | --- |
| OpenAI Responses create 参考 | `compaction_trigger` 应为最后一个 input item；通用 `max_output_tokens` 定义涵盖可见输出及 reasoning，schema 最小值为 16 | 通用参数定义不能单独证明第三方 compact 对 max64 的预算保证。参见 [create 参考](https://developers.openai.com/api/reference/resources/responses/methods/create) |
| OpenAI 官方输出长度帮助 | 针对 `compaction_trigger` 明确要求省略 `max_output_tokens` 或至少设为 20000，更小值会被拒绝 | 该专项说明比 create 页面展示的通用 schema 更严格。第三方此前接受 max64 与此说明存在行为差异，实际采用何种校验及预算规则需各供应商确认。参见 [输出长度帮助](https://help.openai.com/en/articles/5072518-controlling-the-length-of-openai-model-responses) |
| OpenAI Token 计数说明 | output usage 包括不可见的格式与 reasoning Token，可高于可见文本 Token 数；通用输出上限也覆盖不可见 Token | 这只能解释 usage 与可见文本的差异，未提供本次 67 大于请求 64 的 compact 专用解释。参见 [Token 计数说明](https://developers.openai.com/api/docs/guides/token-counting) |
| OpenAI compact 与 streaming 参考 | v1 usage 描述压缩过程的输入、缓存、推理和输出用量；`response.compaction.compacting` 仅报告进度，最终密文由 output item 事件交付 | 进度事件数量、密文长度及可见文本均不足以推算费用。参见 [compact 参考](https://developers.openai.com/api/reference/resources/responses/methods/compact) 和 [streaming 事件](https://developers.openai.com/api/reference/resources/responses/streaming-events) |
| SuperGPT 计费说明 | 输入、输出、缓存及推理可能影响费用，不同协议字段口径可能不同，最终扣费以使用记录为准 | 未找到明确适用于 `gpt-5.6-sol` 原生 compact v2 的 Token 映射、额外压缩计费项或硬预算保证。参见 [模型与计费](https://console.supergpt.dev/help/models-and-billing) |
| SuperGPT 使用记录与公开模型目录 | 使用记录用于核对实际用量和扣费；超时、断流也可能已处理。公开目录可见 `gpt-5.6-sol` | 公开目录未提供本次密钥适用的 compact 专项单价及能力保证，本轮未登录、导出账单或读取私人使用历史。参见 [使用记录说明](https://console.supergpt.dev/help/usage) 和 [公开模型目录](https://console.supergpt.dev/model-plaza) |

OpenAI 的 compaction 指南说明返回项为不透明状态，后续应按相应协议原样传回。
本轮继续使用项目已有的 trigger v2 定义，未将指南中的自动 `context_management`
机制计为 trigger v2 的真实验证。[Compaction 指南](https://developers.openai.com/api/docs/guides/compaction)

SuperGPT 上轮 `max_output_tokens=64`、`usage.output_tokens=67` 及完成状态均为直连
实测。当前证据不足以判定上限实现违规、确定多扣费用，或将差值归因于固定的 3 个
协议 Token。供应商是否映射、忽略或提高 compact 专用输出预算，以及 usage 是否包含
独立压缩过程，均未明确。当前成本边界记为不明，继续保留停止条件。

### 本轮真实调用与预算决定

| 供应商/模型 | 验收场景 | 本轮真实 API 次数 | 状态与原因 |
| --- | --- | --- | --- |
| WLB / `gpt-5.6-luna` | 隔离 Router `/responses`、末尾 trigger、stream=true、store=false、max64；至多一次 max32 回放 | 0，含额度查询 0 | 预算前提阻塞。上轮直连 55 Token 和一次成功回放保留，但单次实际用量不足以证明后续硬上限。新检出的官方 compact 专项最小值与 max64 探针存在差异，未取得 WLB 的明确限额规则，未启动真实 E2E 实例 |
| SuperGPT / `gpt-5.6-sol` | 原生 v2 Router E2E 与回放 | 0，含额度查询 0 | 仅完成零费用文档调查。67>64 的计量和费用边界仍不明，未找到符合继续调用条件的专项证据 |
| 两家 | 原生 v1 `/responses/compact` 和 SuperGPT 非流式复测 | 0 | 复用既有 502 及 200+SSE server_error，未取得可进一步归因的公开资料 |

本轮没有产生新的供应商推理请求或费用。未提高 max64 预算、未省略该上限，也未通过
重试、故障切换或更换凭据继续探针。余额查询推迟到预算规则确认之后，当前可用额度
沿用历史记录并标为未刷新。20 秒 deadline、10 秒 idle 和 64 KiB 响应边界只能约束
本地等待及读取，客户端停止无法证明上游立即停止计费。

原生 v1 仍为「直连供应商 HTTP 502，原因未确认」。SuperGPT 故障帮助将 5xx 列为
临时服务或分组通道异常，未给出 compact 专属归因；WLB 页面本轮访问仍不可用，
定向搜索未取得 `sub2_unavailable` 的可靠定义。[SuperGPT 故障排查](https://console.supergpt.dev/help/troubleshooting)

### Router 源码与本地回归

`src/router.mjs` 在末尾 trigger 处识别 v2，仅在供应商显式开启 `summarizeCompaction`
时进入摘要路径。未开启时保留 trigger 和顶层参数，经 `/responses` 发出。上游原生
compact 成功体先按 32 MiB 总量读取、解析 JSON/SSE，并要求无解析出的 error、status
缺省或为 completed，以及非空 compaction 密文，验证通过后交还原始字节。SSE 事件
解析另有 8 MiB 单事件边界。

本轮重跑既有本地测试，全部使用合成凭据、临时状态和回环 mock。覆盖范围如下。

- 透明 A/B 对两家配置 ID、主代理、子代理和 Pi 验证原生 v2 input、查询参数、协议头
  白名单、凭据隔离及逐字节相同的 SSE 响应。
- 原生 v1/v2 均验证失败、不完整、空密文、非法结构与 SSE failed 的拒绝及错误脱敏。
  现有 33 MiB 超限用例从 v1 入口运行，源码限读分支由 v1/v2 共用，v2 专项超大响应
  和真实密文回放的在线行为仍未单独覆盖。
- Keyless 参数 opt-in 与迁移提示、unknown compaction、兼容项独立启停、zstd、
  无 content-type SSE、caller capability 脱敏及摘要失败保护通过。

执行 `node --test --test-name-pattern="Keyless|透明转发|审计：第三方压缩始终|审计：SSE 压缩|失败或空摘要|请求日志会脱敏|上游巨型错误|无 content-type 的 SSE|zstd 请求|主代理和子代理|Pi 命名空间使用" test/router-fixes.mjs`，
15 项通过，0 失败、0 跳过。`npm run check` 通过，出现已有 npm `store-dir` 配置警告。
本轮未重跑完整 `npm test`，历史 130 项及 Windows CIM 权限复验结论见前文。
当前未取得新的 Router 业务缺陷复现，未修改生产源码或测试。

### 后续需供应商确认

1. SuperGPT 应明确 `gpt-5.6-sol` 的 trigger v2 `usage.output_tokens` 是否汇总压缩及
   其他生成、不可见结构、推理 Token，各项对应哪条实际计费记录，67>64 的原因是什么。
2. 两家应确认 trigger v2 是否接受 max64，是否存在最小值、预算覆盖或忽略行为，
   并给出适用于当前模型和分组的输出或费用硬上限。确认前继续暂停 Router 真实 E2E。
3. SuperGPT 应说明 compact v1/v2 的输入、缓存、推理、输出及独立费用项，以及失败、
   取消、超时的结算规则。若需核对上轮实际扣费，由用户在控制台按合成探针时间查找，
   仅提供必要的脱敏字段。
4. 两家应确认 v1 `/responses/compact` 的端点、模型和订阅支持范围、最小 payload，
   并解释已有 502。WLB 需补充 `sub2_unavailable` 的定义；SuperGPT 需说明非流式
   Responses 的协议与现有 SSE server_error。

两家 `summarizeCompaction` 继续保留。`orphanToolOutputs` 继续依据首轮 400/200
对照保留，`omitClientMetadata` 维持待独立 Review。Keyless 实际部署仍待具体环境。
本轮只增量编辑审计文档，原有 90 行未提交记录完整保留。17 个正式配置、主/子代理/Pi
路由、服务 PID、caller secret 及凭据候选文件前后哈希一致，登记服务进程仍存活。
文档 JSON 示例、相对链接、HTTPS 来源 URL 和新增内容的明显秘密模式检查通过，
`git diff --check` 通过。未暂存或提交文件，Issue #5 继续 open。
