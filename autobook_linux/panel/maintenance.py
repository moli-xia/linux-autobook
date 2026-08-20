"""Maintenance actions and worker activity reporting."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from autobook_linux.panel.envfile import read_env_file, write_env_file
from autobook_linux.panel.jobs import Job, JobManager
from autobook_linux.panel.schema import key_order
from autobook_linux.panel.services import redact
from autobook_linux.panel.settings import PanelSettings

SERVICE_USER = "autobook"
BACKUP_DIR = Path("/var/backups/linux-autobook")

CLAIM_RE = re.compile(r"领取任务 #(\d+): (.*)$")
PROGRESS_RE = re.compile(r"\[#(\d+)\] (.*)$")
DONE_RE = re.compile(r"任务 #(\d+) 完成: (\S+)")
FAIL_RE = re.compile(r"任务 #(\d+) 失败: (.*)$")
STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:+-]+)")


def generate_token() -> str:
    return secrets.token_hex(32)


# --------------------------------------------------------------------- jobs


def fix_dependencies(manager: JobManager) -> Job:
    script = (
        "set -e; export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y p7zip-full aria2 openssl ca-certificates curl; "
        "echo '依赖检查完成'; command -v 7z; command -v aria2c"
    )
    return manager.spawn_command("deps", "修复系统依赖", ["bash", "-lc", script], timeout=900)


def fix_permissions(settings: PanelSettings, manager: JobManager) -> Job:
    install = settings.install_dir
    script = (
        f"set -e; "
        f"install -d -m 750 -o {SERVICE_USER} -g {SERVICE_USER} '{install}/runtime'; "
        f"chown -R {SERVICE_USER}:{SERVICE_USER} '{install}/runtime'; "
        f"if [ -f '{install}/password.txt' ]; then chown {SERVICE_USER}:{SERVICE_USER} '{install}/password.txt'; "
        f"chmod 600 '{install}/password.txt'; fi; "
        f"chmod 600 {settings.config_dir}/*.env 2>/dev/null || true; "
        f"echo '目录属主与权限已修复'; ls -ld '{install}/runtime'"
    )
    return manager.spawn_command("permissions", "修复目录权限", ["bash", "-lc", script], timeout=300)


def regenerate_gateway_cert(settings: PanelSettings, manager: JobManager, host: str) -> Job:
    values = read_env_file(settings.gateway_env)
    cert = values.get("GATEWAY_TLS_CERT") or str(settings.install_dir / "runtime" / "gateway.crt")
    key = values.get("GATEWAY_TLS_KEY") or str(settings.install_dir / "runtime" / "gateway.key")
    safe_host = host if re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", host or "") else "localhost"
    san = f"IP:{safe_host}" if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", safe_host) else f"DNS:{safe_host}"
    script = (
        f"set -e; rm -f '{cert}' '{key}'; "
        f"openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 "
        f"-keyout '{key}' -out '{cert}' -subj '/CN={safe_host}' "
        f"-addext 'subjectAltName={san},DNS:localhost,IP:127.0.0.1'; "
        f"chown {SERVICE_USER}:{SERVICE_USER} '{cert}' '{key}'; chmod 644 '{cert}'; chmod 640 '{key}'; "
        f"openssl x509 -in '{cert}' -noout -subject -enddate -ext subjectAltName; "
        f"echo '证书已重新生成，请把 {cert} 复制到所有 Worker 并重启网关'"
    )
    return manager.spawn_command("cert", "重新生成网关证书", ["bash", "-lc", script], timeout=180)


def backup_config(settings: PanelSettings, manager: JobManager) -> Job:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"config-{stamp}.tar.gz"
    script = (
        f"set -e; install -d -m 700 '{BACKUP_DIR}'; "
        f"tar -C / -czf '{target}' '{str(settings.config_dir).lstrip('/')}'; "
        f"chmod 600 '{target}'; ls -lh '{target}'; "
        f"echo '备份完成: {target}'"
    )
    return manager.spawn_command("backup", "备份配置", ["bash", "-lc", script], timeout=300)


def update_application(settings: PanelSettings, manager: JobManager) -> Job:
    installer = settings.install_dir / "install.sh"
    state = read_env_file(settings.install_env)
    repo = state.get("REPO_URL") or "https://github.com/moli-xia/linux-autobook.git"
    branch = state.get("REPO_BRANCH") or "main"
    script = (
        f"set -e; export AUTOBOOK_REPO_URL='{repo}'; export AUTOBOOK_BRANCH='{branch}'; "
        f"bash '{installer}' --action update --non-interactive"
    )
    # The installer restarts the panel, so the work must outlive this process.
    return manager.spawn_supervised("update", "更新程序", script, timeout=2400)


def switch_role(settings: PanelSettings, manager: JobManager, role: str) -> Job:
    if role not in {"all", "gateway", "worker"}:
        raise ValueError("无效的角色")
    installer = settings.install_dir / "install.sh"
    state = read_env_file(settings.install_env)
    host = state.get("PUBLIC_HOST", "")
    port = state.get("ADMIN_PORT", "8766")
    script = (
        f"bash '{installer}' --action install --role '{role}' "
        f"--install-dir '{settings.install_dir}' --admin-port '{port}' "
        f"--public-host '{host}' --non-interactive"
    )
    return manager.spawn_supervised("role", f"切换角色为 {role}", script, timeout=2400)


def restart_panel() -> None:
    """Restart the panel itself shortly after the HTTP reply is flushed."""
    subprocess.Popen(
        ["bash", "-lc", "sleep 1; systemctl restart autobook-admin.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def rotate_gateway_token(settings: PanelSettings) -> str:
    """Write a fresh shared token into every env file this node owns."""
    token = generate_token()
    for target in settings.roles():
        path = settings.env_path(target)
        values = read_env_file(path)
        values["BAIDU_GATEWAY_TOKEN"] = token
        write_env_file(path, values, key_order(target))
    return token


# ------------------------------------------------------------ password dict


def read_password_dict(settings: PanelSettings) -> dict[str, object]:
    values = read_env_file(settings.worker_env)
    path = Path(values.get("PASSWORD_DICT") or (settings.install_dir / "password.txt"))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    entries = [line for line in text.splitlines() if line.strip()]
    return {"path": str(path), "count": len(entries), "content": "\n".join(entries)}


def write_password_dict(settings: PanelSettings, content: str) -> int:
    values = read_env_file(settings.worker_env)
    path = Path(values.get("PASSWORD_DICT") or (settings.install_dir / "password.txt"))
    resolved = path.resolve()
    allowed_root = settings.install_dir.resolve()
    if not str(resolved).startswith(str(allowed_root)):
        raise ValueError("密码字典必须位于程序安装目录内")
    entries = [line.strip() for line in content.splitlines() if line.strip()]
    if len(entries) > 20000:
        raise ValueError("密码字典最多 20000 行")
    tmp = resolved.with_name(f".{resolved.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(entries) + "\n")
        os.replace(tmp, resolved)
        os.chmod(resolved, 0o600)
        if os.name != "nt":
            try:
                shutil.chown(resolved, user=SERVICE_USER, group=SERVICE_USER)
            except (LookupError, PermissionError):
                pass
    finally:
        Path(tmp).unlink(missing_ok=True)
    return len(entries)


# ---------------------------------------------------------------- activity


def worker_activity(limit: int = 25) -> list[dict[str, object]]:
    """Reconstruct recent task outcomes from the worker journal."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "autobook-worker.service", "-n", "4000", "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    tasks: dict[str, dict[str, object]] = {}

    def touch(task_id: str, stamp: str) -> dict[str, object]:
        entry = tasks.setdefault(
            task_id,
            {"id": task_id, "title": "", "status": "running", "message": "", "url": "",
             "started": stamp, "updated": stamp},
        )
        entry["updated"] = stamp
        return entry

    for line in result.stdout.splitlines():
        stamp_match = STAMP_RE.match(line)
        stamp = stamp_match.group(1) if stamp_match else ""
        claim = CLAIM_RE.search(line)
        if claim:
            entry = touch(claim.group(1), stamp)
            entry["title"] = claim.group(2).strip()
            entry["status"] = "running"
            entry["started"] = stamp
            continue
        done = DONE_RE.search(line)
        if done:
            entry = touch(done.group(1), stamp)
            entry["status"] = "completed"
            entry["url"] = done.group(2)
            entry["message"] = "已交付"
            continue
        failed = FAIL_RE.search(line)
        if failed:
            entry = touch(failed.group(1), stamp)
            entry["status"] = "failed"
            entry["message"] = redact(failed.group(2))[:300]
            continue
        progress = PROGRESS_RE.search(line)
        if progress:
            entry = touch(progress.group(1), stamp)
            if entry["status"] == "running":
                entry["message"] = redact(progress.group(2))[:300]

    ordered = sorted(tasks.values(), key=lambda item: str(item["updated"]), reverse=True)
    return ordered[:limit]
