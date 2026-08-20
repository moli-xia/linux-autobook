"""Background jobs with a streamable log, used by long maintenance actions."""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from autobook_linux.panel.services import redact

MAX_LINES = 4000
LOG_ROOT = Path("/var/tmp/autobook-jobs")


class Job:
    def __init__(self, kind: str, title: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.status = "running"      # running | success | failed
        self.started = time.time()
        self.finished = 0.0
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            for line in text.splitlines():
                self.lines.append(redact(line))

    def snapshot(self, offset: int = 0) -> dict[str, object]:
        with self._lock:
            lines = list(self.lines)
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "exit_code": self.exit_code,
            "started": self.started,
            "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "log": "\n".join(lines[offset:]),
            "total_lines": len(lines),
        }

    def finish(self, status: str, exit_code: int | None = None) -> None:
        self.status = status
        self.exit_code = exit_code
        self.finished = time.time()


class JobManager:
    def __init__(self, keep: int = 30) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self, kind: str) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.kind == kind and job.status == "running":
                    return job
        return None

    def recent(self, limit: int = 10) -> list[dict[str, object]]:
        with self._lock:
            ids = list(self._order)[-limit:]
            jobs = [self._jobs[job_id] for job_id in reversed(ids) if job_id in self._jobs]
        return [
            {
                "id": job.id,
                "kind": job.kind,
                "title": job.title,
                "status": job.status,
                "started": job.started,
                "elapsed": round((job.finished or time.time()) - job.started, 1),
            }
            for job in jobs
        ]

    def _register(self, job: Job) -> None:
        with self._lock:
            if len(self._order) == self._order.maxlen:
                oldest = self._order[0]
                self._jobs.pop(oldest, None)
            self._jobs[job.id] = job
            self._order.append(job.id)

    def spawn(self, kind: str, title: str, worker: Callable[[Job], None], exclusive: bool = True) -> Job:
        if exclusive:
            existing = self.running(kind)
            if existing:
                raise RuntimeError(f"「{existing.title}」正在执行，请等待完成后再试")
        job = Job(kind, title)
        self._register(job)

        def runner() -> None:
            try:
                worker(job)
            except Exception as exc:  # surfaced in the job log, not the HTTP reply
                job.write(f"[错误] {type(exc).__name__}: {exc}")
                job.finish("failed", 1)
            else:
                if job.status == "running":
                    job.finish("success", 0)

        threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
        return job

    def spawn_command(
        self,
        kind: str,
        title: str,
        command: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 3600,
    ) -> Job:
        def worker(job: Job) -> None:
            job.write(f"$ {' '.join(command)}")
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            deadline = time.time() + timeout
            assert process.stdout is not None
            for line in process.stdout:
                job.write(line.rstrip("\n"))
                if time.time() > deadline:
                    process.kill()
                    job.write("[错误] 执行超时，已终止")
                    job.finish("failed", 124)
                    return
            code = process.wait()
            process.stdout.close()
            job.write(f"[退出码 {code}]")
            job.finish("success" if code == 0 else "failed", code)

        return self.spawn(kind, title, worker)

    def spawn_supervised(self, kind: str, title: str, script: str, timeout: int = 2400) -> Job:
        """Run a script in its own transient systemd unit.

        Actions such as updating or switching roles restart the panel itself.
        A plain child process would be killed together with the panel's cgroup,
        leaving a half-finished install, so those scripts are handed to systemd.
        """

        def worker(job: Job) -> None:
            log_path = LOG_ROOT / f"{job.id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            unit = f"autobook-job-{job.id}"
            wrapped = f"({script}) > {log_path} 2>&1"
            launch = subprocess.run(
                [
                    "systemd-run", "--quiet", "--unit", unit,
                    "--property=Type=oneshot",
                    "--property=RemainAfterExit=yes",
                    f"--property=TimeoutStartSec={timeout}",
                    "bash", "-lc", wrapped,
                ],
                capture_output=True, text=True, errors="replace", timeout=60,
            )
            if launch.returncode != 0:
                job.write("[提示] systemd-run 不可用，改为前台执行")
                job.write(launch.stderr.strip())
                inline = subprocess.run(["bash", "-lc", script], capture_output=True, text=True,
                                        errors="replace", timeout=timeout)
                job.write(inline.stdout + inline.stderr)
                job.finish("success" if inline.returncode == 0 else "failed", inline.returncode)
                return
            job.write(f"[已交由 systemd 执行: {unit}]")
            deadline = time.time() + timeout
            seen = 0
            state = "activating"
            while time.time() < deadline:
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                if len(text) > seen:
                    job.write(text[seen:])
                    seen = len(text)
                probe = subprocess.run(
                    ["systemctl", "show", unit, "--property=ActiveState", "--property=ExecMainStatus",
                     "--property=Result"],
                    capture_output=True, text=True, errors="replace", timeout=20,
                )
                state = _property(probe.stdout, "ActiveState")
                if state in {"active", "failed", "inactive"}:
                    exit_code = int(_property(probe.stdout, "ExecMainStatus") or 0)
                    result = _property(probe.stdout, "Result")
                    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True, timeout=20)
                    subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=30)
                    job.write(f"[退出码 {exit_code}]")
                    ok = state == "active" and result in {"success", ""} and exit_code == 0
                    job.finish("success" if ok else "failed", exit_code)
                    return
                time.sleep(2)
            job.write("[错误] 执行超时")
            job.finish("failed", 124)

        return self.spawn(kind, title, worker)


def _property(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""
