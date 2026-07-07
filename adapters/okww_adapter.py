"""
鸣潮 (okww) 适配器

启动方式: ok-ww.exe -t <任务编号> -e
  -t N: 任务编号 (1-11)
  -e: 执行完成后自动退出
"""
from .base_adapter import BaseAdapter, TaskDef


class OkwwAdapter(BaseAdapter):
    name = "鸣潮"
    icon = "wave"
    game_exe = "Client-Win64-Shipping.exe"   # ok-ww 实际控制的游戏进程
    needs_window = True      # Electron 程序需要窗口渲染 EBWebView
    daemon_launcher = True   # ok-ww 启动器是守护进程（永不退出），调度器直接监控游戏进程

    # okww 任务编号映射 (与 tools.yaml 中 args 对应)
    TASK_ID_MAP = {
        "DailyTask": "1",
        "MultiAccountDailyTask": "2",
        "FarmEchoTask": "3",
        "AutoRogueTask": "4",
        "ForgeryTask": "5",
        "NightmareNestTask": "6",
        "SimulationTask": "7",
        "TacetTask": "8",
        "EnhanceEchoTask": "9",
        "ChangeEchoTask": "10",
        "GardenTask": "11",
    }

    def build_command(self, task: TaskDef) -> list[str]:
        task_num = task.args or self.TASK_ID_MAP.get(task.task_id, "1")
        return [self.exe_path, "-t", task_num, "-e"]
