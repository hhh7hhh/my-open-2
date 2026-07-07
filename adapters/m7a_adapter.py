"""
星铁 (March7thAssistant) 适配器

启动方式: March7th Launcher.exe <子任务名>
  daily, routine, power, main, fight, universe, divergent, 
  currencywars, forgottenhall, purefiction, apocalyptic, redemption
"""
from .base_adapter import BaseAdapter, TaskDef


class M7aAdapter(BaseAdapter):
    name = "星铁"
    icon = "train"
    game_exe = "StarRail.exe"
    needs_window = True     # pyappify 打包的 Electron 程序，需要管理员权限

    def build_command(self, task: TaskDef) -> list[str]:
        return [self.exe_path, task.args]