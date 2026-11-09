# MiMo Token Monitor

MiMo Token Plan、WLB、GPT 5 小时/周限额用量及 SuperGPT 账户余额监控桌面悬浮窗。

现役源码位于 [Codex Router 的 lite 分支](https://github.com/DKalien/codex-router/tree/lite/mimo-token-monitor)，
作为 `mimo-token-monitor/` 子项目维护；原独立仓库只保留迁移前的源码和开发历史备份。

## 功能

- 桌面悬浮窗实时显示 Token Plan 用量（已用/总额度/剩余）
- **今日已用额度显示**：自动计算并显示当天使用量及折合金额
- 根据套餐总额自动匹配挡位（Lite/Standard/Pro/Max），显示已用额度折合金额
- MiMo 余额为 0 时自动隐藏；SuperGPT 余额为 0 仍显示 $0.00
- **一键导入 Cookie**：通过 CDP 自动从浏览器读取，无需手动复制
- **Playwright 自动续期**：使用独立浏览器配置定时刷新登录状态，减少重复登录
- 自动读取小米平台 API 获取真实数据
- 额度进度条按剩余量填充，剩余量减少时颜色由绿→黄→红
- 支持手动填写套餐有效期并显示“有效期至”
- 支持按量付费用量查询
- 可拖动、半透明、置顶显示；标题栏图钉按钮可切换置顶状态，默认置顶并自动保存
- **跨窗口边框吸附**：可与 ETF Tracker 悬浮窗相互吸附，主屏和副屏均支持，并兼容不同 DPI 的显示器
- **系统托盘**：最小化到托盘，双击恢复，实时 tooltip，并可直接重启悬浮窗
- **Claude 配置切换**：从托盘在多个 Claude Code 配置模板之间切换，只更新 `~/.claude/settings.json` 的指定 `env` 参数
- **Codex Router 维护**：从托盘查看当前状态、分别切换主代理和子代理的官方 / WLB / SuperGPT 路由，并开启、关闭或重启路由器
- **单实例运行**：防止重复启动；再次运行会自动恢复并置顶已有窗口
- **WLB 用量显示**：标题栏循环图标可切换 MiMo Token / WLB 模式，支持配置 WLB API（默认 https://codex.wlbclub.com），显示剩余百分比、已用百分比、进度条和状态；接口返回 `reset_at` 时同时显示 7 日窗口重置日期
- **用量总览页面**：第一行左侧为 Mimo 套餐用量及剩余天数，右侧为 API余额（SuperGPT 账户余额），金额右对齐、每 100 原币种为一层血条，在同一条内叠加颜色并在条内右侧显示层数。下方 WLB 日/周限额与 GPT 主/周窗口分别显示重置时分或剩余天数、右对齐剩余百分比和左右分段进度条
- **Claude HUD 集成**：生成快照文件供 claude-hud 读取显示
- **本地设置数据库**：配置保存在外置 SQLite 文件中，不通过 Git 或网络同步

MiMo、WLB 和 SuperGPT 的余额/用量请求默认直连，不读取 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY` 等系统代理环境变量；GPT 用量查询沿用独立的认证与请求链路。若直连被防火墙、网络策略或当前运行环境禁止，相关查询会失败，需要先放行对应域名或恢复可用的网络路径。

## 使用方式

### 方式一：从 Router 启动（推荐）

在 Router 仓库根目录执行：

```powershell
python -m pip install -r mimo-token-monitor\requirements.txt
.\codex-router.ps1 monitor
```

也可让 Windows 快捷方式使用 `pythonw.exe`，参数指向当前 checkout 中的
`mimo-token-monitor/launcher.py`，工作目录设为该子目录，图标使用其中的 `icon.ico`。
源码仓库不自带 `dist/MiMo-Token-Monitor.exe`；需要 EXE 时按下文构建轻量启动器。

启动器只在整个代码工作区干净且可安全快进时拉取更新；嵌套在 Router 中时默认识别
父目录为代码仓库、分支为 `lite`，独立布局仍沿用 `main`。显式配置的
`MIMO_TOKEN_MONITOR_CODE_REPO_ROOT`、`MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH` 和
`MIMO_TOKEN_MONITOR_CODE_GIT_BRANCH` 优先。启动不会自动提交或推送。
Python 源码变更后重启悬浮窗即可加载；构建的 EXE 只在 `launcher.py` 变化时需要重打包。

### 方式二：直接运行源码

```bash
# 安装依赖（首次运行）
pip install -r requirements.txt

# 运行
python main.py
```

首次运行需要填入 Cookie。

### Playwright 自动续期（可选）

设置中的“Playwright 自动续期”默认开启，默认每 6 小时打开一次 MiMo 页面并从独立的
`playwright-profile` 配置读取最新 Cookie。首次使用需要安装浏览器运行时：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

该配置目录位于 `MIMO_TOKEN_MONITOR_DATA_DIR` 下，不使用也不修改现有 Edge/Chrome 用户目录。
该目录包含登录状态，请勿复制给他人或提交到 Git。

续期会打开平台页并等待小米账号 SSO 回跳，读取 `api-platform_serviceToken` 等
`xiaomimimo.com` Cookie（SSO 往往需要十余秒，不能打开后立刻读取）。自动续期和过期恢复
默认使用 headless Chromium，不会闪现浏览器窗口；手动「Playwright 续期」会打开有界面浏览器，
并保持到 Cookie 通过 API 校验或超时，以便完成登录/验证码。该窗口使用独立的 Chromium profile，
不会自动共享内置浏览器的登录状态；请在新打开的 Chromium 窗口中完成登录。

若当前配置的 Cookie 仍有效、只是独立 Playwright 会话尚未写入平台 Cookie，自动续期失败只会在
悬浮窗底部和托盘提示「Playwright 未登录，当前 Cookie 仍可用」。若 SSO 等待超时，任务和登录窗口已结束，
会明确提示「续期已结束，MiMo 登录未完成」，不会显示绿色等待状态，也不会弹阻塞对话框；本次不更新 Cookie。真正需要
人工登录时，可从托盘或悬浮窗菜单手动执行「Playwright 续期」。验证码仍然需要人工输入。
Playwright 不能延长服务端设置的 Cookie 有效期。

续期只在明确的 HTTP 401 后自动恢复一次；HTTP 403、网络错误、TLS 错误和业务错误不会被当作 Cookie 过期。
MiMo 查询遇到官网定义的 `SIGN_MISSING`、`SIGN_KEY_MISSING` 或 `TIMESTAMP_INVALID` 时，
使用当前 Cookie 获取会话签名密钥，按官网 GET 协议添加签名并最多重试一次。签名错误单独显示，
其中 HTTP 401 也须排除签名错误后才触发登录恢复。签名密钥仅在本次调用内存中使用，密钥响应限读 64 KiB。
手动和自动续期均等待 Cookie 通过余额 API 校验，不重复验证同一份已被 401 拒绝的 Cookie，
等待期间不会主动导航离开小米登录页。403 或其他非登录错误会立即保留具体 API 错误并结束本次续期。
余额与用量均恢复成功后才重置自动恢复次数；也可手动续期或重新导入 Cookie。
续期线程仍在运行时，退出/重启会提示先结束任务；可先关闭登录浏览器，再退出悬浮窗。

若 Chromium 关闭或页面崩溃，本次续期立即结束，提示保留首次发现的阶段、headed/headless 模式、
异常类型或关闭事件，不再持续调用 `cookies()`。专用 profile 被占用时，关闭其他续期任务后重试，
**不要删除、重置或把它交给他人**。日常 Edge/Chrome 已登录不代表这个独立 profile 或已保存 Cookie 有效。

排查 MiMo HTTP 403 时，悬浮窗 tooltip 会同时保留余额和用量错误，显示 `/api/v1/...` 路径、
HTTP 状态和 HTML/JSON/未知响应类型。HTML 403 不能单独证明触发风控，JSON 403 也不能单独证明 Cookie 失效。
MiMo Requests 仍使用 `trust_env=False` 直连、保持 TLS 校验；Chromium 未显式配置代理，
其网络出口和系统证书环境可能与 Requests 不同。MiMo API 重定向现会明确拒绝，不再把 Cookie 带去登录页。

安全的人工诊断步骤（由用户主动执行；这些操作会访问自己的账户）：

1. 重启悬浮窗加载新代码，查看 tooltip 中失败的是 balance、用量还是两者；仅记录路径、状态、响应类型。
2. 在日常浏览器 DevTools → Network 中查看平台自己发起的相同 API 的状态和响应类型。
   页面能打开不等于该 API 成功；不要导出 HAR、复制请求头、Cookie 或响应正文给他人。
3. 若浏览器 API 成功，可显式「从浏览器导入」后再次查看状态；403 仍在时停止反复续期，
   向平台支持确认 API 权限/访问策略，并只提供脱敏的路径、状态和时间。
4. 若登录浏览器自行消失，记录是否手动关窗及新提示的阶段；在 Windows 可靠性监视器中检查对应时间的 Chromium 崩溃事件，
   检查安全软件是否拦截。先关闭其他续期任务，再手动重试；不要关闭证书验证或自动修改代理来尝试绕过。


本次隔离调查和验证范围见 [MiMo 403 / Playwright 调查记录](docs/mimo-403-playwright-20261106.md)。

若提示“持久化会话启动失败”，错误会标明失败阶段（创建专用目录、启动 Playwright 驱动或
启动 Chromium）；`OSError` 还会显示可用的 `errno` / `winerror` 数字，便于定位原因。
提示不会包含原始异常消息、目录路径或 Cookie；仅凭 `OSError` 类型不能确定具体根因。

通过 `pythonw` 或无控制台 EXE 启动时，入口会在创建后台线程前为缺失的 `sys.stderr`
提供持续有效的空设备流，避免 Playwright 驱动继承无效错误输出句柄而出现 `winerror=6`。
已有的错误输出流保持原样；此修复修改源码后重启悬浮窗即可生效。

**方式一：一键导入（推荐）**

1. 在 Edge 快捷方式「属性 → 目标」末尾添加（注意前面有空格）：
   ```
   --remote-debugging-port=9222 --remote-allow-origins=*
   ```
2. 关闭所有 Edge 窗口，从该快捷方式重新打开
3. 浏览器打开 [platform.xiaomimimo.com](https://platform.xiaomimimo.com) 并登录
4. 在程序设置中点击「从浏览器导入」按钮，自动读取并验证

**方式二：手动复制**

1. 浏览器打开 [platform.xiaomimimo.com](https://platform.xiaomimimo.com) 并登录
2. 按 F12 → Network → 刷新页面 → 点任意请求
3. 复制 Request Headers 中的 Cookie 值
4. 粘贴到设置中

### WLB 设置（可选）

在设置中填写 **API Base URL** 和 **API Key** 后，可通过标题栏循环图标切换到第三方用量显示模式或“总览”页面：

- **Base URL**：默认 https://codex.wlbclub.com；可填写主域名、`/v1` 或完整的 `/v1/usage`，程序会自动避免重复拼接
- **API Key**：与 Codex Router 共用。填写新 Key 并保存后，双方后续请求都会使用它；留空保持现有密钥，不会删除或停用 Router 的 Key。设置窗口不回显已有密钥。
- 切换后悬浮窗显示剩余百分比、已用百分比、7 天窗口和状态；当日、周窗口同时返回时，总览中的日剩余百分比以日、周两者剩余量的较小值计算，原始日/周 `limit` 字段保持不变；接口返回 `reset_at` 或 `reset_after` 时显示重置日期时间，总览中的 WLB 左侧显示日窗口重置时分（如 `WLB - 18:18`，缺失时为 `WLB - --:--`），右侧只显示周重置剩余天数（如 `5天`，缺失时为 `--天`）
- 未配置共享 API Key 时切换会显示提示，不会发送空请求

WLB 和 SuperGPT 的密钥统一保存在 Router 的受保护状态目录中，使用 Router 原有的原子写入和私有文件权限，
不再写入悬浮窗 SQLite / JSON 设置。首次启动新版会把旧设置中非空的 Key 迁移到共享存储（以旧悬浮窗设置值为准），
成功后清除旧密钥字段；失败时保留旧配置并显示错误，不继续启动。可通过 `MIMO_TOKEN_MONITOR_ROUTER_ROOT`
指定 Router 源码目录，默认从安装清单查找；需要本机可用的 Node。
从悬浮窗修改 Key 或运行 `provider-key ... set` 后，Router 下一次请求和悬浮窗下一次刷新会重新读取密钥，无需重启 Router。
读取顺序兼容 Router：对应供应商的环境变量优先，其次读取状态目录和兼容旧目录中的密钥文件；环境变量覆盖了不同 Key 时，
设置保存会明确报错，需先移除覆盖变量。Base URL 仍用于悬浮窗查询接口。

### 总览与 SuperGPT 余额

总览及详情中的 Mimo、WLB 日/周和 GPT 5 小时/周额度进度条均按剩余比例填充，与百分比文字一致；剩余 100% 为绿色满条、50% 为黄色半条、20% 为红色 20% 长度，0% 为空条。沿用剩余量大于 50% 为绿色、大于 20% 至 50% 为黄色、20% 及以下为红色的阈值；缺失或未知额度显示空条，总览悬停提示继续显示剩余比例。

总览标题为 **Mimo**，进度条与下方 WLB 左侧进度条等宽（111 px，扣除栏间距后均分）；右侧标题为 **API余额**，金额右对齐，下方同一条血条以 **100 原币种**为一层，不增加行数或窗口高度。余额 100 为第一层满条，200 为第二层满条，250 为第二层满色作底、第三层覆盖 50%；多层时条内右侧显示 7pt 小号 `×层数`（保留原条宽）。第一层仍按剩余量使用绿、黄、红；之后各层使用绿、蓝、紫、青循环区分，显示时只绘制上一层底色与当前层覆盖。
余额沿用刷新间隔在独立后台线程查询，可在设置窗口修改 **SuperGPT Base URL** 和 **SuperGPT API Key**。
Base URL 默认 `https://api.supergpt.dev`，支持主域名、`/v1` 或完整 `/v1/usage`，留空恢复默认地址。
API Key 与 WLB 一样使用共享存储；填写新值并保存会更新 Router 的 `supergpt-api-key.secret` 并启用该供应商，
留空保持共享 Key 不变。支持 Router 的状态目录环境变量，不把密钥放入命令行参数或日志；输入框默认隐藏字符。
查询使用 `/v1/usage` 的 `balance` 和 `unit`，不把密钥额度当作充值余额。
该请求默认直连，不使用系统代理环境变量。
美元显示 `$`、人民币显示 `¥`，分别按每 100 USD 或 100 CNY 分层，无需汇率换算；缺少数据时显示 `--`，零或负余额仍显示金额，血条为空。失败时保留上次余额、层数、进度和原有颜色，
悬停可查看完整金额、完整层数、每层额度、当前层百分比、最后成功时间和错误原因；过长层数在条内右侧省略显示（完整层数见 tooltip）。切换到 SuperGPT 路由后，余额标题会变绿。

底部状态行只显示一次供应商名称；如果原始错误内容已经带有 `MiMo`、`WLB`、`GPT` 或 `SuperGPT` 前缀，界面会自动去掉重复前缀，完整原因仍保留在 tooltip 中。

### GPT 5 小时与周限额

总览会优先使用本机 Codex 登录信息查询 GPT 5 小时与周限额，也可在设置中填写 ChatGPT Session Cookie 作为备用认证；本地 Codex 会话记录是最后兜底。只有一个窗口且时长为 7 天（`window_minutes=10080` 或 `limit_window_seconds=604800`）时，将其作为周窗口解析；5 小时窗口缺失时显示 `--`，网络来源失败后仍可读取本地周限额记录。GPT 行使用一条左右分段的进度条，左侧显示 `GPT - HH:MM` 主窗口重置时分，右侧只显示周重置剩余天数；缺失重置数据时分别显示 `GPT - --:--` 与 `--天`。网络、限流或服务端瞬时失败会自动重试一次；刷新仍失败时继续显示上次成功数据，并在 tooltip 中标明具体失败来源。

本地兜底会扫描最近修改的 10 个会话文件，按额度事件自身的时间选择最新快照；缺少有效带时区时间的记录不采用。历史快照只有比当前显示的数据更新时才替换，且始终标明实时查询失败、数据已过期，并保留失败来源。悬停可查看 GPT 数据的实际时间，实时查询恢复后自动清除警告。底部“检查于”表示最近检查时间，不能用来判断各项数据是否更新成功。

GPT 实时请求沿用系统或环境代理，使用独立的 Requests 会话复用连接，减少每次刷新重新建立代理/TLS 连接的开销；跨工作线程串行访问，每次请求前后清空会话 Cookie，认证信息仅通过本次请求头传递。Python 请求最多尝试两次，最后一次遇到连接或超时错误时，会使用已有的 Playwright/Chromium 在隔离的无头浏览器中查询同一接口；正常查询不启动浏览器，HTTP 认证、限流及服务端错误仍按原规则处理。浏览器兜底仅向指定接口发送一次请求，不跟随重定向，不使用现有浏览器或 MiMo 登录资料；查询完成后关闭浏览器。需已安装 Playwright 和 Chromium（`python -m playwright install chromium`）；不可用时可使用符合条件的本地历史数据，并显示过期提示。两种传输都失败时，tooltip 会显示安全分类后的网络原因，便于排查代理、TLS 或超时问题。源码更新后需重启悬浮窗才能生效。

### 构建轻量启动器 EXE

```powershell
.\build-launcher.ps1
```

该脚本会生成 `dist\MiMo-Token-Monitor.exe`，并把 PyInstaller 的 spec 与中间文件放在 `.build-launcher`，不会覆盖完整发行版的 `MiMo-Token-Monitor.spec`。

### 构建独立完整发行版

```bash
pip install pyinstaller
python -m PyInstaller MiMo-Token-Monitor.spec --clean
```

## 操作

- **拖动**：左键拖动窗口位置（除最小化按钮区域外）
- **窗口吸附**：将 ETF Tracker 与 MiMo Token Monitor 的边框拖到约 15 个像素以内，会自动对齐左右/上下边框；两个窗口必须同时可见且未最小化，主屏和副屏均有效
- **置顶按钮**：点击标题栏图钉图标切换置顶/取消置顶；图标高亮表示当前处于置顶状态，取消置顶时显示斜杠
- **标题栏循环图标**：点击标题旁的图标，在 MiMo Token / WLB / 总览之间循环切换；总览会同时展示 Mimo 套餐用量、SuperGPT API余额、WLB 日/周限额和 GPT 双窗口进度
- **双击悬浮窗**：立即刷新数据
- **右键悬浮窗**：刷新 / 从浏览器导入 / 设置 / 查看原始数据 / 退出
- **悬停悬浮窗**：显示详细 tooltip
- **最小化按钮**：右上角 `─` 按钮，点击最小化到系统托盘
- **系统托盘**：
  - 双击托盘图标：恢复显示悬浮窗
  - 右键托盘图标：显示主窗口 / 刷新 / 从浏览器导入 / Playwright 续期 /
    切换 Claude 配置 / 路由控制 / 重启悬浮窗 / 退出
  - **切换 Claude 配置**：首次运行会把当前 `settings.json` 中的配置保存为
    `API-Claude`。进入二级菜单中的“编辑配置…”即可通过窗口新增、编辑或删除配置，保存后模板会立即刷新。
    配置模板文件为 `C:\Users\你的用户名\.claude\claude_profiles.json`，也可以直接编辑该文件；每个对象只需填写 `name` 和以下 `env` 参数：
    `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、
    `ANTHROPIC_DEFAULT_FABLE_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、
    `ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、
    `CLAUDE_CODE_SUBAGENT_MODEL`。模板格式示例：
    ```json
    [
      {
        "name": "API-GPT",
        "env": {
          "ANTHROPIC_AUTH_TOKEN": "填写 token",
          "ANTHROPIC_BASE_URL": "https://example.com",
          "ANTHROPIC_DEFAULT_FABLE_MODEL": "填写模型名",
          "ANTHROPIC_DEFAULT_OPUS_MODEL": "填写模型名",
          "ANTHROPIC_DEFAULT_SONNET_MODEL": "填写模型名",
          "ANTHROPIC_DEFAULT_HAIKU_MODEL": "填写模型名",
          "CLAUDE_CODE_SUBAGENT_MODEL": "填写模型名"
        }
      }
    ]
    ```
    切换时其他 settings 内容（hooks、权限、插件等）保持不变；Claude Code 已打开的会话通常需要重启后生效。
  - 路由控制：菜单显示“已开启”“已关闭”或“状态未知”；主代理可选择自动（WLB → GPT → SuperGPT）或切换到 WLB / SuperGPT / GPT，子代理可选择自动（GPT → SuperGPT）或手动切换供应商，Pi 可独立切换 WLB / SuperGPT / GPT（官方），也可开启、关闭或重启 Codex Router
  - 重启悬浮窗：退出当前进程并释放单实例锁后启动新实例
  - 悬停托盘图标：显示用量概览

### Codex Router 维护

- 需要先安装 Codex Router；程序默认从
  `~/.codex/codex-router/install-manifest.json` 读取当前源码目录。
- “切换到 WLB”“切换到 SuperGPT”和“切换到 GPT”调用 Router 的
  `gpt-route.mjs wlb|supergpt|official`，不重启服务、不自动开启 Router。请先开启路由；
  主代理切换影响所有任务后续的非子代理原生 GPT 请求。
  菜单勾选已确认的当前服务商，总览对应的 WLB / GPT 整行标题文字显示绿色；WLB
  单独页面的标题也同步变绿。SuperGPT 勾选菜单项并高亮余额标题，不高亮现有的官方 GPT / WLB
  用量栏。官方直连时标记 GPT；状态未知或 Router 不健康时不高亮。
- “自动（WLB → GPT → SuperGPT）”调用 `gpt-route.mjs auto`，仅作用于主代理。
  菜单勾选配置模式 `auto`，用量标题按 Router 返回的 `effectiveMode` 高亮实际档位；首次
  请求尚未判断时不高亮。成功启用后由后续状态轮询读取有效档，失败不提前勾选或猜测路由。
  主代理可用 `route auto` 启用自动模式，按 **WLB → 官方 GPT → SuperGPT** 的顺序选择；
  额度恢复后，后续请求自动回到最高可用档，配置模式始终保持 `auto`。WLB 读取原始 `1d` / `7d`
  窗口，任一额度耗尽即跳过。GPT 同时监控 5 小时和周窗口：5 小时剩余 **≤10%** 或
  周剩余 **≤5%** 时跳过；所有实际存在的窗口均高于阈值时才恢复，缺失窗口不参与判断。
  Node Router 在请求到来时刷新额度，默认缓存 30 秒；没有整机常驻定时器，悬浮窗也不参与额度决策。
  同一请求至多尝试三家、每家一次，只有明确额度失败且尚未输出时才继续降级，SuperGPT 为终点。
  查询失败时保留已有档位；首次无法可靠判断则明确报错，不凭网络、认证或普通限流错误猜测额度。
  子代理 auto 按官方 GPT → SuperGPT 自动切换，阈值与主代理一致；WLB 不进入子代理自动循环。`codex-auto-review` 特殊模型继续使用官方。
- “路由控制 → 子代理供应商”可独立选择 WLB / SuperGPT / GPT（官方），默认官方。
  菜单调用 `gpt-route.mjs subagent auto|wlb|supergpt|official`，同样要求 Router 已开启；
  选择对所有任务后续已识别的原生 GPT 子代理请求生效。主代理和子代理分别勾选已确认的供应商，
  用量标题高亮表示主代理路由。需要配套支持该命令的 Router；首次更新源码后重启 Router
  和悬浮窗以加载新功能，之后切换供应商无需重启。
- “路由控制 → Pi 路由”可独立选择 WLB / SuperGPT / GPT（官方），默认官方，无 auto 模式。
  菜单调用 `gpt-route.mjs pi official|wlb|supergpt`，同样要求 Router 已开启；仅影响 Pi 的
  GPT 请求，Codex 主代理与子代理路由及用量高亮保持不变。需要配套支持 `route pi` 的 Router。
- “开启路由”和“关闭路由”复用路由器的 `codex-router.ps1 enable|disable`，因此会
  同步调整 Codex 配置和后台服务；“重启路由器”只重启现有服务。
- 程序启动后、关闭托盘菜单时及每 30 秒，会在后台调用 `node src/status.mjs --json`，
  缓存并在下次打开菜单时显示路由已开启或已关闭；检测失败时显示“状态未知”。菜单
  关闭后的状态刷新会延迟到事件循环下一轮，避免点击启停后被旧状态刷新竞态覆盖。
  菜单可见或已有操作运行时跳过轮询；切换成功后立即刷新高亮和勾选。
  后台状态检查中点击切换，会等待检查完成后串行执行。
- 所有操作均在后台线程执行。执行期间路由菜单会暂时禁用；悬浮窗底部显示“正在…”状态，
  完成后显示成功或失败摘要并在 5 秒后清除，同时保留托盘通知。为避免中途销毁线程，
  操作完成前不能退出程序。
- 路由源码根目录优先取 `MIMO_TOKEN_MONITOR_ROUTER_ROOT`；未设置时读取 `$CODEX_HOME`
  （默认 `~/.codex`）下 `codex-router/install-manifest.json` 的
  `current.sourceRoot`，并校验所需入口文件。状态使用 `node src/status.mjs --json`，
  启停使用
  `codex-router.ps1 enable|disable`，重启使用 `node src/service.mjs restart`。

### 多屏吸附说明

- 两个程序使用固定的原生窗口标题识别彼此，不会吸附到普通应用窗口或设置对话框。
- 跨窗口吸附在主屏和副屏均启用。Win32 窗口矩形使用物理像素；程序会根据当前屏幕的 `devicePixelRatio` 转换为 Qt 坐标，以保持不同 DPI 显示器上的吸附位置一致。
- 吸附后的最终位置会在松开鼠标时保存，重启后从配置恢复。

## 技术栈

- Python + PyQt6
- 直接调用小米平台 REST API（`/api/v1/tokenPlan/usage`）
- Cookie 认证，支持 CDP（Chrome DevTools Protocol）自动导入
- 配置默认存储于本地外置 SQLite，不通过 Git 同步

## Claude HUD 集成

本程序可以为 [claude-hud](https://github.com/DKalien/claude-hud) 生成用量快照，在 Claude Code 状态栏显示 MIMO 用量。

### 配置步骤

1. 在本程序的设置中，填写**快照路径**：
   ```
   ~/.claude/plugins/claude-hud/mimo-snapshot.json
   ```
   Windows 完整路径示例：`C:\Users\你的用户名\.claude\plugins\claude-hud\mimo-snapshot.json`

2. 在 claude-hud 配置中启用 MIMO 显示（`~/.claude/plugins/claude-hud/config.json`）：
   ```json
   {
     "display": {
       "showMimoUsage": true,
       "mimoSnapshotPath": "~/.claude/plugins/claude-hud/mimo-snapshot.json"
     }
   }
   ```

3. 重启 Claude Code，HUD 会显示：
   ```
   Context ███░░░░░░░ 29% │ Usage ██░░░░░░░░ 25% │ MIMO ██░░░░░░░░ 2% │ 1.9B / 82.0B
   ```

### 工作原理

- 本程序每次刷新数据时，自动写入快照 JSON 文件
- claude-hud 每 ~300ms 读取快照并显示
- 本程序关闭后，快照不再更新，claude-hud 在快照过期后停止显示 MIMO

## 数据存储

- 配置默认存储在外置 SQLite 文件：`D:\python\data\mimo-token-monitor\settings.db`（单表 `settings`，每行一个配置键）。
- 可通过环境变量 `MIMO_TOKEN_MONITOR_DATA_DIR` 覆盖本地数据目录。
- 首次运行时，如果外置库无配置且旧文件 `~/.mimo-widget/config.json` 存在，会自动读取并迁移；旧文件保留，但不再作为主配置来源。
- 数据库仅在本机读写，不执行 Git 拉取、提交或推送；轻量启动器的源码更新功能不受影响。
- **敏感数据提示**：数据库中包含 MiMo / ChatGPT Cookie，`claude_profiles.json` 包含 Claude Auth Token；WLB / SuperGPT Key 使用 Router 的受保护文件，不再写入 SQLite / JSON。上述本机状态均不得加入 Git 或公开仓库。

可用环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MIMO_TOKEN_MONITOR_DATA_DIR` | `D:\python\data\mimo-token-monitor` | 本地数据目录 |

## 隐私

- 配置与快照文件仅在本地存储。未配置 WLB API Key 时不会请求 WLB 服务
- MiMo / ChatGPT Cookie 明文存储在外置 SQLite 数据库（默认 `D:\python\data\mimo-token-monitor\settings.db`）；外置库不可用时可回退到旧 JSON（`~/.mimo-widget/config.json`）。WLB / SuperGPT 密钥由共享存储单独管理，旧字段迁移成功后清除
- MiMo 请求发往 `platform.xiaomimimo.com`；启用 WLB 后，会按配置向第三方 Base URL 发送带 Bearer API Key 的用量请求
- WLB / SuperGPT 密钥每次刷新从对应环境变量或 Router 受保护文件读取，不复制进 SQLite、快照或日志；向各自配置的查询地址发送 Bearer API Key。SuperGPT 默认请求 `https://api.supergpt.dev/v1/usage`，不跟随重定向，未配置密钥时不请求
- GPT 5 小时/周限额查询会优先使用本机 Codex 登录访问 `chatgpt.com`；也可能读取已配置的 ChatGPT Session Cookie 或本地 Codex 会话记录作为备用来源。浏览器兜底使用隔离的临时会话，不读取现有浏览器或 MiMo 登录资料
