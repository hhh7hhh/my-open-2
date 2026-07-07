"""
监护引擎 - 进程保活、窗口僵死检测、CPU假死检测、日志监控
"""
import time
import threading
import logging
import subprocess
import ctypes
import ctypes.wintypes
from typing import Optional
from datetime import datetime

logger = logging.getLogger("guardian")

# Windows API 常量
WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
HungWindowCheckTimeout = 5000  # 5秒无响应判定僵死


class Guardian:
    """
    监护引擎

    运行于后台线程，监控当前任务的进程状态：
    - 进程是否意外退出
    - 窗口是否僵死 (无响应)
    - CPU 是否假死 (持续低使用率)
    - 输出日志是否卡住
    """

    def __init__(
        self,
        cpu_dead_threshold: float = 0.0,
        cpu_dead_duration: int = 10,
        hung_check_interval: int = 5,
    ):
        self._cpu_dead_threshold = cpu_dead_threshold
        self._cpu_dead_duration = cpu_dead_duration
        self._hung_check_interval = hung_check_interval

        self._proc: Optional[subprocess.Popen] = None
        self._proc_pid: Optional[int] = None
        self._monitoring = False
        self._lock = threading.Lock()
        self._last_log_line = ""
        self._last_log_time: Optional[float] = None
        self._log_event = threading.Event()

        # CPU 采样
        self._cpu_samples: list[float] = []
        self._cpu_dead_start: Optional[float] = None

        # 僵死检测线程
        self._hung_thread: Optional[threading.Thread] = None

        # 回调
        self._on_process_died: Optional[callable] = None
        self._on_window_hung: Optional[callable] = None
        self._on_cpu_dead: Optional[callable] = None
        self._on_log_stuck: Optional[callable] = None

    # ---- 回调设置 ----

    def on_process_died(self, cb):
        self._on_process_died = cb

    def on_window_hung(self, cb):
        self._on_window_hung = cb

    def on_cpu_dead(self, cb):
        self._on_cpu_dead = cb

    def on_log_stuck(self, cb):
        self._on_log_stuck = cb

    # ---- 监控控制 ----

    def start(self, proc: subprocess.Popen, pid: Optional[int] = None):
        with self._lock:
            self._proc = proc
            self._proc_pid = pid or proc.pid
            self._monitoring = True
            self._cpu_samples = []
            self._cpu_dead_start = None
            self._last_log_time = None
            self._log_event.clear()

        logger.info(f"Guardian started monitoring PID {self._proc_pid}")

        # 启动僵死检测线程
        self._hung_thread = threading.Thread(target=self._hung_loop, daemon=True)
        self._hung_thread.start()

    def stop(self):
        with self._lock:
            self._monitoring = False
            self._proc = None
            self._proc_pid = None
        logger.info("Guardian stopped")

    def feed_log(self, line: str):
        """外部传入日志行，用于检测日志是否卡住"""
        with self._lock:
            self._last_log_line = line
            self._last_log_time = time.time()
        self._log_event.set()

    # ---- 检测循环 ----

    def _hung_loop(self):
        """窗口僵死 + CPU假死 检测循环"""
        while self._monitoring:
            with self._lock:
                if not self._monitoring or self._proc_pid is None:
                    break
                pid = self._proc_pid

            # 1. 检查进程是否存活
            if not self._is_process_alive(pid):
                self._emit_process_died()
                break

            # 2. 检查窗口僵死
            if self._is_window_hung(pid):
                self._emit_window_hung()

            # 3. 检查 CPU 假死
            cpu = self._get_cpu_usage(pid)
            if cpu is not None:
                with self._lock:
                    self._cpu_samples.append((time.time(), cpu))
                    # 只保留最近60秒的样本
                    self._cpu_samples = self._cpu_samples[-60:]

                if self._check_cpu_dead():
                    self._emit_cpu_dead()

            time.sleep(self._hung_check_interval)

    def _is_process_alive(self, pid: int) -> bool:
        """检查进程是否存活"""
        try:
            process_handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not process_handle:
                return False
            exit_code = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(process_handle)
            return exit_code.value == 259  # STILL_ACTIVE
        except Exception:
            return False

    def _is_window_hung(self, pid: int) -> bool:
        """检查进程的主窗口是否僵死"""
        hung = False

        def _enum_windows_callback(hwnd, pid_to_check):
            nonlocal hung
            _, found_pid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd)
            if found_pid == pid_to_check:
                result = ctypes.wintypes.DWORD()
                ctypes.windll.user32.SendMessageTimeoutW(
                    hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG,
                    HungWindowCheckTimeout, ctypes.byref(result),
                )
                if result.value == 0:
                    hung = True
                    return False
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum_windows_callback), pid)
        return hung

    def _get_cpu_usage(self, pid: int) -> Optional[float]:
        """获取进程 CPU 使用率（近似值，基于时间差）"""
        try:
            return (
                ctypes.windll.kernel32.GetProcessTimes
                and self._calc_cpu_times(pid)
            )
        except Exception:
            return None

    def _calc_cpu_times(self, pid: int) -> Optional[float]:
        """计算 CPU 时间百分比"""
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if not h:
                return None
            creation = ctypes.wintypes.FILETIME()
            exit_t = ctypes.wintypes.FILETIME()
            kernel = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            ret = ctypes.windll.kernel32.GetProcessTimes(
                h, ctypes.byref(creation), ctypes.byref(exit_t),
                ctypes.byref(kernel), ctypes.byref(user),
            )
            ctypes.windll.kernel32.CloseHandle(h)
            if not ret:
                return None

            k64 = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
            u64 = (user.dwHighDateTime << 32) | user.dwLowDateTime
            total_time = k64 + u64

            # 存储上一轮数据用于计算差值
            with self._lock:
                if not hasattr(self, '_last_cpu_time'):
                    self._last_cpu_time = total_time
                    self._last_cpu_tick = time.time()
                    return 0.0

                time_delta = time.time() - self._last_cpu_tick
                cpu_delta = total_time - self._last_cpu_time
                self._last_cpu_time = total_time
                self._last_cpu_tick = time.time()

                if time_delta <= 0:
                    return 0.0
                cpu_percent = (cpu_delta / (time_delta * 10_000_000)) * 100
                return max(0.0, min(100.0, cpu_percent))
        except Exception:
            return None

    def _check_cpu_dead(self) -> bool:
        """检查是否 CPU 持续低使用率（假死）"""
        with self._lock:
            if not self._cpu_samples:
                return False

            # 检查最近 cpu_dead_duration 秒的样本
            cutoff = time.time() - self._cpu_dead_duration
            recent = [cpu for t, cpu in self._cpu_samples if t >= cutoff]

            if len(recent) < 2:
                return False

            if all(cpu <= self._cpu_dead_threshold for cpu in recent):
                if self._cpu_dead_start is None:
                    self._cpu_dead_start = time.time()
                elif time.time() - self._cpu_dead_start >= self._cpu_dead_duration:
                    return True
            else:
                self._cpu_dead_start = None

            return False

    # ---- 事件触发 ----

    def _emit_process_died(self):
        logger.warning(f"Process {self._proc_pid} died")
        if self._on_process_died:
            self._on_process_died()

    def _emit_window_hung(self):
        logger.warning(f"Window hung detected for PID {self._proc_pid}")
        if self._on_window_hung:
            self._on_window_hung()

    def _emit_cpu_dead(self):
        logger.warning(f"CPU dead detected for PID {self._proc_pid}")
        if self._on_cpu_dead:
            self._on_cpu_dead()

    def _emit_log_stuck(self):
        logger.warning("Log stuck detected")
        if self._on_log_stuck:
            self._on_log_stuck()

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    @property
    def monitored_pid(self) -> Optional[int]:
        return self._proc_pid