"""
战报系统
"""
import json
import os
from datetime import datetime
from typing import Optional
from adapters.base_adapter import TaskResult, TaskStatus


class BattleReporter:
    def __init__(self, report_dir=""):
        if not report_dir:
            report_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        self._report_dir = report_dir
        os.makedirs(self._report_dir, exist_ok=True)

    def save(self, results):
        now = datetime.now()
        filename = f"report_{now.strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self._report_dir, filename)
        total = len(results)
        success = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
        failed = sum(1 for r in results if r.status == TaskStatus.FAILED)
        report = {
            "timestamp": now.isoformat(),
            "summary": {
                "total": total,
                "success": success,
                "failed": failed,
                "success_rate": f"{success/total*100:.1f}%" if total > 0 else "0%",
            },
            "tasks": [],
        }
        for r in results:
            duration = round(r.end_time - r.start_time, 1) if r.start_time and r.end_time else 0
            report["tasks"].append({
                "tool": r.tool_name,
                "task_id": r.task_id,
                "task_name": r.task_name,
                "status": r.status.value,
                "duration_seconds": duration,
                "restart_count": r.restart_count,
                "error": r.error_message[:300] if r.error_message else "",
                "exit_code": r.exit_code,
            })
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return filepath

    def list_reports(self):
        reports = []
        if not os.path.exists(self._report_dir):
            return reports
        for filename in sorted(os.listdir(self._report_dir), reverse=True):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(self._report_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data["_file"] = filepath
                    reports.append(data)
            except Exception:
                pass
        return reports

    def load(self, filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None