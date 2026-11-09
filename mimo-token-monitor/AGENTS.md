# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents when working with code in this repository.

## 维护位置

现役源码为 `DKalien/codex-router` 的 `lite` 分支下 `mimo-token-monitor/`；原独立仓库仅作历史备份。
在 Router 根目录可执行 `codex-router.ps1 monitor`，在子目录执行下面的 Python 命令。
启动器检测父目录 `package.json` 的 Router 标识后，默认代码同步分支为 `lite`；显式环境变量优先。
桌面监控当前支持 Windows（使用 Win32 单实例和窗口 API）；POSIX shell 入口不能代表 Linux/macOS GUI 已受支持。

## 项目概述

MiMo Token Monitor — MiMo Token Plan、WLB、GPT 5 小时/周限额用量与 SuperGPT 账户余额桌面悬浮窗。Python + PyQt6 桌面应用，通过浏览器 Cookie 认证调用 `platform.xiaomimimo.com` REST API，并提供 Codex Router 日常维护入口。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py

# 运行全部 unittest
python -m unittest discover -s tests -v

# 打包 exe（使用 spec 文件，自动包含 icon.ico）
pip install pyinstaller
python -m PyInstaller MiMo-Token-Monitor.spec --clean
```

## 架构

Python 模块采用单目录扁平结构：

- **main.py** — 入口。单实例检查（Windows Mutex），创建 `QApplication`，加载配置并将旧设置中非空的 WLB / SuperGPT Key 一次性迁移到 Router 共享存储：默认保留 Router 现有生效密钥，旧值不同时弹窗由用户显式选择是否覆盖；迁移只写密钥、不启用供应商；写入失败提示并停止启动，冲突项保留旧值供重试。首次运行无 Cookie 时弹出 `SettingsDialog`，然后启动 `TokenWidget`；重启请求在事件循环结束并释放单实例锁后拉起新进程。
- **config.py** — 配置管理。默认存储于本地外置 SQLite `D:\python\data\mimo-token-monitor\settings.db`，可由 `MIMO_TOKEN_MONITOR_DATA_DIR` 覆盖，不通过 Git 同步；首次运行会从旧的 `~/.mimo-widget/config.json` 迁移，外置库不可用时保留 JSON 回退。主要字段包括 MiMo Cookie、刷新与显示设置、WLB 配置、GPT Session Cookie 和当前显示模式；完整默认值以 `config.py::DEFAULT_CONFIG` 为准。
- **usage_url.py** — WLB / SuperGPT 共用的 Base URL 安全校验与 `/v1/usage` 规范化：默认仅 HTTPS、禁止 URL 内嵌用户名/密码、查询参数与片段，校验失败的消息不回显输入原文；仅显式开启 `MIMO_TOKEN_MONITOR_ALLOW_HTTP_LOOPBACK` 时允许 loopback 主机的 `http://`（本地测试）。请求路径与设置界面共用此规则；旧配置中的非默认域名首次使用前必须由用户确认（记入 `approved_base_url_hosts`），确认前不发送共享 Key。
- **api_client.py** — API 客户端。包含 MiMo、WLB 与 GPT 5 小时/周限额查询；MiMo 余额/用量和 WLB 用量使用独立 `requests.Session` 并设置 `trust_env=False`，默认直连且不读取系统代理环境变量；WLB 用量请求禁止跟随重定向（`allow_redirects=False`，3xx 返回脱敏的配置错误），Base URL 发送密钥前先经 `usage_url` 校验；WLB 从 `/v1/usage` 的 `rate_limits` 精确读取 `7d` 窗口并保留既有扁平字段，同时从同一响应附加 `daily` 的 `1d` 窗口（缺少 `1d` 只显示无数据，不影响 `7d` 成功），供总览和详情显示；当日、周窗口同时返回时，总览中的日剩余百分比以日、周两者剩余量的较小值计算，原始日/周 `limit` 字段保持不变；GPT 查询依次尝试本机 Codex 登录、ChatGPT Session Cookie 和本地会话记录，并对网络、429、5xx 瞬时失败重试一次；共享解析器按时长将单个 7 天 primary 窗口（`window_minutes=10080` 或 `limit_window_seconds=604800`）归入周窗口，缺失的 5 小时窗口保持无数据，网络失败后本地会话仍可兜底。
- **cookie_reader.py** — 浏览器 Cookie 自动读取。优先通过 CDP 从运行中的浏览器读取明文 Cookie（绕过 v20 加密），回退到 `browser_cookie3` 读取本地数据库。设置对话框「从浏览器导入」按钮调用此模块。
- **playwright_session.py** — 可选的独立 Playwright 持久化登录会话。定时刷新和过期恢复默认使用 headless Chromium；打开平台页后轮询等待小米账号 SSO 回跳并读取 `api-platform_serviceToken` 等 Cookie（SSO 常需十余秒，禁止打开后立刻读一次就失败）。手动续期使用 headed 浏览器，两种模式均用 `cookie_validator` 校验余额；仅等待 Cookie 缺失/SSO 未完成/明确 401，同一份已被 401 拒绝的 Cookie 不重复验证，403/网络/TLS 错误立即保留并返回。浏览器关闭或页面崩溃立即停止，不把终止性错误当作登录等待；不主动导航离开 SSO 登录页。不使用现有 Chrome/Edge User Data。
- **gpt_browser.py** — GPT 网络失败后的 Chromium 传输兜底，复用 Playwright 懒加载。只允许 GPT 用量和登录接口，沿用 Requests 选定的系统/环境代理；使用隔离无头浏览器、禁用 JavaScript 和 Service Worker，通过 CDP 逐请求拦截，仅首次精确匹配的 GET 发送认证头，后续请求（含重定向）全部中止。不使用持久化资料、不关闭 TLS 校验，结束后释放浏览器资源。
- **widget.py** — 全部 UI 代码。`FetchWorker(QThread)` 后台线程发请求，`SettingsDialog` 设置表单，`TokenWidget` 主悬浮窗（自定义 `paintEvent`、拖动+边缘吸附、跨屏跨窗口吸附、标题栏置顶按钮、右键菜单、定时刷新、数据解析、tooltip、系统托盘）。悬浮窗和托盘右键菜单均支持「从浏览器导入」快速导入 Cookie（自动保存并刷新）；托盘还通过后台线程显示 Codex Router 当前开关状态并提供启停和重启入口，以及重启悬浮窗入口。
- **router_control.py** — Codex Router 托盘操作边界。优先从安装清单读取源码目录，可由 `MIMO_TOKEN_MONITOR_ROUTER_ROOT` 覆盖；状态读取复用 Router 的 `status.mjs --json`，主代理 GPT/WLB/SuperGPT/自动切换调用 `gpt-route.mjs official|wlb|supergpt|auto`，子代理供应商切换调用 `gpt-route.mjs subagent official|wlb|supergpt|auto`，Pi 路由切换调用 `gpt-route.mjs pi official|wlb|supergpt`，其余操作只串行调用路由器现有脚本，并在首个失败处停止。
- **claude_profiles.py** — Claude Code 配置模板与切换。首次使用从 `~/.claude/settings.json` 创建 `~/.claude/claude_profiles.json` 的 `API-Claude` 模板；托盘可切换模板，原子写回 settings.json 时只替换指定 `env` 参数，保留 hooks、权限和插件等其他内容。
- **window_snap.py** — Win32 顶层窗口枚举、窗口标题筛选、Qt 逻辑坐标与 Win32 物理像素坐标转换，以及跨屏跨窗口边框吸附的纯几何计算。
- **provider_keys.py** — WLB / SuperGPT 共享密钥边界。复用 Router 的 `writeProviderCredential`，只通过子进程 stdin 传密钥，沿用其原子写入和私有 ACL；`enableProvider` 仅在设置窗口显式保存密钥时调用，迁移旧配置只写密钥、不改供应商启用状态。读取区分环境变量覆盖（`env_override`）与持久存储（`read_persistent_key`），迁移比对先看环境变量覆盖、再看持久存储的生效值。读取每次刷新重新进行，兼容供应商环境变量、Router 状态目录及旧目录；拒绝符号链接和非普通文件。保存遇到不同值的环境变量覆盖时拒绝操作；超时或部分成功返回脱敏且明确的错误。设置窗口用后台线程保存，留空不改密钥、不回显现有值；保存成功后双方后续请求生效，不重启 Router。SQLite / JSON 不再保存供应商 Key，旧配置仅作一次性迁移来源，成功前不丢弃旧值。
- **supergpt_balance.py** — 只读直连查询 SuperGPT `/v1/usage` 的账户 `balance` 和 `unit`；设置窗口支持 `supergpt_base_url`（默认 `https://api.supergpt.dev`，复用 WLB 的主域名、`/v1`、`/v1/usage` 规范化逻辑）。默认通过 `provider_keys.py` 读取与 Router 共用的 Key。请求设置 `trust_env=False`，不读取系统代理环境变量；不在日志或错误中暴露密钥，Base URL 与 WLB 共用 `usage_url` 安全规则（默认仅 HTTPS、禁止内嵌凭据/查询参数/片段），不跟随 HTTP 重定向；查询失败返回安全错误，不把缺失余额当作 0。
- **snapshot_writer.py** — 快照写入。为 claude-hud 生成用量快照 JSON 文件，包含余额、用量、今日用量等信息。
- **code_sync.py** — 检查并同步代码项目 `mimo-token-monitor/`；合并布局下启动同步默认禁用（`MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED` 默认 0），只有托盘「检查并更新代码…」显式操作（检查 → 展示变更范围 → 确认后快进）或该变量显式设为 1 才执行 fetch/merge；检查阶段记录远端提交 SHA，执行前验证仍为同一提交，已变化则取消并要求重新检查、确认，并且只允许干净工作区快进。更新结果按「Router 核心 / 监控器」分类报告文件范围，绝不重启正在运行的 Router。模块保留独立的源码推送能力，但程序退出流程不调用它。

数据流：轻量启动器路径为 `launcher.py` → `code_sync.py`（合并布局默认跳过；显式启用或托盘显式更新时才检查并快进拉取）→ 启动 `main.py`；直接运行 `main.py` 会跳过源码同步。进入应用后，`config.py` 加载本地配置并迁移旧密钥 → `TokenWidget` 通过 `QTimer` 定时触发 → 工作线程调用 `api_client` / `supergpt_balance`（供应商密钥由 `provider_keys.py` 读取共享存储）→ 信号回传解析 → `paintEvent()` 绘制；退出或重启悬浮窗不执行数据库 Git 操作，重启会在旧进程释放单实例锁后启动新进程。托盘路由操作走 `TokenWidget` → `RouterWorker` → `router_control.py` → Codex Router 现有脚本；设置中的供应商密钥单独由 `ProviderKeysWorker` 写入共享存储。

## 关键实现细节

- UI 全部通过 `QPainter` 自定义绘制，不使用 QSS 样式表或 Qt Designer。
- 窗口使用 `FramelessWindowHint` + `Tool`，默认附加 `WindowStaysOnTopHint`；标题栏图钉按钮可切换置顶状态并保存到 `always_on_top`。通过 `mouseMoveEvent` 实现拖动，拖动时有屏幕边缘吸附逻辑。拖动时设置 `WA_NoSystemBackground` 防止 Windows DWM 残留阴影导致闪烁。
- 跨窗口吸附在主屏和副屏均识别原生标题为 `ETF Tracker` 或 `MiMo Token Monitor` 的可见、非最小化窗口，阈值为 15 个 Qt 逻辑像素。`GetWindowRect()` 返回物理像素；拖动时必须按当前 `QScreen.geometry()` 原点和 `devicePixelRatio` 转换后再计算，并将最终坐标转换回 Qt 逻辑坐标。
- API 认证依赖 Cookie（关键名为 `api-platform_serviceToken`）。支持通过 CDP 自动导入（需 Edge 快捷方式添加调试参数，见 README）、Playwright 独立会话自动续期，或手动从 DevTools 复制。
- MiMo API：HTTP 401 使用显式 `auth_expired` 标记触发一次自动恢复，HTTP 403 只标记拒绝访问；余额必须返回有限数值（零有效），不能凭 HTTP 200 判断 Cookie 有效。错误保留各失败端点、状态和响应类型，原始异常/正文不回显。MiMo 请求拒绝重定向；余额和用量全部恢复成功后才重置恢复次数。
- MiMo 会话签名：仅识别官网固定签名错误码和固定中文消息。`SIGN_MISSING`、`SIGN_KEY_MISSING`、`TIMESTAMP_INVALID` 获取 `/api/v1/user/sign-key` 后按 HMAC-SHA256 GET 协议重试一次；`SIGN_INVALID` 和普通 403 保留错误。签名类 HTTP 401 不触发 Cookie 续期，签名密钥接口明确 HTTP 401 才标记登录失效。使用服务端时间校准，密钥响应限读 64 KiB，密钥仅在本次调用内存中使用。修改后运行 `python -m unittest discover -s tests -p test_mimo_signing.py` 及全量 unittest。
- Playwright 续期 UI：开始/成功/失败会在悬浮窗底部状态行与托盘通知；失败状态在模态框关闭前不自动清除，关闭后再保留约 12 秒。独立 Chromium 不共享内置浏览器登录状态；若当前 Cookie 仍可用而仅 Playwright 会话未登录，或 SSO 等待超时，手动和自动续期都只给软提示，不弹阻塞对话框。结果回传时会话和窗口已经关闭，SSO 超时须明确显示续期结束和重新打开登录入口，不能显示绿色等待；已有续期任务时手动点击会提示进行中而不是静默忽略；运行期间退出/重启会提示等待，结果槽先等待线程结束再销毁。工作线程信号名为 `result_ready`，避免与 `QThread.finished` 冲突。
- 平台目标为 Windows（字体 `Microsoft YaHei`，`.ico` 图标）。
- 内置 `PLAN_TIERS` 常量（4 个挡位：Lite ¥39 / Standard ¥99 / Pro ¥329 / Max ¥659），通过 `_get_plan_tier_info()` 根据套餐总额自动匹配挡位并计算每 Credit 单价，在悬浮窗内显示已用额度折合金额。
- MiMo 余额为 0 时自动隐藏（包括右上角金额和 tooltip 余额行）；SuperGPT API余额为 0 时仍显示金额，进度条为空。
- 系统托盘：`QSystemTrayIcon` 实现最小化到托盘，右上角绘制最小化按钮（`─`），双击托盘图标恢复窗口，托盘 tooltip 与悬浮窗同步更新；“重启悬浮窗”直接退出当前进程并在释放单实例锁后启动新进程。
- Codex Router 维护：`router_control.py` 优先使用 `MIMO_TOKEN_MONITOR_ROUTER_ROOT`，否则按 `$CODEX_HOME`（默认 `~/.codex`）读取 `codex-router/install-manifest.json` 的 `current.sourceRoot`，并校验所需入口文件。状态调用 `node src/status.mjs --json`；主代理 GPT/WLB/SuperGPT 切换调用 `node src/gpt-route.mjs official|wlb|supergpt`，子代理供应商切换调用 `node src/gpt-route.mjs subagent official|wlb|supergpt|auto`，Pi 路由切换调用 `node src/gpt-route.mjs pi official|wlb|supergpt`（先检查 Router 已开启，不自动启停服务）；启停复用 `codex-router.ps1 enable|disable`；重启调用 `node src/service.mjs restart`。全部命令由 `RouterWorker` 后台串行执行，首个失败即停止，操作期间禁用路由菜单并阻止退出；托盘菜单关闭后的状态刷新延迟到事件循环下一轮，避免覆盖刚触发的操作。
- 路由操作开始时，悬浮窗底部显示进行中状态，完成后显示成功或失败摘要并在 5 秒后清除；托盘通知仍保留，用于即时反馈。
- 单实例：使用 Windows Mutex 防止重复启动，并通过命名事件唤醒已有窗口；禁止把第二次启动保留在阻塞提示框中。
- 进度条填充圆角动态调整：当填充宽度较小时，圆角半径限制为 `min(4, fill_w//2)`，避免超出外框圆角范围。
- 今日用量计算：通过记录每天首次刷新时的月累计用量作为基准，后续刷新时计算差值得到今日用量。基准值持久化存储，程序重启不丢失数据。
- 子代理供应商：托盘“路由控制 → 子代理供应商”独立选择自动（GPT → SuperGPT）/ WLB / SuperGPT / GPT（官方），按 `RouterResult.subagent_route` 勾选；选择对所有任务后续已识别的原生 GPT 子代理请求生效。
- Pi 路由：托盘“路由控制 → Pi 路由”独立选择 WLB / SuperGPT / GPT（官方），按 `RouterResult.pi_route` 勾选；仅影响 Pi 的 GPT 请求，不改变 Codex 主代理/子代理路由与用量标题高亮，Pi 无 auto 模式。
- 路由高亮：菜单用 `RouterResult.gpt_route` 勾选配置模式（含 `auto`）；自动模式的高亮使用 `gpt_effective_route`，首次尚未判断时不高亮；手动模式沿用原高亮。有效档位为 `official` / `wlb` 时，总览对应 GPT / WLB 整行标题使用 `ACCENT_GREEN`，WLB 单独页面标题同步变绿；状态未知时不高亮。托盘切换项只勾选已确认成功的路由；`supergpt` 勾选 SuperGPT 菜单项并高亮余额标题，不高亮现有官方 GPT / WLB 用量栏。启动、菜单关闭后及每 30 秒后台刷新状态，菜单可见或操作进行中跳过；退出时停止轮询。官方直连显示 GPT，Router 已接入但不健康时不显示绿色。
- 自动主路由：主代理可用 `route auto` 启用自动模式，按 **WLB → 官方 GPT → SuperGPT** 的顺序选择；
  额度恢复后，后续请求自动回到最高可用档，配置模式始终保持 `auto`。WLB 读取原始 `1d` / `7d`
  窗口，任一额度耗尽即跳过。GPT 同时监控 5 小时和周窗口：5 小时剩余 **≤10%** 或
  周剩余 **≤5%** 时跳过；所有实际存在的窗口均高于阈值时才恢复，缺失窗口不参与判断。
  Node Router 在请求到来时刷新额度，默认缓存 30 秒；没有整机常驻定时器，悬浮窗也不参与额度决策。
  同一请求至多尝试三家、每家一次，只有明确额度失败且尚未输出时才继续降级，SuperGPT 为终点。
  查询失败时保留已有档位；首次无法可靠判断则明确报错，不凭网络、认证或普通限流错误猜测额度。
  子代理 auto 按官方 GPT → SuperGPT 自动切换，阈值与主代理一致；WLB 不进入子代理自动循环。`codex-auto-review` 特殊模型继续使用官方。
- SuperGPT 余额：总览第一行标题为 Mimo，进度条为 111 px，与 WLB 左侧进度条对齐，间隔 6 px 后右侧 111 px 显示 API余额标题和右对齐金额；下方同一条血条按每 100 原币种（USD/CNY，无需换算汇率）分层，用 `divmod` 计算层数与当前层比例，整百停留在当前满层。第一层沿用剩余量绿/黄/红阈值，多层颜色按绿/蓝/紫/青循环，仅绘制上一层底色与当前层覆盖，绘制次数不随层数增加；多层条内右侧显示 7pt 小号深色 `×层数`，保留原条宽，过长时省略（完整层数见 tooltip）。250 为第二层满色作底、第三层覆盖 50%，200 为第二层满条，100 为第一层满条；不增加行数或窗口高度。独立 `SuperGPTBalanceWorker` 后台查询，仅总览页刷新，防止重复启动；退出时中断并等待线程。失败保留旧余额、层数、进度和原有颜色，tooltip 显示完整金额、完整层数、每层额度、当前层百分比、独立的最后成功时间与失败原因；零或负余额仍显示金额但血条为空，未知余额不填充，长金额缩小字号或省略。
- 总览使用 9pt 单行双栏标签：Mimo 标题显示剩余天数（无效日期不加后缀）；WLB 左侧显示 `WLB - HH:MM`（缺失时为 `WLB - --:--`），右侧只显示周重置剩余天数；GPT 左侧显示 `GPT - HH:MM`（缺失时为 `GPT - --:--`）和右对齐百分比，右侧只显示周重置剩余天数（缺失时为 `--天`）。WLB 日/周限额、GPT 5 小时/周限额均使用左右分段进度条，Mimo、WLB 日/周和 GPT 5 小时/周的百分比文字及 tooltip 均显示剩余比例（100% 减去规范化后的已用比例），总览和详情进度条均按剩余比例填充：大于 50% 为绿色、大于 20% 至 50% 为黄色、20% 及以下为红色，零剩余或未知额度为空条；共享详情绘制函数仍接收已用比例。百分比右对齐且标签自动省略避免重叠。错误摘要只显示一次供应商名称，原始错误已有供应商前缀时会在 UI 展示层去重；刷新失败时保留上次成功数据，界面与 tooltip 会标明数据已过期，并展示本机 Codex 登录、ChatGPT Cookie、本地会话三路的具体失败原因。
- 日常启动器：`launcher.py` 会被打包成轻量的 `dist/MiMo-Token-Monitor.exe`，从项目根目录或 `dist` 启动时读取项目中的 `main.py`。业务源码变更后只需重启启动器；只有修改启动器时才运行 `./build-launcher.ps1`。完整独立发行版仍使用 `MiMo-Token-Monitor.spec`，轻量构建不得覆盖该 spec。
- GPT 新鲜度：本地兜底扫描最近修改的 10 个会话文件，按有效带时区的额度事件 `timestamp` 选择最新完整快照，并返回 `observed_at` 与实时来源的 `source_errors`；缺失或非法时间的记录跳过。UI 用独立 `_gpt_data_time` 记录实时成功时间或历史事件时间，本地快照仅严格较新时替换；历史兜底始终保留过期提示和实时失败原因，实时恢复后清除。总览 tooltip 显示实际 GPT 数据时间；底部“检查于”仅表示最近检查时间。
- GPT 传输：`_gpt_get()` 使用独立的 Requests 会话跨刷新复用连接，并以线程锁串行访问，请求前后清空会话 Cookie，退出时关闭会话；每次认证仅通过本次请求头传递，禁止自动重定向。最多进行两次、每次超时 15 秒的 Requests 尝试；最后一次遇到连接或超时异常时调用 `gpt_browser.fetch_gpt_response()`，不对 HTTP 401/403/429/5xx 启动浏览器额外重试。浏览器不可用或请求失败时保留脱敏后的具体网络类型，再按既有认证来源及本地历史顺序兜底。所有 GPT 响应均在解析后关闭，重试前关闭上次 HTTP 响应；不增加新依赖，不改变 MiMo/WLB/SuperGPT 查询路径。
- 代码同步：合并布局下启动不再隐式 fast-forward 整个 Router checkout；托盘「检查并更新代码…」显式检查远端并展示变更范围（Router 核心 / 监控器），确认后才在干净工作区执行 `fetch` + `merge --ff-only`，dirty 工作区、detached HEAD、错误分支或分叉一律不变更；直接运行 `main.py` 以及退出/重启流程都不会自动提交或推送源码。
- **红线规则**：PyQt6 在 Windows 上使用浮点数坐标调用 `QPainter.drawLine()` 会导致崩溃（退出码 `0xC0000409`）；所有 `drawLine()` 坐标必须使用整数，`QRectF`/`QPointF` 等其他 Qt 几何 API 不受此约束。
- 本地设置目录环境变量：`MIMO_TOKEN_MONITOR_DATA_DIR`（默认 `D:\python\data\mimo-token-monitor`）；设置数据库不得通过 Git 拉取、提交或推送。
- 代码同步配置环境变量：`MIMO_TOKEN_MONITOR_CODE_REPO_ROOT`、`MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH`、`MIMO_TOKEN_MONITOR_CODE_GIT_REMOTE`、`MIMO_TOKEN_MONITOR_CODE_GIT_BRANCH`、`MIMO_TOKEN_MONITOR_CODE_GIT_TIMEOUT_SECONDS`、`MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED`（合并布局默认 `0`）。第三方 Base URL 的回环 HTTP 测试通道由 `MIMO_TOKEN_MONITOR_ALLOW_HTTP_LOOPBACK` 显式开启。
