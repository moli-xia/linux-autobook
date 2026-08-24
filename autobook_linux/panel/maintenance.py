"""Maintenance actions and worker activity reporting."""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

from autobook_linux.panel import services
from autobook_linux.panel.envfile import read_env_file, write_env_file
from autobook_linux.panel.jobs import Job, JobManager
from autobook_linux.panel.schema import key_order
from autobook_linux.panel.services import redact
from autobook_linux.panel.settings import PanelSettings, in_container, service_user

BACKUP_DIR = Path("/var/backups/linux-autobook")

CLAIM_RE = re.compile(r"领取任务 #(\d+): (.*)$")
PROGRESS_RE = re.compile(r"\[#(\d+)\] (.*)$")
DONE_RE = re.compile(r"任务 #(\d+) 完成: (\S+)")
FAIL_RE = re.compile(r"任务 #(\d+) 失败: (.*)$")
STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ][\d:,+-]+)")


def generate_token() -> str:
    return secrets.token_hex(32)


# --------------------------------------------------------------------- jobs


def fix_dependencies(manager: JobManager) -> Job:
    if in_container():
        script = (
            "echo '容器镜像已内置全部依赖，无需安装。'; "
            "for tool in 7z aria2c openssl; do printf '%-10s ' \"$tool\"; command -v \"$tool\" || echo '缺失'; done"
        )
        return manager.spawn_command("deps", "检查系统依赖", ["bash", "-lc", script], timeout=120)
    script = (
        "set -e; export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y p7zip-full aria2 openssl ca-certificates curl; "
        "echo '依赖检查完成'; command -v 7z; command -v aria2c"
    )
    return manager.spawn_command("deps", "修复系统依赖", ["bash", "-lc", script], timeout=900)


def fix_permissions(settings: PanelSettings, manager: JobManager) -> Job:
    install = settings.install_dir
    owner = service_user()
    own = f"chown -R {owner}:{owner} '{install}/runtime'; " if owner else ""
    own_dict = (
        f"chown {owner}:{owner} '{install}/password.txt'; " if owner else ""
    )
    script = (
        f"set -e; "
        f"mkdir -p '{install}/runtime'; chmod 750 '{install}/runtime'; "
        f"{own}"
        f"if [ -f '{install}/password.txt' ]; then {own_dict}chmod 600 '{install}/password.txt'; fi; "
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
    owner = service_user()
    chown_step = f"chown {owner}:{owner} '{cert}' '{key}'; " if owner else ""
    script = (
        f"set -e; rm -f '{cert}' '{key}'; "
        f"openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 "
        f"-keyout '{key}' -out '{cert}' -subj '/CN={safe_host}' "
        f"-addext 'subjectAltName={san},DNS:localhost,IP:127.0.0.1'; "
        f"{chown_step}chmod 644 '{cert}'; chmod 640 '{key}'; "
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


def run_cleanup(settings: PanelSettings, manager: JobManager, execute: bool) -> Job:
    """Sweep expired deliveries and transfer leftovers off the drives.

    The services already do this on a timer; this is the on-demand button, and
    it previews by default so an operator can see what would go first.
    """
    python = settings.install_dir / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    script = str(settings.install_dir / "tools" / "storage_sweep.py")
    command = [str(python), script] + (["--execute"] if execute else [])
    title = "清理网盘存储" if execute else "预览可清理的文件"
    # spawn_command replaces the environment wholesale, so merge rather than
    # hand the child a bare one-key env with no PATH.
    environment = dict(os.environ, ADMIN_CONFIG_DIR=str(settings.config_dir))
    return manager.spawn_command(
        "cleanup", title, command, cwd=settings.install_dir,
        env=environment, timeout=1800,
    )


def update_application(settings: PanelSettings, manager: JobManager) -> Job:
    if in_container():
        raise ValueError(
            "容器部署请在宿主机拉取新镜像后重建容器："
            "docker compose pull && docker compose up -d"
        )
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


def switch_role_in_container(settings: PanelSettings, role: str) -> str:
    """Change roles without reinstalling: the supervisor owns the processes."""
    if role not in {"all", "gateway", "worker"}:
        raise ValueError("无效的角色")
    state = read_env_file(settings.install_env)
    state["INSTALL_ROLE"] = role
    write_env_file(settings.install_env, state, ["INSTALL_DIR", "CONFIG_DIR", "INSTALL_ROLE", "PUBLIC_HOST"])
    wanted = {"gateway", "worker"} if role == "all" else {role}
    stopped = []
    for name in ("gateway", "worker"):
        if name not in wanted and services.status(name)["running"]:
            services.control(name, "stop")
            stopped.append(name)
    suffix = f"，已停止 {'、'.join(stopped)}" if stopped else ""
    return f"本机角色已切换为 {role}{suffix}。请在概览页启动需要的服务。"


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
    if in_container():
        # Docker's restart policy brings the container straight back up.
        subprocess.Popen(
            ["bash", "-lc", "sleep 1; kill 1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return
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


# ---------------------------------------------------------------- activity


def worker_activity(limit: int = 25) -> list[dict[str, object]]:
    """Reconstruct recent task outcomes from the worker journal."""
    try:
        text = services.logs("worker", lines=1000)
    except Exception:
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

    for line in text.splitlines():
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
