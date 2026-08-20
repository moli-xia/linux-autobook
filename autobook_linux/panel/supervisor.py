"""In-process supervisor used when the panel runs inside the container.

systemd is not available in the image, so the panel itself owns the gateway and
worker processes: it starts them, restarts them on unexpected exits, captures
their output to rotating log files and reports the same status shape the
systemd backend produces, so the rest of the panel does not care which is used.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from autobook_linux.panel.envfile import read_env_file

LOG_LIMIT_BYTES = 8 * 1024 * 1024
RESTART_DELAY_SECONDS = 20
CONFIG_NOT_READY_EXIT = 78     # the runners use this when configuration is incomplete
STOP_GRACE_SECONDS = 60


class ManagedProcess:
    """One supervised child process with its own log file."""

    def __init__(self, name: str, command: list[str], env_file: Path, cwd: Path, log_path: Path) -> None:
        self.name = name
        self.command = command
        self.env_file = env_file
        self.cwd = cwd
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at = 0.0
        self.restarts = 0
        self.last_exit: int | None = None
        self.stopping = False
        self.want_running = False
        self.message = ""
        self._lock = threading.RLock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def running(self) -> bool:
        with self._lock:
            return bool(self.process and self.process.poll() is None)

    def _rotate(self) -> None:
        try:
            if self.log_path.is_file() and self.log_path.stat().st_size > LOG_LIMIT_BYTES:
                self.log_path.replace(self.log_path.with_suffix(self.log_path.suffix + ".1"))
        except OSError:
            pass

    def _spawn(self) -> None:
        self._rotate()
        environment = dict(os.environ)
        environment.update({key: value for key, value in read_env_file(self.env_file).items()})
        environment["PYTHONUNBUFFERED"] = "1"
        log_file = self.log_path.open("ab")
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            log_file.write(f"\n{stamp} [supervisor] 启动 {self.name}\n".encode("utf-8"))
            log_file.flush()
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_file.close()
        self.started_at = time.time()
        self.stopping = False
        self.message = "运行中"

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            self.want_running = True
            self._spawn()

    def stop(self, grace: int = STOP_GRACE_SECONDS) -> None:
        with self._lock:
            self.want_running = False
            self.stopping = True
            process = self.process
        if not process or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()
        deadline = time.time() + grace
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.5)
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
        with self._lock:
            self.message = "已停止"

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    # ------------------------------------------------------------------
    def reap(self) -> None:
        """Called by the supervisor loop; restarts unexpected exits."""
        with self._lock:
            process = self.process
            if not process:
                return
            code = process.poll()
            if code is None:
                return
            self.last_exit = code
            self.process = None
            if not self.want_running:
                self.message = "已停止"
                return
            if code == CONFIG_NOT_READY_EXIT:
                self.want_running = False
                self.message = "配置尚未完成，已停止自动重启"
                self._append(f"[supervisor] {self.name} 因配置不完整退出（{code}），不再自动重启")
                return
            self.restarts += 1
            self.message = f"异常退出（{code}），{RESTART_DELAY_SECONDS} 秒后重启"
            self._append(f"[supervisor] {self.name} 退出码 {code}，{RESTART_DELAY_SECONDS} 秒后重启")
            threading.Timer(RESTART_DELAY_SECONDS, self._delayed_restart).start()

    def _delayed_restart(self) -> None:
        with self._lock:
            if self.want_running and not self.running():
                self._spawn()

    def _append(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as output:
                output.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    def memory_bytes(self) -> int:
        with self._lock:
            process = self.process
        if not process or process.poll() is not None:
            return 0
        try:
            for line in Path(f"/proc/{process.pid}/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
        except (OSError, ValueError, IndexError):
            return 0
        return 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self.running()
            return {
                "running": running,
                "pid": self.process.pid if running and self.process else 0,
                "uptime_seconds": int(time.time() - self.started_at) if running else 0,
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "enabled": self.want_running,
                "message": self.message,
            }

    def read_log(self, lines: int) -> str:
        chunks: list[str] = []
        for path in (self.log_path.with_suffix(self.log_path.suffix + ".1"), self.log_path):
            if path.is_file():
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
        text = "".join(chunks)
        return "\n".join(text.splitlines()[-lines:])


class Supervisor:
    """Owns every managed process and the loop that reaps them."""

    def __init__(self, install_dir: Path, config_dir: Path, runtime_dir: Path) -> None:
        self.install_dir = Path(install_dir)
        self.config_dir = Path(config_dir)
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.config_dir / "supervisor-state.json"
        python = str(self.install_dir / ".venv" / "bin" / "python")
        if not Path(python).exists():
            python = "python3"
        self.processes: dict[str, ManagedProcess] = {
            "gateway": ManagedProcess(
                "gateway",
                [python, str(self.install_dir / "run_gateway.py")],
                self.config_dir / "gateway.env",
                self.install_dir,
                self.runtime_dir / "logs" / "gateway.log",
            ),
            "worker": ManagedProcess(
                "worker",
                [python, str(self.install_dir / "run_worker.py")],
                self.config_dir / "worker.env",
                self.install_dir,
                self.runtime_dir / "logs" / "worker.log",
            ),
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def get(self, name: str) -> ManagedProcess:
        if name not in self.processes:
            raise ValueError("未知服务")
        return self.processes[name]

    def load_state(self) -> dict[str, bool]:
        try:
            return dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def save_state(self) -> None:
        payload = {name: process.want_running for name, process in self.processes.items()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def autostart(self, roles: list[str]) -> None:
        """Bring back whatever was running before the container was recreated."""
        state = self.load_state()
        for name, process in self.processes.items():
            if name not in roles:
                continue
            if state.get(name):
                try:
                    process.start()
                except Exception:  # a bad command must not stop the panel
                    process.message = "启动失败"

    def run(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(2.0):
            for process in self.processes.values():
                try:
                    process.reap()
                except Exception:
                    continue

    def shutdown(self) -> None:
        self._stop.set()
        for process in self.processes.values():
            if process.running():
                process.stop(grace=20)
