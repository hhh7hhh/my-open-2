"""
日常小帮手 v2 — 一条龙全自动执行入口
=====================================
用法:
    python main_v2.py                      # 按 tools.yaml 顺序执行所有启用的一龙任务
    python main_v2.py --tool 鸣潮            # 只跑鸣潮的一条龙
    python main_v2.py --dry-run             # 仅打印计划，不实际执行
"""
import argparse
import logging
import sys
import os
import time
import yaml
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adapters.okww_adapter import OkwwAdapter
from adapters.m7a_adapter import M7aAdapter
from adapters.base_adapter import BaseAdapter, TaskDef
from core.deepseek import DeepSeekClient
from core_v2.pipeline import OneDragonPipeline, PipelineResult, PipelineStatus

# ─── Logging ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "reports", "pipeline_v2.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main_v2")

# ─── Adapter registry ────────────────────────────
ADAPTER_MAP: dict[str, type[BaseAdapter]] = {
    "okww_adapter": OkwwAdapter,
    "m7a_adapter": M7aAdapter,
}


def load_config(path: str = None) -> dict:
    """Load tools.yaml configuration."""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config", "tools.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_adapter(tool_cfg: dict) -> "Optional[BaseAdapter]":
    """Build an adapter instance from tool config."""
    adapter_name = tool_cfg.get("adapter", "")
    adapter_cls = ADAPTER_MAP.get(adapter_name)
    if not adapter_cls:
        logger.warning(f"Unknown adapter: {adapter_name}, skipping {tool_cfg.get('name')}")
        return None
    adapter = adapter_cls()
    adapter.load_config(tool_cfg)
    return adapter


def build_deepseek(cfg: dict) -> "Optional[DeepSeekClient]":
    """Build DeepSeek client from config."""
    ds = cfg.get("deepseek", {})
    if not ds.get("enabled"):
        return None
    api_key = ds.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("DeepSeek enabled but no api_key configured. AI diagnosis disabled.")
    return DeepSeekClient(
        api_key=api_key,
        base_url=ds.get("base_url", "https://api.deepseek.com"),
        model=ds.get("model", "deepseek-chat"),
        daily_budget_yuan=float(ds.get("daily_budget_yuan", 1.0)),
        max_retry_per_error=int(ds.get("max_retry_per_error", 2)),
    )


def find_one_dragon_task(adapter: BaseAdapter) -> "Optional[TaskDef]":
    """Find the '一条龙' task in an adapter's task list.
    Priority: DailyTask (okww) / main (m7a) — the full daily automation task."""
    one_dragon_ids = {"DailyTask", "main", "daily", "routine"}
    for task in adapter.get_enabled_tasks():
        if task.task_id in one_dragon_ids:
            return task
    # Fallback: first enabled task
    enabled = adapter.get_enabled_tasks()
    return enabled[0] if enabled else None


def run_all(cfg: dict, dry_run: bool = False, tool_filter: str = None):
    """Run all enabled one-dragon tasks across all tools."""
    tools = cfg.get("tools", [])
    settings = cfg.get("settings", {})
    deepseek = build_deepseek(cfg)

    results: list[PipelineResult] = []
    start_time = time.time()

    for tool_cfg in tools:
        # Skip disabled
        if not tool_cfg.get("enabled", True):
            logger.info(f"Skipping disabled tool: {tool_cfg.get('name')}")
            continue

        # Tool filter
        if tool_filter and tool_cfg.get("name") != tool_filter:
            continue

        adapter = build_adapter(tool_cfg)
        if not adapter:
            continue

        task = find_one_dragon_task(adapter)
        if not task:
            logger.warning(f"No one-dragon task found for {adapter.name}, skipping")
            continue

        if dry_run:
            logger.info(f"[DRY-RUN] Would run: {adapter.name} -> {task.task_name} (args={task.args})")
            continue

        # Build pipeline
        pipeline = OneDragonPipeline(adapter, deepseek)
        pipeline.set_exe_path(adapter.exe_path)

        timeout = task.timeout_minutes or settings.get("default_timeout_minutes", 60)
        logger.info(f"{'='*60}")
        logger.info(f"Starting: {adapter.name} -> {task.task_name} (timeout={timeout}m)")
        logger.info(f"{'='*60}")

        try:
            result = pipeline.run(task, timeout_minutes=timeout)
            results.append(result)
            print_result(result)
        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            pipeline.stop()
            break
        except Exception as e:
            logger.exception(f"Pipeline crashed for {adapter.name}: {e}")

        # Buffer between tools
        interval = settings.get("task_interval_seconds", 3)
        logger.info(f"Waiting {interval}s before next tool...")
        time.sleep(interval)

    # ── Summary ──
    total_duration = time.time() - start_time
    print_summary(results, total_duration, dry_run)


def print_result(result: PipelineResult):
    """Print a single pipeline result."""
    icon = {PipelineStatus.SUCCESS: "✓", PipelineStatus.PARTIAL: "⚠", PipelineStatus.FAILED: "✗"}.get(
        result.status, "?"
    )
    logger.info(
        f"  {icon} {result.tool_name} {result.task_name}: {result.status.value} "
        f"({result.duration_seconds:.0f}s, retries={result.retry_count}, skips={result.stages_skipped})"
    )
    if result.final_error:
        logger.info(f"    Error: {result.final_error}")
    for entry in result.recovery_log:
        logger.info(f"    {entry}")


def print_summary(results: list[PipelineResult], total_duration: float, dry_run: bool):
    """Print overall execution summary."""
    if dry_run:
        logger.info("\n=== DRY-RUN Complete ===")
        logger.info(f"Would execute {len([r for r in results if r.status != PipelineStatus.PENDING])} tasks")
        return

    total = len(results)
    success = sum(1 for r in results if r.status == PipelineStatus.SUCCESS)
    partial = sum(1 for r in results if r.status == PipelineStatus.PARTIAL)
    failed = sum(1 for r in results if r.status == PipelineStatus.FAILED)

    logger.info(f"\n{'='*60}")
    logger.info(f"=== Pipeline Summary ===")
    logger.info(f"  Total: {total}  Success: {success}  Partial: {partial}  Failed: {failed}")
    logger.info(f"  Duration: {total_duration:.0f}s ({total_duration/60:.1f}min)")
    logger.info(f"{'='*60}")

    if failed > 0:
        logger.warning("Some tasks FAILED. Check reports/pipeline_v2.log for details.")

    # Write summary report
    report_path = os.path.join(os.path.dirname(__file__), "reports", "pipeline_summary.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"Pipeline Summary — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*60}\n")
            f.write(f"Total: {total}  Success: {success}  Partial: {partial}  Failed: {failed}\n")
            f.write(f"Duration: {total_duration:.0f}s ({total_duration/60:.1f}min)\n\n")
            for r in results:
                f.write(f"  [{r.status.value.upper()}] {r.tool_name} -> {r.task_name}\n")
                f.write(f"    retries={r.retry_count}  skips={r.stages_skipped}  restart={r.restart_count}  AI={r.ai_calls}\n")
                if r.final_error:
                    f.write(f"    error: {r.final_error}\n")
                f.write("\n")
        logger.info(f"Report saved: {report_path}")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="日常小帮手 v2 — 一条龙自动执行")
    parser.add_argument("--tool", type=str, default=None, help="只执行指定工具（如：鸣潮）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印执行计划")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径（默认 config/tools.yaml）")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("日常小帮手 v2 — One-Dragon Pipeline Engine")
    logger.info("=" * 60)

    cfg = load_config(args.config)
    run_all(cfg, dry_run=args.dry_run, tool_filter=args.tool)


if __name__ == "__main__":
    main()