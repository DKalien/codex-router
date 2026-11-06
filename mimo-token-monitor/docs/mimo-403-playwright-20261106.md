# MiMo 403 与 Playwright 续期调查（2026-11-06）

起点：`lite` / `09725a86`，工作区干净。只读取仓库实现和已安装依赖元数据，
未读取真实 Cookie、Token、settings.db、浏览器 profile 或账户数据；没有访问真实账户，未改 Router/Pi 配置，未执行远端 Git 写操作。

## 执行路径与边界

1. `launcher.py` → `main.py` → `config.load_config()`：外置 SQLite，旧 JSON 回退/迁移，默认 Cookie 为空；
   数据目录由 `MIMO_TOKEN_MONITOR_DATA_DIR` 决定。`main.py` 的 Windows 单实例锁保护正常入口，
   没有为单独调用 `playwright_session.refresh_cookie()` 提供跨进程锁，Chromium 自己拒绝占用中的 profile。
2. 设置中的手动 Cookie/浏览器导入写入设置；`cookie_reader` 优先 CDP（9222/9223），再尝试 browser_cookie3。
   读取到 Cookie 不等于已确认 API 登录有效。快速导入会调用余额校验；日常浏览器 profile 不会共享给 Playwright。
   本次保留导入和 Cookie 域筛选方式，没有读取/修改日常浏览器或真实导入数据。
3. `TokenWidget` 的定时器 → `FetchWorker.run()`：先余额，再依次尝试最多四个用量端点；WLB/GPT/SuperGPT 各自工作线程独立运行。
   原 UI 余额错误优先，可能掩盖同时发生的用量错误；其他供应商数据也可能是保留的历史成功结果，不能反证 MiMo 正常。
4. MiMo Requests 使用 `Session(trust_env=False)`、手动 Cookie/UA/Referer、10 秒请求 timeout；
   不读取环境代理且保持 TLS 验证。Chromium 没有显式代理参数，可能使用不同的系统网络/证书环境。
   本次没有改 UA、代理、TLS 校验，也没有增加风控规避或浏览器 API 代发。
5. 手动续期 → `PlaywrightCookieWorker(interactive=True)` → 独立 persistent Chromium，headed，180 秒登录等待；
   定时和 401 恢复使用 headless，30 秒。Playwright 对象均由创建它的同一个 QThread 同步使用并在 finally 中关闭。
   新增共享校验路径：Cookie 必须通过真实运行时的余额校验才能保存，只有明确 401 才等待下一份 Cookie。
6. 结果以 `result_ready` 交还 UI；结果槽等线程退出再销毁对象，避免信号发出后 run 尚未结束的销毁竞态。
   原退出路径只等待五秒就让应用退出；现有续期线程运行时提示先结束任务，避免销毁运行中的 QThread。

## 已证实并修复

- 在 fake context 上让 `cookies()` 抛出 `TargetClosedError`：原实现三个模拟轮询周期调用三次，最终仅报告读取失败；
  新实现第一次就终止。只有 Cookie 缺失/SSO 未完成可继续等待，其他驱动/读取错误也不会重试到 180 秒。
- 增加 context close 和 page crash 事件，保留首先观察到的原因；错误只显示阶段、headed/headless、异常类型和固定提示，
  不回显 Playwright 原始日志、URL、profile 路径或 Cookie。profile 锁的已知错误标记会转成安全的占用提示。
- 初始 SSO 等待异常原先未经过统一包装，SSO 回跳导航异常又被吞掉；现在统一保留阶段并停止终止性故障。
  等待使用 Playwright 自己的等待方法以处理事件；取消会打断人工登录的主动回跳导航。
- balance 原先对任何可解析的 HTTP 200 JSON 返回 `ok=True`，包括业务错误/空对象；
  validator 因而可能过早结束登录。现在要求有限数值，保留零值和既有数字/数字字符串形态；
  `code` 缺失、0、200（含相应字符串）可接受，其他业务 code 不被推定为登录成功或 Cookie 过期。
- HTTP 403 保留为拒绝访问，带端点和 Content-Type 分类；HTML/JSON 只是响应类型，不推断风控或认证根因。
  余额和全部用量失败现在均在 tooltip 保留；请求异常只输出安全分类，重定向不跟随，响应均关闭。
- 明确 `auth_expired` 取代错误文本中的“过期”匹配。相同已被 401 拒绝的候选不重复请求；
  403、网络/TLS/业务错误在校验时立即返回。自动续期成功不立即重置恢复保护，直到余额和用量都成功，
  防止余额正常但用量持续 401 时形成恢复循环。手动续期/成功快速导入仍可重试。

## 验证

相关测试命令（工作目录 `mimo-token-monitor/`）：

- `python -m unittest discover -s tests -p test_api_client.py -v`：59 通过，0 失败、0 skip。
- `python -m unittest discover -s tests -p test_playwright_session.py`：22 通过，0 失败、0 skip。
- `python -m unittest discover -s tests -p test_widget_overview.py`：89 通过，0 失败、0 skip。
- `python -m unittest discover -s tests`：300 通过（原有 283 项 + 17 项新增），0 失败、0 skip；100.063 秒。

Router 根目录：`node scripts-check.mjs` 通过；`python -m compileall -q mimo-token-monitor` 通过；`git diff --check` 通过。
新用例用 fake Cookie/Key、mock HTTP 与回环 HTTP 服务，不调用真实账户。
最早一次全量 284 项（原有 283 项 + 首个新用例）也通过，随后 296 项通过。
两次完整运行均出现 SQLite `ResourceWarning: unclosed database`，未升级为失败；与本次 MiMo 路径无关，没有扩大修改数据库测试。

隔离 Chromium 探针（Playwright 1.62.0 / Requests 2.32.5 / PyQt6 6.11.0）：

- 每次使用 `TemporaryDirectory(prefix="mimo-isolated-smoke-")` 的新 profile，
  `launch_options={"timeout":10000,"args":["--disable-background-networking","--no-proxy-server"]}`，
  context 建立后 `route("**/*", route.abort)`，只打开 `about:blank`；添加一个假 serviceToken Cookie 并断言读取结果，
  关闭 context 后断言返回会话关闭错误，最后 `session.close()`，临时目录只由 TemporaryDirectory 清理。
- headed：假 Cookie 读取与主动关闭后的立即报错通过（约 13.14 秒）。这是人工关闭上下文复现，不是实际崩溃复现。
- headless：沙箱内和沙箱外均在 Chromium 启动阶段超时；没有访问 MiMo，没有触碰真实 profile。
  这验证了本机存在 headless 启动不确定性，不能证明它就是用户的 TargetClosedError 根因。
- 没有重新安装浏览器、杀其他进程、清理真实 profile 或调整安全软件。

## 仍未知与下一步

没有对真实 MiMo 账户发请求，因而未证实截图当时具体哪个 endpoint、响应类型、业务 code、Cookie 或网络出口。
也没有捕获用户 Chromium 的退出码/崩溃转储，无法在“用户关窗、外部进程结束、崩溃、安全软件、profile 冲突”之间下结论。

用户重启悬浮窗后可按 README 的安全步骤记录 API 路径/状态/类型，并在日常浏览器 DevTools 中比较平台自己发出的相同 API。
只记录这些非敏感字段，不导出 HAR、Cookie、请求头或响应正文。若独立浏览器自行退出，检查 Windows 可靠性监视器对应时间的事件。
持续 403 应向平台确认 API 权限/策略，停止反复续期，不尝试绕过风控。

剩余实现限制：Requests timeout 是连接/读取闲置超时，不是严格总时限；余额 validator 一次调用和 Chromium 启停可能越过登录等待预算。
快速导入和续期失败后的旧 Cookie 校验仍在 UI 槽内同步执行，可能短暂影响 UI 响应，本次没有扩大为全部 GUI 网络任务重构。

## API 依据

本地已安装 Playwright 的 cookies 签名及 TargetClosedError 继承结构已核对。
官方参考：[BrowserContext cookies / close](https://playwright.dev/python/docs/api/class-browsercontext)、
[Page crash](https://playwright.dev/python/docs/api/class-page#page-event-crash)、
[同步库的 time.sleep 限制](https://playwright.dev/python/docs/library#time-sleep-leads-to-outdated-state)。
