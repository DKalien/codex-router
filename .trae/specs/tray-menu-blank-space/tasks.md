# Tasks：托盘菜单底部空白区修复

- [x] 1 注册 `aboutToShow` 归一化钩子 — `mimo-token-monitor/widget.py` `_setup_tray()`（`aboutToHide`
  连接之后，L1180-1183）：`tray_menu.aboutToShow.connect(self._normalize_tray_menu_geometry)`。
- [x] 2 新增 `_normalize_tray_menu_geometry()` — `mimo-token-monitor/widget.py` L1208-1226，紧邻
  `_setup_claude_profile_menu`；边距非零时归零并向 stderr 打印
  `[tray] QMenu contentsMargins=...`，为 0 时零写入。
- [x] 3 新增回归测试 — `mimo-token-monitor/tests/test_sync_runtime.py` L203-213
  `test_tray_menu_about_to_show_resets_stale_contents_margins`：注入 `(0,0,16,126)` 后
  `aboutToShow` 使 margins 归零且 `sizeHint()` 回到基线。
- [x] 4 运行测试门禁 — `cd mimo-token-monitor && python -m unittest discover -s tests`：
  `Ran 303 tests ... OK`（首轮出现的 `test_code_sync.test_check_updates_reports_scope_without_changing_worktree`
  失败在单独复跑时通过，属既有全量套件下的 git 临时仓库偶发竞争，与本次改动无关）。
- [ ] 5 手工验证 — Windows 桌面：需用户在真实托盘上重复"弹出菜单 → 展开/关闭子菜单"10+ 次，
  确认高度稳定、底部无空白；若再复现，收集启动日志中的 `[tray] QMenu contentsMargins=` 行。

范围声明：未改 `src/**`（Router Node 核心）、未改 `mimo-token-monitor` 其它模块、未新增依赖。
