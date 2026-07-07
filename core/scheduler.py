"""
调度引擎 — 串行任务调度、超时保护、重启策略
"""
import os
import time
import subprocess
import logging
from typing import Optional
from datetime import datetime

from adapters.base_adapter import TaskResult, TaskStatus

logger = logging.getLogger("scheduler")


class Scheduler:
    """
    调度引擎 - 简化版

    用于执行任务（启动进程 → 等待 → 超时/重启 → 返回结果）。
    支持 Guardian 监护引擎进行后台监控。
    支持 pyappify 启动器模式（needs_window=True）— 启动器退出后
    自动等待游戏进程启动并退出，避免重复启动。
    """

    def __init__(self, guardian=None):
        """
        :param guardian: 可选的 Guardian 监护引擎实例
        """
        self._guardian = guardian
        self._results: list[TaskResult] = []

    def run(
        self,
        cmd: list[str],
        task_id: str = "",
        task_name: str = "",
        tool_name: str = "",
        timeout: float = 3600,
        max_restarts: int = 3,
        cwd: str = "",
        game_exe: str = "",
        needs_window: bool = False,
        daemon_launcher: bool = False,
    ) -> TaskResult:
        """
        执行一个任务

        :param cmd: 命令行参数列表
        :param task_id: 任务ID
        :param task_name: 任务名称
        :param tool_name: 工具名称
        :param timeout: 超时秒数
        :param max_restarts: 最大自动重启次数
        :param cwd: 启动器工作目录（exe 所在目录）
        :param game_exe: 游戏进程名（任务结束后清理用）
        :param needs_window: True=工具需要 GUI 窗口（如 Electron 程序）
        :param daemon_launcher: True=启动器是守护进程，跳过 proc.wait 直接监控游戏进程
        :return: TaskResult
        """
        result = TaskResult(
            tool_name=tool_name,
            task_id=task_id,
            task_name=task_name,
        )

        for attempt in range(max_restarts + 1):
            result.restart_count = attempt
            result.status = TaskStatus.RUNNING
            result.start_time = time.time()
            result.error_message = ""

            logger.info(
                f"▶ [{tool_name}][{task_id}] {task_name} "
                f"第 {attempt + 1} 次执行"
            )

            # 启动进程
            try:
                popen_kwargs = {
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
                if os.name == 'nt':
                    if needs_window:
                        # pyappify 启动器模式 — 用 shell=True 以便系统根据 exe 的 manifest 触发 UAC 提权
                        # Electron 程序需要管理员权限才能操作游戏窗口
                        popen_kwargs["stdout"] = subprocess.DEVNULL
                        popen_kwargs["stderr"] = subprocess.DEVNULL
                        popen_kwargs["shell"] = True
                    else:
                        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                        popen_kwargs["stdout"] = subprocess.PIPE
                        popen_kwargs["stderr"] = subprocess.PIPE
                else:
                    popen_kwargs["stdout"] = subprocess.PIPE
                    popen_kwargs["stderr"] = subprocess.PIPE
                if cwd:
                    popen_kwargs["cwd"] = cwd
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except Exception as e:
                result.error_message = f"启动失败: {e}"
                logger.error(result.error_message)
                if attempt < max_restarts:
                    time.sleep(2)
                continue

            # 启动监护
            if self._guardian:
                self._guardian.start(proc)

            # 等待启动器完成（或超时）
            launcher_exit_normally = False
            launcher_pid = None
            if daemon_launcher and game_exe:
                # 守护进程模式（如 ok-ww Electron 封装）：
                # 启动器永不退出，直接进入游戏进程监控阶段
                launcher_exit_normally = True
                launcher_pid = proc.pid
                logger.info(
                    f"🔌 [{tool_name}][{task_id}] "
                    f"守护进程模式，跳过启动器等待，直接监控游戏进程 {game_exe}"
                )
            else:
                try:
                    proc.wait(timeout=timeout)
                    launcher_exit_normally = True
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    result.error_message = f"启动器超时 ({timeout}s)"
                    result.status = TaskStatus.TIMEOUT

            # 停止监护
            if self._guardian:
                self._guardian.stop()

            result.end_time = time.time()
            duration = result.end_time - (result.start_time or result.end_time)

            # ---- 启动器模式：启动器退出后等待目标进程 ----
            if needs_window and launcher_exit_normally:
                # 确定需要等待的目标进程
                #   game_exe 有值 → 游戏进程（星铁模式：March7th Launcher → StarRail.exe）
                #   game_exe 为空 → 从 cmd[0] 提取启动器 exe 名（鸣潮模式：shell=True 下 cmd.exe 包裹 ok-ww.exe）
                target_exe = game_exe or os.path.basename(cmd[0])
                launcher_code = proc.returncode
                logger.info(
                    f"🔌 [{tool_name}][{task_id}] 启动器已退出 "
                    f"(返回码 {launcher_code})，等待目标进程 {target_exe}..."
                )

                target_ran = self._wait_for_game_process(
                    target_exe,
                    timeout=timeout,
                    poll_interval=3,
                    exclude_pids=(os.getpid(),),
                )

                if target_ran:
                    # 目标进程启动过并已退出 → 任务完成
                    # 守护进程模式：先杀启动器进程树再清理游戏
                    if launcher_pid:
                        self._kill_process_tree(launcher_pid)
                    result.status = TaskStatus.SUCCESS
                    result.exit_code = 0
                    logger.info(
                        f"✅ [{tool_name}][{task_id}] {task_name} "
                        f"目标进程已退出，任务完成 ({duration:.0f}s)"
                    )
                    self._results.append(result)
                    self._kill_game(game_exe)
                    return result

                # target_ran=False 但有可能是时序问题（cmd.exe 退出瞬间
                # Electron 进程尚未出现在进程快照中）。额外轮询 60 秒。
                # launcher_code 为 None 表示守护进程模式（启动器仍在运行）
                if launcher_code in (0, None):
                    logger.info(
                        f"🔍 [{tool_name}][{task_id}] 未捕捉到 {target_exe} 启动，"
                        f"额外轮询 60 秒确认进程存在..."
                    )
                    found_in_poll = False
                    poll_deadline = time.time() + 60
                    while time.time() < poll_deadline:
                        pid = self._find_process_pid(target_exe, exclude_pids=(os.getpid(),))
                        if pid is not None:
                            found_in_poll = True
                            logger.info(
                                f"🎮 [{tool_name}][{task_id}] "
                                f"发现 {target_exe} (PID={pid})，等待退出..."
                            )
                            # 等到它退出 — 复用等待游戏进程退出逻辑
                            game_deadline = time.time() + timeout
                            while time.time() < game_deadline:
                                if self._find_process_pid(target_exe, exclude_pids=(os.getpid(),)) is None:
                                    logger.info(
                                        f"🏁 [{tool_name}][{task_id}] "
                                        f"{target_exe} 已退出，任务完成"
                                    )
                                    break
                                time.sleep(3)
                            else:
                                # 超时 → 强制杀掉
                                logger.warning(
                                    f"⏰ [{tool_name}][{task_id}] "
                                    f"{target_exe} 超时，强制结束"
                                )
                                self._kill_game(target_exe)
                                time.sleep(2)
                            break
                        time.sleep(2)

                    if found_in_poll:
                        if launcher_pid:
                            self._kill_process_tree(launcher_pid)
                        result.status = TaskStatus.SUCCESS
                        result.exit_code = 0
                        logger.info(
                            f"✅ [{tool_name}][{task_id}] {task_name} "
                            f"任务完成 ({duration:.0f}s)"
                        )
                        self._results.append(result)
                        self._kill_game(game_exe)
                        return result
                    else:
                        # 60s 内仍未检测到目标进程，可能是任务瞬间完成
                        if launcher_pid:
                            self._kill_process_tree(launcher_pid)
                        result.status = TaskStatus.SUCCESS
                        result.exit_code = 0
                        logger.info(
                            f"✅ [{tool_name}][{task_id}] {task_name} "
                            f"启动器正常退出(返回码0)，60s内未检测到 "
                            f"{target_exe}，任务视为完成 ({duration:.0f}s)"
                        )
                        self._results.append(result)
                        self._kill_game(game_exe)
                        return result

                else:
                    # 启动器异常退出 + 目标进程未检测到 → 启动器可能失败了
                    result.status = TaskStatus.FAILED
                    result.exit_code = launcher_code
                    result.error_message = (
                        f"启动器退出(返回码{launcher_code})，"
                        f"但目标进程 {target_exe} 未启动"
                    )
                    logger.warning(
                        f"❌ [{tool_name}][{task_id}] {task_name}: "
                        f"{result.error_message}"
                    )
                    if attempt < max_restarts:
                        logger.info(f"🔄 第 {attempt + 1} 次重启...")
                        time.sleep(2)
                    continue

            # ---- 非启动器模式：判断返回码 ----
            if not needs_window:
                if result.status != TaskStatus.TIMEOUT:
                    if proc.returncode == 0:
                        result.status = TaskStatus.SUCCESS
                        result.exit_code = 0
                    else:
                        result.status = TaskStatus.FAILED
                        result.exit_code = proc.returncode
                        result.error_message = self._read_output(proc)

            if result.status == TaskStatus.SUCCESS:
                logger.info(
                    f"✅ [{tool_name}][{task_id}] {task_name} "
                    f"完成 ({duration:.0f}s)"
                )
                self._results.append(result)
                self._kill_game(game_exe)
                return result

            # 失败（非启动器模式）
            logger.warning(
                f"❌ [{tool_name}][{task_id}] {task_name}: "
                f"{result.status.value} - {result.error_message[:100]}"
            )

            if attempt < max_restarts:
                logger.info(f"🔄 第 {attempt + 1} 次重启...")
                time.sleep(2)

        result.status = TaskStatus.FAILED
        self._results.append(result)
        self._kill_game(game_exe)
        return result

    def _read_output(self, proc) -> str:
        """读取 stdout/stderr"""
        parts = []
        try:
            out = proc.stdout.read() if proc.stdout else ""
            if out:
                parts.append(out[:500])
        except Exception:
            pass
        try:
            err = proc.stderr.read() if proc.stderr else ""
            if err:
                parts.append(err[:500])
        except Exception:
            pass
        return " | ".join(parts) if parts else "无错误输出"

    def _kill_game(self, game_exe: str):
        """任务结束后清理游戏进程，防止残留进程阻塞后续任务"""
        if not game_exe:
            return
        try:
            if os.name == 'nt':
                subprocess.run(
                    ["taskkill", "/f", "/im", game_exe],
                    capture_output=True,
                )
                logger.info(f"🧹 已清理游戏进程: {game_exe}")
            else:
                subprocess.run(
                    ["pkill", "-f", game_exe],
                    capture_output=True,
                )
        except Exception as exc:
            logger.warning(f"清理游戏进程失败: {exc}")

    @staticmethod
    def _kill_process_tree(pid: int):
        """杀掉进程及其所有子进程（Windows）"""
        if os.name != 'nt':
            return
        try:
            subprocess.run(
                ["taskkill", "/f", "/t", "/pid", str(pid)],
                capture_output=True,
            )
        except Exception:
            pass

    def _wait_for_game_process(
        self,
        game_exe: str,
        timeout: float = 3600,
        poll_interval: float = 3,
        exclude_pids: tuple = (),
    ) -> bool:
        """
        等待游戏进程启动并退出（用于 pyappify 启动器模式）

        逻辑：
        1. 轮询等待 game_exe 进程出现（最多等 120 秒）
        2. 出现后轮询等待其退出（最多等 timeout 秒）
        3. 退出后返回 True
        4. 如果在步骤1中 game_exe 从未出现，返回 False

        :param game_exe: 游戏进程名，如 "Wuthering Waves.exe"
        :param timeout: 游戏进程最大存活时间（秒）
        :param poll_interval: 轮询间隔（秒）
        :param exclude_pids: 排除的 PID 列表（如当前进程、启动器进程）
        :return: True=游戏进程启动过并已退出, False=从未启动
        """
        logger.info(f"⏳ 等待游戏进程 {game_exe} 启动...")
        start = time.time()
        wait_for_spawn = 120  # 最多等120秒让游戏进程出现
        spawned = False

        # 阶段1：等游戏进程出现
        while time.time() - start < wait_for_spawn:
            pid = self._find_process_pid(game_exe, exclude_pids=exclude_pids)
            if pid is not None:
                spawned = True
                logger.info(f"🎮 检测到游戏进程 {game_exe} (PID={pid})，等待退出...")
                break
            time.sleep(poll_interval)

        if not spawned:
            logger.warning(f"⚠ 游戏进程 {game_exe} 在 {wait_for_spawn}s 内未启动")
            return False

        # 阶段2：等游戏进程退出
        game_deadline = time.time() + timeout
        while time.time() < game_deadline:
            pid = self._find_process_pid(game_exe, exclude_pids=exclude_pids)
            if pid is None:
                logger.info(f"🏁 游戏进程 {game_exe} 已退出")
                return True
            time.sleep(poll_interval)

        # 超时 — 杀掉游戏进程
        logger.warning(f"⏰ 游戏进程 {game_exe} 超时，强制结束")
        self._kill_game(game_exe)
        time.sleep(2)
        return True

    @staticmethod
    def _find_process_pid(exe_name: str, exclude_pids: tuple = ()) -> Optional[int]:
        """通过进程名查找 PID（Windows），返回第一个匹配且不在排除列表中的 PID"""
        if os.name != 'nt':
            return None
        try:
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/fi", f"imagename eq {exe_name}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.replace('"', '').strip().split(',')
                if len(parts) >= 2 and parts[0].lower() == exe_name.lower():
                    try:
                        pid = int(parts[1].strip())
                        if pid not in exclude_pids:
                            return pid
                    except ValueError:
                        continue
            return None
        except Exception:
            return None

    def get_results(self) -> list[TaskResult]:
        return self._results
