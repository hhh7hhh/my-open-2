# 日常小帮手 (DailyHelper)

游戏日常自动化管家 — 一键编排多款游戏日常任务，支持进程守护、AI 诊断失败原因、智能恢复。

## 支持的游戏

| 游戏 | 适配引擎 | 功能 |
|------|----------|------|
| 鸣潮 | [ok-ww](https://github.com/ok-oldworld/ok-ww) | 日常任务、刷声骸、肉鸽、锻造、批量强化声骸等 |
| 崩坏：星穹铁道 | [March7thAssistant](https://github.com/ImYrx/March7thAssistant) | 每日实训、清体力、模拟宇宙、忘却之庭等 |

## 特性

- **YAML 配置驱动** — 在 `config/tools.yaml` 中声明工具和任务，开箱即用
- **插件式适配器** — 新增游戏只需实现 `BaseAdapter` 接口
- **进程守护 (Guardian)** — 实时监控窗口僵死、CPU 假死、进程意外退出
- **AI 诊断** — 集成 DeepSeek，任务失败时自动分析日志并给出修复建议
- **v2 智能管道** — 错误模式库 + 恢复引擎，自动重试/重启/跳过
- **战报生成** — 每次运行生成 JSON 格式战报

## 项目结构

```
├── main.py                  # v1 入口（基础调度）
├── main_v2.py               # v2 入口（智能管道）
├── config/
│   └── tools.yaml           # 工具与任务配置
├── core/
│   ├── scheduler.py         # 任务调度器
│   ├── guardian.py          # 进程守护引擎
│   ├── deepseek.py          # AI 诊断客户端
│   └── battle_report.py     # 战报生成
├── core_v2/
│   ├── error_patterns.py    # 错误模式库（13种模式）
│   ├── log_analyzer.py      # 日志分析器
│   ├── recovery.py          # 恢复决策引擎
│   └── pipeline.py          # 一条龙管道编排
├── adapters/
│   ├── base_adapter.py      # 适配器基类
│   ├── okww_adapter.py      # 鸣潮适配器
│   └── m7a_adapter.py       # 星铁适配器
└── gui/
    └── __init__.py          # GUI 模块（预留）
```

## 环境要求

- Windows 10/11
- Python 3.9+
- 各游戏对应的自动化工具已安装并配置好

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config/tools.yaml`：

```yaml
tools:
  - name: "鸣潮"
    exe_path: "D:/your/path/to/ok-ww.exe"   # ← 改为你的路径
    tasks:
      - id: "DailyTask"
        enabled: true                        # ← 启用的任务
```

如需 AI 诊断，填入 DeepSeek API Key：

```yaml
deepseek:
  api_key: "sk-xxxxxxxx"   # ← 填入你的 Key（可选）
```

### 3. 运行

```bash
# v2 智能管道（推荐）
python main_v2.py

# 只跑鸣潮
python main_v2.py --tool 鸣潮

# 仅预览计划
python main_v2.py --dry-run
```

```bash
# v1 基础调度
python main.py
```

## 自定义适配器

实现 `BaseAdapter` 即可接入新游戏：

```python
from adapters.base_adapter import BaseAdapter

class MyAdapter(BaseAdapter):
    @property
    def name(self) -> str:
        return "my_game"

    @property
    def exe_name(self) -> str:
        return "MyGame.exe"

    @property
    def needs_window(self) -> bool:
        return True

    def launch(self, task_name: str, task_args: str = "") -> subprocess.Popen:
        return subprocess.Popen(
            [self._exe_path, task_args],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
```

## License

MIT