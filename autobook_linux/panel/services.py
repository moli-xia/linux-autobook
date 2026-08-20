"""Service status, control and logs, over systemd or the internal supervisor.

Both backends return the same status shape so the API and front-end never need
to know whether the deployment is a systemd host or the Docker image.
"""
from __future__ import annotations

import re
import subprocess
import time

from autobook_linux.panel.settings import supervisor_backend

SERVICES: dict[str, dict[str, str]] = {
    "gateway": {
        "unit": "autobook-gateway.service",
        "label": "百度下载网关",
        "role": "gateway",
        "summary": "持有百度账号，负责转存与高速下载，供所有 Worker 调用。",
    },
    "worker": {
        "unit": "autobook-worker.service",
        "label": "任务 Worker",
        "role": "worker",
        "summary": "领取网站任务，下载、转换 PDF 并上传到结果网盘。",
    },
    "admin": {
        "unit": "autobook-admin.service",
        "label": "管理面板",
        "role": "all",
        "summary": "当前正在使用的这个 Web 面板本身。",
    },
}

ACTIONS = {"start", "stop", "restart", "enable", "disable"}
# journald lines may contain signed Baidu download URLs; never show them.
SENSITIVE_URL_RE = re.compile(r"https?://\S*(?:dlink|download|pcs\.baidu|bduss|sign=)\S*", re.IGNORECASE)
SENSITIVE_KV_RE = re.compile(r"(?i)\b(bduss|stoken|token|password|passwd|secret)\s*[=:]\s*\S+")

_SUPERVISOR = None


def bind_supervisor(supervisor) -> None:
    """Register the in-process supervisor used by the container backend."""
    global _SUPERVISOR
    _SUPERVISOR = supervisor


def redact(text: str) -> str:
    text = SENSITIVE_URL_RE.sub("[链接已隐藏]", text)
    return SENSITIVE_KV_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # systemd is absent on developer machines and inside minimal containers.
        return subprocess.CompletedProcess(args, 127, "", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------- systemd


def _show(unit: str, properties: list[str]) -> dict[str, str]:
    result = _run(["systemctl", "show", unit, *[f"--property={name}" for name in properties]])
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _systemd_status(name: str) -> dict[str, object]:
    meta = SERVICES[name]
    unit = meta["unit"]
    props = _show(
        unit,
        ["LoadState", "ActiveState", "SubState", "UnitFileState", "ExecMainStartTimestamp",
         "MemoryCurrent", "MainPID", "NRestarts", "Result"],
    )
    load_state = props.get("LoadState", "not-found")
    installed = load_state not in {"not-found", "masked", ""}
    active_state = props.get("ActiveState", "unknown") if installed else "not-installed"
    memory_raw = props.get("MemoryCurrent", "")
    memory = int(memory_raw) if memory_raw.isdigit() else 0
    uptime = 0
    started = props.get("ExecMainStartTimestamp", "")
    if started and active_state == "active":
        parsed = _run(["date", "-d", started, "+%s"], timeout=5)
        if parsed.returncode == 0 and parsed.stdout.strip().lstrip("-").isdigit():
            uptime = max(0, int(time.time()) - int(parsed.stdout.strip()))
    return {
        "installed": installed,
        "active": active_state,
        "sub_state": props.get("SubState", "") if installed else "",
        "enabled": props.get("UnitFileState", "unknown") if installed else "not-installed",
        "memory_bytes": memory,
        "pid": int(props.get("MainPID", "0") or 0),
        "restarts": int(props.get("NRestarts", "0") or 0),
        "result": props.get("Result", ""),
        "uptime_seconds": uptime,
        "running": active_state == "active",
        "message": "",
    }


def _systemd_control(name: str, action: str) -> str:
    unit = SERVICES[name]["unit"]
    if action in {"start", "restart"}:
        _run(["systemctl", "reset-failed", unit], timeout=15)
    args = ["systemctl", action, unit]
    if action == "enable":
        args = ["systemctl", "enable", "--now", unit]
    elif action == "disable":
        args = ["systemctl", "disable", "--now", unit]
    result = _run(args, timeout=120)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "systemctl 执行失败").strip()[-1200:])
    return (result.stdout or "").strip()


def _systemd_logs(name: str, lines: int, priority: str) -> str:
    args = ["journalctl", "-u", SERVICES[name]["unit"], "-n", str(lines), "--no-pager", "--output=short-iso"]
    if priority in {"err", "warning", "info"}:
        args += ["-p", priority]
    result = _run(args, timeout=30)
    return result.stdout or result.stderr or ""


# -------------------------------------------------------------- internal


def _internal_status(name: str, roles: list[str] | None = None) -> dict[str, object]:
    if name == "admin":
        return {
            "installed": True, "active": "active", "sub_state": "running", "enabled": "enabled",
            "memory_bytes": 0, "pid": 0, "restarts": 0, "result": "success",
            "uptime_seconds": 0, "running": True, "message": "面板进程",
        }
    if _SUPERVISOR is None:
        return {
            "installed": False, "active": "not-installed", "sub_state": "", "enabled": "not-installed",
            "memory_bytes": 0, "pid": 0, "restarts": 0, "result": "", "uptime_seconds": 0,
            "running": False, "message": "",
        }
    process = _SUPERVISOR.get(name)
    snapshot = process.snapshot()
    installed = roles is None or name in roles
    return {
        "installed": installed,
        "active": "active" if snapshot["running"] else ("inactive" if installed else "not-installed"),
        "sub_state": "running" if snapshot["running"] else "dead",
        "enabled": "enabled" if snapshot["enabled"] else "disabled",
        "memory_bytes": process.memory_bytes(),
        "pid": snapshot["pid"],
        "restarts": snapshot["restarts"],
        "result": "success" if snapshot["last_exit"] in (None, 0) else "exit-code",
        "uptime_seconds": snapshot["uptime_seconds"],
        "running": snapshot["running"],
        "message": snapshot["message"],
    }


def _internal_control(name: str, action: str) -> str:
    if name == "admin":
        raise RuntimeError("容器模式下请用 docker restart 重启面板容器")
    if _SUPERVISOR is None:
        raise RuntimeError("进程管理器尚未初始化")
    process = _SUPERVISOR.get(name)
    if action in {"start", "enable"}:
        process.start()
    elif action in {"stop", "disable"}:
        process.stop()
    elif action == "restart":
        process.restart()
    _SUPERVISOR.save_state()
    return process.snapshot()["message"] or ""


def _internal_logs(name: str, lines: int) -> str:
    if name == "admin":
        return "面板自身日志请用 docker logs 查看。"
    if _SUPERVISOR is None:
        return ""
    return _SUPERVISOR.get(name).read_log(lines)


# ------------------------------------------------------------ public API


def status(name: str, roles: list[str] | None = None) -> dict[str, object]:
    meta = SERVICES[name]
    if supervisor_backend() == "internal":
        state = _internal_status(name, roles)
    else:
        state = _systemd_status(name)
    return {"name": name, "unit": meta["unit"], "label": meta["label"], "summary": meta["summary"], **state}


def control(name: str, action: str) -> str:
    if name not in SERVICES:
        raise ValueError("未知服务")
    if action not in ACTIONS:
        raise ValueError("不允许的操作")
    if supervisor_backend() == "internal":
        return _internal_control(name, action)
    return _systemd_control(name, action)


def logs(name: str, lines: int = 200, priority: str = "", grep: str = "") -> str:
    if name not in SERVICES:
        raise ValueError("未知服务")
    count = min(1000, max(20, int(lines)))
    if supervisor_backend() == "internal":
        text = _internal_logs(name, count)
    else:
        text = _systemd_logs(name, count, priority)
    if grep:
        needle = grep.lower()
        text = "\n".join(line for line in text.splitlines() if needle in line.lower())
    return redact(text[-200_000:])


def units_for_roles(roles: list[str]) -> list[str]:
    names = [name for name, meta in SERVICES.items() if meta["role"] in roles]
    return names + ["admin"]
