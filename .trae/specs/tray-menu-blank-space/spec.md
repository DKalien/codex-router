# Spec：托盘菜单底部多出空白区（偶发）——根因与最小修复

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

### 2.3 像素级复现（决定性）

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

**结论（机制层已证实）**：异常菜单的窗口高度来自一个 right≈16 / bottom≈126 的
`sizeHint()`，即 `QWidget::contentsMargins()` 的 right/bottom 被计入了菜单尺寸；条目仍从顶部紧排，
右下多出的空间被 `CE_MenuEmptyArea` 填成灰白空块，面板与边框贯穿全高。正常菜单该边距恒为 0。

## 3. 已排除项 / 剩余缺口

已排除（均有证据）：

1. 滚动菜单模式与滚动条（`SH_Menu_Scrollable=0`，条目未超屏，`actionRects` 单列）；
2. 反复 popup 导致的尺寸累积（连续 5 次 popup/hide，`geometry == sizeHint`）；
3. 子菜单展开/返回、`aboutToShow` 清空重建 Claude 子菜单、对可见菜单 `setText`/`setChecked`；
4. 样式表（应用无全局 QSS，QMenu 上无 `setStyleSheet`；全仓仅两处 QLabel 局部样式）；
5. `minimumWidth`/`minimumHeight`、`QWidgetAction`、`tearOff` 分支（全仓无相关调用）；
6. Router Node 核心（与菜单渲染无关，本次不改）。

**剩余缺口**：`rightmargin`/`bottommargin` 只能由 `QWidget::setContentsMargins` 写入；
Qt 内 `QMenu` 自身仅 torn-off 分支调用（`qmenu.cpp` L110），而本仓库**全无** `setContentsMargins`
调用。故"运行期是谁写入该边距"无法靠静态分析闭环，需在下次真实出现时由运行期证据确认。

## 4. 触发条件与"偶发"成因（当前最佳解释）

`QSystemTrayIcon` 复用**同一个** `_tray_menu` 实例（`widget.py` L1082/L1196），其
`contentsMargins` 一旦被置为非零就会被后续每次 `sizeHint()` 继承，直到进程退出；
`popup()` 也只按 `sizeHint()` 给窗口尺寸，不会纠正。因此表现为：

- 只在某个外部/平台侧事件（样式 polish、DPI/主题切换、其它窗口 setContentsMargins 误用等）
  写入边距的那一次开始出现；
- 之后的每次弹出都保持异常 → 用户感觉"偶发出现、抓不住"，且重启悬浮窗即恢复。

这是"偶发"的直接解释：**状态一旦被污染就持久化在复用实例上，而不是每次弹出都可能发生**。

## 5. 修复方案（最小范围，一处逻辑 + 一处测试）

### 5.1 `mimo-token-monitor/widget.py`

1. 在 `_setup_tray()` 中注册父菜单的 `aboutToShow` 归一化钩子（当前父菜单只有 `aboutToHide`）：

```python
tray_menu.aboutToShow.connect(self._normalize_tray_menu_geometry)
```

2. 新增方法（紧邻 `_setup_claude_profile_menu`）：

```python
def _normalize_tray_menu_geometry(self) -> None:
    """Keep the reused tray menu compact.

    QMenu::sizeHint() adds QWidget::contentsMargins() right/bottom to the
    action rects, while items are still laid out from the top-left corner,
    so a stale non-zero margin renders a blank CE_MenuEmptyArea strip.
    """
    menu = getattr(self, "_tray_menu", None)
    if menu is None:
        return
    margins = menu.contentsMargins()
    if margins.left() or margins.top() or margins.right() or margins.bottom():
        print(
            "[tray] QMenu contentsMargins="
            f"{margins.left()},{margins.top()},{margins.right()},{margins.bottom()}"
            " 已归零",
            file=sys.stderr,
        )
        menu.setContentsMargins(0, 0, 0, 0)
```

- `aboutToShow` 在 `popup()` 的 `updateActionRects()` **之前**触发，因此归零后本次
  `sizeHint()` 立即按内容计算，不需要 `adjustSize()`（避免隐藏态下按陈旧 `actionRects` 计算）。
- 边距为 0 时不做任何写入（正常路径行为完全不变，无 flicker）。
- 若下次复现，stderr 会打印真实边距值，即为**运行期根因证据**（`main.py` 已把 stderr 落盘）。

### 5.2 `mimo-token-monitor/tests/test_sync_runtime.py`

新增回归测试（复用现有 `managed_widget` 上下文）：

```python
def test_tray_menu_about_to_show_resets_stale_contents_margins(self):
    with managed_widget({"position": [100, 100]}) as widget:
        menu = widget._tray_menu
        baseline = menu.sizeHint()
        menu.setContentsMargins(0, 0, 16, 126)   # 缺陷截图对应的边距
        inflated = menu.sizeHint()
        self.assertGreater(inflated.height(), baseline.height())
        menu.aboutToShow.emit()
        self.assertEqual(menu.contentsMargins().right(), 0)
        self.assertEqual(menu.contentsMargins().bottom(), 0)
        self.assertEqual(menu.sizeHint(), baseline)
```

## 6. 验证

1. `cd mimo-token-monitor && python -m unittest discover -s tests` 全绿；
2. `node scripts-check.mjs`、Router 侧检查不涉及（未改动 `src/`）；
3. Windows 手工验证：
   - 正常：托盘右键 → 菜单紧凑、条目下方无空块（8 条目 + 分隔线）；
   - 故障注入：开一次 Python 交互会话构造同样菜单并 `setContentsMargins(0,0,16,126)`，
     确认复现截图后，再验证 `aboutToShow` 钩子能把高度拉回 `sizeHint()`；
   - 真实环境：菜单弹出 → 悬停"路由控制"展开子菜单 → 移开 → 关闭 → 重复 10 次以上，
     菜单高度不变、底部无空白；若再出现，收集 stderr 中的 `[tray] QMenu contentsMargins=...` 行。
