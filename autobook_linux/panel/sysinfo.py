"""Host metrics shown on the dashboard."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key.strip()] = int(parts[0]) * 1024
    except OSError:
        pass
    return values


def _uptime() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _load() -> list[float]:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        return [0.0, 0.0, 0.0]


def _binary(name: str) -> dict[str, object]:
    path = shutil.which(name)
    version = ""
    if path:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8, errors="replace")
            version = (result.stdout or result.stderr).strip().splitlines()[0][:120] if (result.stdout or result.stderr).strip() else ""
        except Exception:
            version = ""
    return {"name": name, "path": path or "", "found": bool(path), "version": version}


def snapshot(install_dir: Path, work_paths: list[Path] | None = None) -> dict[str, object]:
    memory = _meminfo()
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    try:
        disk = shutil.disk_usage(str(install_dir if Path(install_dir).exists() else "/"))
        disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_info = {"total": 0, "used": 0, "free": 0}
    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "os": _pretty_os(),
        "cpu_count": os.cpu_count() or 1,
        "load": _load(),
        "memory": {"total": total, "available": available, "used": max(0, total - available)},
        "disk": disk_info,
        "uptime_seconds": _uptime(),
        "now": int(time.time()),
    }


def _pretty_os() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def binaries(names: list[str]) -> list[dict[str, object]]:
    return [_binary(name) for name in names]


def directory_usage(path: Path) -> dict[str, object]:
    """Shallow size and file count for a runtime directory."""
    target = Path(path)
    if not target.is_dir():
        return {"path": str(target), "exists": False, "bytes": 0, "entries": 0}
    total = 0
    entries = 0
    for root, _dirs, files in os.walk(target):
        for name in files:
            entries += 1
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
        if entries > 20000:
            break
    return {"path": str(target), "exists": True, "bytes": total, "entries": entries}
