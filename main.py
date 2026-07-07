"""
日常小帮手 — 主入口

用法：
  python main.py              # 启动 GUI
  python main.py --headless   # 无头模式（命令行执行所有启用的任务）
"""
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    if "--headless" in sys.argv:
        run_headless()
    else:
        run_gui()


def run_gui():
    from gui import run
    run()


def run_headless():
    import logging
    import time
    import yaml
    from adapters import OkwwAdapter, M7aAdapter, TaskDef, TaskResult, TaskStatus
    from core import Scheduler, BattleReporter, DeepSeekClient, Guardian

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # PyInstaller 打包后 __file__ 指向临时目录，用 sys._MEIPASS 找嵌入资源
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config", "tools.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    tools_cfg = config.get("tools", [])
    settings = config.get("settings", {})

    # 初始化
    adapters = {}
    for tool in tools_cfg:
        if tool.get("adapter") == "okww_adapter":
            adapter = OkwwAdapter()
        elif tool.get("adapter") == "m7a_adapter":
            adapter = M7aAdapter()
        else:
            continue
        adapter.load_config(tool)
        adapters[tool["name"]] = adapter

    reporter = BattleReporter()
    guardian = Guardian(
        cpu_dead_threshold=settings.get("cpu_dead_threshold", 0.0),
        cpu_dead_duration=settings.get("cpu_dead_duration", 10),
        hung_check_interval=settings.get("hung_check_interval", 5),
    )
    scheduler = Scheduler(guardian=guardian)

    max_restarts = settings.get("max_restart_count", 3)
    interval = settings.get("task_interval_seconds", 3)
    default_timeout = settings.get("default_timeout_minutes", 60) * 60

    results = []

    for tool in tools_cfg:
        name = tool["name"]
        adapter = adapters.get(name)
        if not adapter or not tool.get("enabled", True):
            continue

        for task_cfg in tool.get("tasks", []):
            if not task_cfg.get("enabled", False):
                continue

            td = TaskDef(
                task_id=task_cfg["id"],
                task_name=task_cfg["name"],
                args=task_cfg.get("args", ""),
                desc=task_cfg.get("desc", ""),
                eta=task_cfg.get("eta", 30),
            )

            logging.getLogger("headless").info(f"▶ 开始: {name} - {td.task_name}")

            cmd = adapter.build_command(td)
            result = scheduler.run(
                cmd,
                task_id=f"{name}::{td.task_id}",
                task_name=f"{name} - {td.task_name}",
                tool_name=name,
                timeout=default_timeout,
                max_restarts=max_restarts,
                cwd=os.path.dirname(adapter.exe_path),
                game_exe=adapter.game_exe,
                needs_window=adapter.needs_window,
                daemon_launcher=adapter.daemon_launcher,
            )

            result.task_name = td.task_name
            result.tool_name = name
            results.append(result)

            status = "OK" if result.status == TaskStatus.SUCCESS else "FAIL"
            logging.getLogger("headless").info(
                f"{'✅' if result.status == TaskStatus.SUCCESS else '❌'} "
                f"{status}: {name} - {td.task_name} | 重启{result.restart_count}次"
            )

            if interval > 0:
                time.sleep(interval)

    # 保存战报
    report_path = reporter.save(results)
    logging.getLogger("headless").info(f"战报已保存至 {report_path}")

    # 打印汇总
    total = len(results)
    success = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
    print(f"\n{'='*40}")
    print(f"全部完成: {success}/{total} 成功")
    print(f"战报: {report_path}")
    print(f"{'='*40}")


if __name__ == "__main__":
    main()