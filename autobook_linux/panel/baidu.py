"""Baidu QR login driven from the panel."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from autobook_linux.panel.envfile import read_env_file
from autobook_linux.panel.settings import PanelSettings

SERVICE_USER = "autobook"

PHASES = (
    ("扫码登录成功", "success", "登录成功，凭据已保存"),
    ("已确认", "confirming", "手机已确认，正在建立网盘会话"),
    ("已扫码", "scanned", "已扫码，请在手机上点击确认"),
    ("等待扫码", "waiting", "二维码已就绪，请用百度网盘 App 扫码"),
    ("二维码已保存", "waiting", "二维码已就绪，请用百度网盘 App 扫码"),
)


class QrLoginManager:
    """Runs ``run_worker.py --baidu-login`` and exposes its live progress."""

    def __init__(self, settings: PanelSettings) -> None:
        self.settings = settings
        self.runtime = settings.install_dir / "runtime"
        self.qr_path = self.runtime / "panel-baidu-qr.png"
        self.log_path = self.runtime / "panel-baidu-login.log"
        self.process: subprocess.Popen[str] | None = None
        self.phase = "idle"
        self.message = "尚未开始扫码登录"
        self.started_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有扫码任务在进行中，请先完成或等待超时")
            values = read_env_file(self.settings.gateway_env)
            environment = dict(os.environ)
            environment.update({key: value for key, value in values.items() if value != ""})
            environment["BAIDU_QR_PATH"] = str(self.qr_path)
            environment["PYTHONUNBUFFERED"] = "1"
            self.runtime.mkdir(parents=True, exist_ok=True)
            self.qr_path.unlink(missing_ok=True)
            log_file = self.log_path.open("w", encoding="utf-8")
            command = [
                str(self.settings.venv_python()),
                str(self.settings.install_dir / "run_worker.py"),
                "--baidu-login",
                "--qr-output",
                str(self.qr_path),
            ]
            runuser = shutil.which("runuser")
            if os.name != "nt" and os.geteuid() == 0 and runuser:
                # The credential file must belong to the service account,
                # otherwise the gateway cannot read its own 0600 secret.
                command = [runuser, "--user", SERVICE_USER, "--preserve-environment", "--", *command]
            self.process = subprocess.Popen(
                command,
                cwd=str(self.settings.install_dir),
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.phase = "starting"
            self.message = "正在生成二维码…"
            self.started_at = time.time()
            threading.Thread(target=self._watch, args=(self.process, log_file), daemon=True).start()

    def cancel(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.phase = "idle"
                self.message = "已取消扫码"

    # ------------------------------------------------------------------
    def _watch(self, process: subprocess.Popen[str], log_file) -> None:
        code = process.wait()
        try:
            log_file.close()
        except Exception:
            pass
        with self._lock:
            if code == 0:
                self.phase = "success"
                self.message = "扫码登录成功，正在重启网关服务"
            else:
                self.phase = "failed"
                self.message = f"扫码登录失败（退出码 {code}），请查看下方日志"
        if code == 0:
            subprocess.run(["systemctl", "restart", "autobook-gateway.service"], capture_output=True, timeout=90)
            with self._lock:
                self.message = "扫码登录成功，网关服务已重启"

    def _tail(self, lines: int = 20) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])

    def status(self) -> dict[str, object]:
        with self._lock:
            running = bool(self.process and self.process.poll() is None)
            phase, message = self.phase, self.message
        log = self._tail()
        if running:
            for needle, new_phase, new_message in PHASES:
                if needle in log:
                    phase, message = new_phase, new_message
                    break
            with self._lock:
                self.phase, self.message = phase, message
        credentials = Path(read_env_file(self.settings.gateway_env).get("BAIDU_AUTH_FILE", ""))
        return {
            "running": running,
            "phase": phase,
            "message": message,
            "has_qr": self.qr_path.is_file(),
            "qr_mtime": int(self.qr_path.stat().st_mtime) if self.qr_path.is_file() else 0,
            "elapsed": int(time.time() - self.started_at) if self.started_at else 0,
            "log": log,
            "logged_in": credentials.is_file(),
            "credentials_age": int(time.time() - credentials.stat().st_mtime) if credentials.is_file() else 0,
        }

    def read_qr(self) -> bytes:
        return self.qr_path.read_bytes()
