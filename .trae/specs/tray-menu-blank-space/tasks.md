# 托盘菜单底部空白区修复任务

- [x] 1 在 `_setup_tray()` 连接 `aboutToShow` 到 `_on_tray_menu_about_to_show()`，弹出前归零非零边距。
- [x] 2 增加启动、菜单显示、首次弹出 probe，以及异常几何日志和菜单截图，文件位置见
  [监控器 README](../../../mimo-token-monitor/README.md#托盘菜单异常诊断)。
- [x] 3 在 `tests/test_sync_runtime.py` 覆盖边距归零、异常日志、首次弹出和每进程启动记录。
- [x] 4 2026-11-09 全量验证包含托盘测试，`python -m unittest discover -s tests` 共 316 项通过，
  0 失败、0 skip，`node scripts-check.mjs` 通过，代码已提交为 `291f4361`。
- [ ] 5 Windows 真实托盘复测。重复弹出菜单与展开/关闭子菜单 10 次以上，确认高度稳定。
  若复现，记录时间并收集对应进程 probe、异常日志与菜单截图，继续判断现场触发原因。

托盘改动范围为 `mimo-token-monitor/widget.py`、相邻测试及调查资料。
