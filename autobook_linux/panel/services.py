"""systemd interaction: status, control and log retrieval."""
from __future__ import annotations

import re
import subprocess
import time

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


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # systemd is absent on developer machines and inside minimal containers.
        return subprocess.CompletedProcess(args, 127, "", f"{type(exc).__name__}: {exc}")


def _show(unit: str, properties: list[str]) -> dict[str, str]:
    result = _run(["systemctl", "show", unit, *[f"--property={name}" for name in properties]])
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def status(name: str) -> dict[str, object]:
    """Structured state of one managed unit."""
    meta = SERVICES[name]
    unit = meta["unit"]
    props = _show(
        unit,
        [
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "ExecMainStartTimestamp",
            "MemoryCurrent",
            "MainPID",
            "NRestarts",
            "Result",
        ],
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
        "name": name,
        "unit": unit,
        "label": meta["label"],
        "summary": meta["summary"],
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
    }


def control(name: str, action: str) -> str:
    """Run a systemctl action, returning its combined output."""
    if name not in SERVICES:
        raise ValueError("未知服务")
    if action not in ACTIONS:
        raise ValueError("不允许的操作")
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
        detail = (result.stderr or result.stdout or "systemctl 执行失败").strip()
        raise RuntimeError(detail[-1200:])
    return (result.stdout or "").strip()


def redact(text: str) -> str:
    text = SENSITIVE_URL_RE.sub("[链接已隐藏]", text)
    return SENSITIVE_KV_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)


def logs(name: str, lines: int = 200, priority: str = "", grep: str = "") -> str:
    if name not in SERVICES:
        raise ValueError("未知服务")
    count = min(1000, max(20, int(lines)))
    args = ["journalctl", "-u", SERVICES[name]["unit"], "-n", str(count), "--no-pager", "--output=short-iso"]
    if priority in {"err", "warning", "info"}:
        args += ["-p", priority]
    result = _run(args, timeout=30)
    text = result.stdout or result.stderr or ""
    if grep:
        needle = grep.lower()
        text = "\n".join(line for line in text.splitlines() if needle in line.lower())
    return redact(text[-200_000:])


def units_for_roles(roles: list[str]) -> list[str]:
    names = [name for name, meta in SERVICES.items() if meta["role"] in roles]
    return names + ["admin"]
