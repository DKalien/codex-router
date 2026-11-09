# 托盘菜单底部偶发空白的调查与修复

## 1. 需求（还原）

`mimo-token-monitor` 托盘右键菜单（显示主窗口 / 刷新 / 从浏览器导入 / Playwright 续期 /
切换 Claude 配置 / 路由控制（已开启）/ 重启悬浮窗 / 退出）偶尔渲染异常：

- **唯一真实缺陷**：条目下方多出一整块空白，菜单整体高度明显大于正常值、显得松散。
- 左侧贯穿全高的灰色竖条 = 子菜单展开时的正常视觉，**不是缺陷、不是滚动条**（用户已更正）。
- 正常时是紧凑的纯菜单（8 个条目 + 分隔线，无额外留白）。
- 偶发，无稳定复现步骤。

约束：只改 `mimo-token-monitor/`；不动 Router Node 核心；不新增依赖；改后运行
`python -m unittest discover -s tests`。

## 2. 已完成的取证（结论依据）

### 2.1 缺陷截图的定量结构（PIL 逐行/逐列 run 统计，主屏 DPR=1.0，1 像素 = 1 物理像素）

| 区域 | 观测 |
| --- | --- |
| 窗口外框 | x=1..194、y=0..327 → 约 **196×328** |
| 条目区 | y=1..196，8 条目 + 2 分隔线，行距≈22–23（与正常菜单一致） |
| 空区 | **y=197..321 整片 `#f0f0f0`**（约 126 px） |
| 异常尾行 | y=323–324 全宽 `#f2f2f2`（条目区底色，正常菜单不会出现在底边） |
| 底边框 | y=325 `#cccccc` |
| 右侧 | x≈176..192 约 17 px `#f0f0f0`（正常仅约 3 px） |

### 2.2 正常基线（真实 `TokenWidget` 托盘菜单，8 个场景实测全部正常）

- `sizeHint()` = 192×200（"状态未知"）/ 180×200（"已开启"）；条目高度和 = 197。
- `contentsMargins()` 恒为 `0,0,0,0`；`geometry() == sizeHint()`。
- 覆盖场景：首次 popup、路由子菜单开/关、`_set_router_state`、`_set_gpt_route`、
  `_reload_claude_profile_menu`、连续 5 次 popup/hide、屏幕右下角定位、Claude 子菜单开/关。

### 2.3 注入边距的复现

对同一套 QAction 构建的纯 `QMenu` 施加 `setContentsMargins(0, 0, 16, 126)` 后
`resize(sizeHint())`，渲染结果为 196×326，逐行结构与缺陷截图**完全一致**：
条目区正常 → y≈197..321 全宽 `#f0f0f0` → 底部两行全宽 `#f2f2f2` → `#cccccc` 底边框，
且右侧出现约 17 px 空带。

### 2.4 机制（Qt 6.8 源码，`qmenu.cpp`）

- `QMenu::sizeHint()`（L2276-2285）：条目 `actionRects` 已含 top/left margin，随后只再叠加
  `PM_MenuHMargin + fw + d->rightmargin`（宽）与 `PM_MenuVMargin + fw + d->bottommargin`（高）
  → **窗口尺寸会把 right/bottom contents margin 计入**。
- `QMenuPrivate::updateActionRects()`（L347-480）：条目一律从**左上角**
  （`x = hmargin + fw + leftmargin`，`base_y = vmargin + fw + topmargin ...`）开始排。
- `QMenu::popup()`（L2307-2541）：`aboutToShow` → `updateActionRects(screen)` → `size = sizeHint()`
  → 仅在超屏时 `qMin` 收缩 → `setGeometry(QRect(pos, size))`。**窗口尺寸基本恒等于 sizeHint**。
- `QMenu::paintEvent`：`PE_PanelMenu` 铺满整个窗口，再按 `actionRects` 画条目，剩余区域画
  `CE_MenuEmptyArea`（windowsvista 下 `#f0f0f0`），底色 `#f2f2f2`、边框 `#cccccc`。

注入结果证明非零 right/bottom 边距能够产生相同的空白结构，条目仍从顶部紧排，
右下空间由菜单空区绘制。真实故障当时的 `contentsMargins()` 和 `sizeHint()` 尚未捕获，
截图相似度不足以确定现场也由边距引起。

## 3. 已测范围与剩余缺口

初轮检查与已测场景中未发现以下路径造成异常，结论受当时场景覆盖范围限制。

1. 滚动菜单模式与滚动条（`SH_Menu_Scrollable=0`，条目未超屏，`actionRects` 单列）；
2. 反复 popup 导致的尺寸累积（连续 5 次 popup/hide，`geometry == sizeHint`）；
3. 子菜单展开/返回、`aboutToShow` 清空重建 Claude 子菜单、对可见菜单 `setText`/`setChecked`；
4. 样式表（应用无全局 QSS，QMenu 上无 `setStyleSheet`；全仓仅两处 QLabel 局部样式）；
5. `minimumWidth`/`minimumHeight`、`QWidgetAction`、`tearOff` 分支（全仓无相关调用）；
6. Router Node 核心（与菜单渲染无关，本次不改）。

初轮源码检查时，业务代码未设置托盘父菜单的非零边距。当前已增加归零保护，
但尚无现场证据表明真实故障存在非零边距，也无法确定其潜在写入来源。
下一次复现需要同时记录 Qt 几何、条目区域、屏幕/DPR 与 Windows 原生窗口尺寸。

## 4. 待验证的触发假设

`QSystemTrayIcon` 复用同一个 `_tray_menu` 实例。若其边距在运行期变为非零，
后续弹出可能持续沿用异常尺寸。该假设尚待运行期记录验证，
样式、DPI/主题或其他平台事件也没有被证实为触发条件。

## 5. 已实现的保护与诊断

### 5.1 菜单生命周期

`widget.py` 的 `_setup_tray()` 将父菜单 `aboutToShow` 连接到 `_on_tray_menu_about_to_show()`。
该方法先调用 `_normalize_tray_menu_geometry()`，仅在边距非零时归零，
随后记录首次弹出 probe，并通过 `QTimer.singleShot(0, ...)` 采样显示后的几何信息。
`aboutToHide` 也触发几何采样，菜单 Show 事件由 `_TrayMenuProbeFilter` 记录。

归零发生在 Qt 重新计算弹出布局之前，注入边距的测试验证 `sizeHint()` 能回到基线。
诊断代码同时覆盖尺寸大于 `sizeHint()`、条目下方留白超过阈值及非零边距的情况。

### 5.2 专用诊断文件

系统临时目录中的 `mimo-token-monitor-tray-menu-probe-<pid>.log` 记录启动、菜单显示和首次弹出钩子，
`mimo-token-monitor-tray-menu.log` 只追加检测到的异常，
`mimo-token-monitor-tray-menu.png` 在显示阶段检测到异常时覆盖保存菜单截图。
文件字段与使用步骤见 [监控器 README](../../../mimo-token-monitor/README.md#托盘菜单异常诊断)。

`_normalize_tray_menu_geometry()` 的 stderr 输出仅在控制台可用时可见。
`main.py` 在 `pythonw.exe` 缺少 stderr 时连接 `os.devnull`，因此现场排查依赖专用文件。

### 5.3 回归覆盖

`tests/test_sync_runtime.py` 覆盖四项行为。

- 注入 `(0,0,16,126)` 后，`aboutToShow` 将边距归零并恢复 `sizeHint()`。
- 正常几何不写异常日志，注入异常几何后写入诊断。
- `first-popup` 每个 widget 只写一次。
- `startup` 每个进程、文件只写一次。

## 6. 验证

2026-11-09 的 MiMo 签名修复全量验证包含上述托盘测试，共 316 项通过，0 失败、0 skip，
`node scripts-check.mjs` 通过。托盘代码随后提交为 `291f4361`。

Windows 真实托盘复测仍待完成。操作为弹出菜单、展开与关闭路由子菜单、关闭菜单，重复 10 次以上，
观察菜单高度及底部空白。若复现，记录时间并收集对应 probe、异常日志和菜单截图。
