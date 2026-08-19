"""HTTPS administration panel for linux-autobook.

The panel intentionally uses only the Python standard library.  It stores no
plain-text login password, protects every mutation with a CSRF token, and only
allows a fixed list of service actions and configuration keys.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_COOKIE = "autobook_admin_session"
PBKDF2_ITERATIONS = 600_000
MANAGED_SERVICES = {"gateway": "autobook-gateway.service", "worker": "autobook-worker.service"}


@dataclass(frozen=True)
class ConfigField:
    key: str
    target: str  # gateway | worker | both
    section: str
    label: str
    default: str = ""
    secret: bool = False
    help_text: str = ""
    options: tuple[str, ...] = ()


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("SITE_BASE_URL", "worker", "任务网站", "任务网站地址", "https://544544.xyz"),
    ConfigField("WORKER_TOKEN", "worker", "任务网站", "Worker Token", secret=True),
    ConfigField("WORKER_ID", "worker", "任务网站", "Worker 唯一标识", "linux-worker-1"),
    ConfigField("WORKER_QUEUE", "worker", "任务网站", "领取队列", "pdf", options=("pdf", "ocr", "all")),
    ConfigField("POLL_SECONDS", "worker", "任务网站", "空队列轮询秒数", "15"),
    ConfigField("CONCURRENCY", "worker", "任务网站", "Worker 并发任务数", "3"),
    ConfigField("LEASE_HEARTBEAT_SECONDS", "worker", "任务网站", "任务续租心跳秒数", "60"),

    ConfigField("BAIDU_GATEWAY_URL", "worker", "Worker 下载网关", "网关 HTTPS 地址", "https://127.0.0.1:8765"),
    ConfigField("BAIDU_GATEWAY_TOKEN", "both", "Worker 下载网关", "网关共享令牌", secret=True, help_text="修改时会同时写入网关与 Worker。"),
    ConfigField("BAIDU_GATEWAY_CA_FILE", "worker", "Worker 下载网关", "网关 CA/证书路径", "/opt/autobook-linux/runtime/gateway.crt"),
    ConfigField("BAIDU_GATEWAY_TIMEOUT_SECONDS", "worker", "Worker 下载网关", "下载总超时秒数", "7200"),
    ConfigField("BAIDU_GATEWAY_POLL_SECONDS", "worker", "Worker 下载网关", "状态轮询秒数", "3"),

    ConfigField("BAIDU_AUTH_FILE", "gateway", "百度网盘", "扫码凭据文件", "/opt/autobook-linux/runtime/baidu_credentials.json"),
    ConfigField("BAIDU_BDUSS", "gateway", "百度网盘", "BDUSS（手工 Cookie）", secret=True),
    ConfigField("BAIDU_STOKEN", "gateway", "百度网盘", "STOKEN（手工 Cookie）", secret=True),
    ConfigField("BAIDU_BAIDUID", "gateway", "百度网盘", "BAIDUID（可选）", secret=True),
    ConfigField("BAIDU_PANWEB", "gateway", "百度网盘", "PANWEB", "1"),
    ConfigField("BAIDU_GROUP_NAME", "gateway", "百度网盘", "目标群名称", "读秀12群"),
    ConfigField("BAIDU_GROUP_GID", "gateway", "百度网盘", "目标群 GID", "498636198303058255"),
    ConfigField("BAIDU_SAVE_DIR", "gateway", "百度网盘", "个人网盘临时目录", "/autobook_inbox"),
    ConfigField("BAIDU_PROXY", "gateway", "百度网盘", "百度代理（可选）"),
    ConfigField("BAIDU_QR_PATH", "gateway", "百度网盘", "二维码文件", "/opt/autobook-linux/runtime/baidu-login-qr.png"),
    ConfigField("BAIDU_QR_TIMEOUT_SECONDS", "gateway", "百度网盘", "扫码等待秒数", "120"),
    ConfigField("DOWNLOAD_UA", "gateway", "百度下载", "下载 User-Agent", "netdisk;P2SP;3.0.20.56;netdisk;7.36.0.6;PC;PC-Windows;10.0.22621;WindowsBaiduYunGuanJia"),
    ConfigField("ARIA2_SPLIT", "gateway", "百度下载", "aria2 分片数", "16"),
    ConfigField("ARIA2_MAX_CONNECTION", "gateway", "百度下载", "aria2 最大连接数", "16"),
    ConfigField("DOWNLOAD_TIMEOUT_SECONDS", "gateway", "百度下载", "下载/解压超时秒数", "1800"),
    ConfigField("FULL_SYNC_MAX_PAGES", "gateway", "百度下载", "手工索引最大页数", "2000"),

    ConfigField("GATEWAY_BIND", "gateway", "网关服务", "监听地址", "0.0.0.0"),
    ConfigField("GATEWAY_PORT", "gateway", "网关服务", "监听端口", "8765"),
    ConfigField("GATEWAY_TLS_CERT", "gateway", "网关服务", "TLS 证书", "/opt/autobook-linux/runtime/gateway.crt"),
    ConfigField("GATEWAY_TLS_KEY", "gateway", "网关服务", "TLS 私钥", "/opt/autobook-linux/runtime/gateway.key", secret=True),
    ConfigField("GATEWAY_CONCURRENCY", "gateway", "网关服务", "百度下载并发数", "3"),
    ConfigField("GATEWAY_CACHE_TTL_SECONDS", "gateway", "网关服务", "完成文件缓存秒数", "3600"),
    ConfigField("GATEWAY_JOB_ROOT", "gateway", "网关服务", "网关任务目录", "/opt/autobook-linux/runtime/gateway/jobs"),

    ConfigField("WORK_ROOT", "worker", "本地处理", "任务工作目录", "/opt/autobook-linux/runtime/work"),
    ConfigField("DOWNLOAD_ROOT", "worker", "本地处理", "Worker 下载目录", "/opt/autobook-linux/runtime/downloads"),
    ConfigField("INDEX_DB", "worker", "本地处理", "本地索引数据库", "/opt/autobook-linux/runtime/library_index.sqlite3"),
    ConfigField("PASSWORD_DICT", "worker", "本地处理", "解压密码字典", "/opt/autobook-linux/password.txt"),
    ConfigField("SEVEN_ZIP_BIN", "worker", "本地处理", "7-Zip 命令", "7z"),
    ConfigField("ARIA2C_BIN", "worker", "本地处理", "aria2c 命令", "aria2c"),
    ConfigField("PDG_DPI", "worker", "本地处理", "PDG 输出 DPI", "200"),

    ConfigField("DRIVE_EMAIL", "worker", "结果网盘", "Cloudreve 账号", secret=True),
    ConfigField("DRIVE_PASSWORD", "worker", "结果网盘", "Cloudreve 密码", secret=True),
    ConfigField("DRIVE_BASE_URL", "worker", "结果网盘", "Cloudreve 地址", "https://drive.netupdown.com"),
    ConfigField("DRIVE_POLICY_ID", "worker", "结果网盘", "存储策略 ID（可选）"),
    ConfigField("DRIVE_TARGET_DIR", "worker", "结果网盘", "上传目录", "transfer"),
    ConfigField("DRIVE_EXPIRE_DAYS", "worker", "结果网盘", "分享有效天数", "7"),
    ConfigField("DRIVE_REQUIRE_UPLOAD_DATE_VERIFY", "worker", "结果网盘", "校验上传日期", "1", options=("1", "0")),
)

BLANK_IS_MEANINGFUL = {
    "BAIDU_GATEWAY_CA_FILE",  # use the operating-system CA store
    "BAIDU_GROUP_GID",       # resolve by group name instead
    "BAIDU_GROUP_NAME",      # an explicit GID is sufficient
    "BAIDU_PROXY",
    "DRIVE_POLICY_ID",
}


@dataclass
class AdminSettings:
    bind: str
    port: int
    tls_cert: Path
    tls_key: Path
    state_file: Path
    gateway_env: Path
    worker_env: Path
    session_seconds: int
    public_host: str
    role: str = "all"

    @classmethod
    def load(cls) -> "AdminSettings":
        role = os.environ.get("ADMIN_ROLE", "all").strip().lower()
        if role not in {"all", "gateway", "worker"}:
            role = "all"
        return cls(
            bind=os.environ.get("ADMIN_BIND", "0.0.0.0"),
            port=int(os.environ.get("ADMIN_PORT", "8766")),
            tls_cert=Path(os.environ.get("ADMIN_TLS_CERT", "/etc/linux-autobook/admin.crt")),
            tls_key=Path(os.environ.get("ADMIN_TLS_KEY", "/etc/linux-autobook/admin.key")),
            state_file=Path(os.environ.get("ADMIN_STATE_FILE", "/etc/linux-autobook/admin-state.json")),
            gateway_env=Path(os.environ.get("ADMIN_GATEWAY_ENV", "/etc/linux-autobook/gateway.env")),
            worker_env=Path(os.environ.get("ADMIN_WORKER_ENV", "/etc/linux-autobook/worker.env")),
            session_seconds=max(300, int(os.environ.get("ADMIN_SESSION_SECONDS", "28800"))),
            public_host=os.environ.get("ADMIN_PUBLIC_HOST", "").strip(),
            role=role,
        )

    def has_role(self, role: str) -> bool:
        return self.role == "all" or self.role == role


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
            if raw.split("=", 1)[1].lstrip().startswith('"'):
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key.strip()] = value
    return values


def _quote_env(value: str) -> str:
    clean = str(value).replace("\r", " ").replace("\n", " ")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_file(path: Path, values: dict[str, str], target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    managed_order = [field.key for field in CONFIG_FIELDS if field.target in {target, "both"}]
    ordered = list(
        dict.fromkeys(
            [key for key in managed_order if key in values]
            + sorted(key for key in values if key not in managed_order)
        )
    )
    body = "# Managed by linux-autobook admin panel. chmod 600\n"
    body += "\n".join(f"{key}={_quote_env(values.get(key, ''))}" for key in ordered) + "\n"
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def apply_config_defaults(values: dict[str, str], target: str) -> None:
    """Fill non-empty application defaults without inventing blank env vars."""
    for field in CONFIG_FIELDS:
        if (
            field.target in {target, "both"}
            and field.default
            and (field.key not in values or (not values[field.key] and field.key not in BLANK_IS_MEANINGFUL))
        ):
            values[field.key] = field.default


class PasswordStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not path.exists():
            self.set_credentials("admin", "admin", initial=True)

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)

    def set_credentials(self, username: str, password: str, initial: bool = False) -> None:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            raise ValueError("用户名必须为 3–40 位字母、数字、点、下划线或连字符")
        if not initial and len(password) < 10:
            raise ValueError("新密码至少需要 10 个字符")
        salt = secrets.token_bytes(24)
        payload = {
            "username": username,
            "salt": salt.hex(),
            "password_hash": self._derive(password, salt).hex(),
            "must_change": bool(initial),
            "updated_at": int(time.time()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        with self._lock:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(payload, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            finally:
                tmp.unlink(missing_ok=True)

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def authenticate(self, username: str, password: str) -> bool:
        try:
            state = self._read()
            salt = bytes.fromhex(state["salt"])
            expected = bytes.fromhex(state["password_hash"])
            actual = self._derive(password, salt)
            return hmac.compare_digest(username.encode(), str(state["username"]).encode()) and hmac.compare_digest(actual, expected)
        except Exception:
            LOGGER.exception("读取管理账号状态失败")
            return False

    def username(self) -> str:
        return str(self._read().get("username") or "admin")

    def must_change(self) -> bool:
        return bool(self._read().get("must_change", False))


class SessionStore:
    def __init__(self, lifetime: int) -> None:
        self.lifetime = lifetime
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {"username": username, "csrf": csrf, "expires": time.time() + self.lifetime}
        return token, csrf

    def get(self, token: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            for key in [key for key, value in self._sessions.items() if value["expires"] < now]:
                self._sessions.pop(key, None)
            session = self._sessions.get(token)
            if session:
                session["expires"] = now + self.lifetime
                return dict(session)
        return None

    def delete(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class LoginLimiter:
    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        cutoff = time.time() - 600
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(address, []) if stamp > cutoff]
            self._attempts[address] = recent
            return len(recent) < 8

    def fail(self, address: str) -> None:
        with self._lock:
            self._attempts.setdefault(address, []).append(time.time())

    def clear(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


class QrLoginManager:
    def __init__(self, settings: AdminSettings) -> None:
        self.settings = settings
        self.process: subprocess.Popen[str] | None = None
        self.status = "未启动"
        self.qr_path = PROJECT_ROOT / "runtime" / "admin-baidu-login-qr.png"
        self.log_path = PROJECT_ROOT / "runtime" / "admin-baidu-login.log"
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有扫码登录正在进行")
            gateway_env = read_env_file(self.settings.gateway_env)
            environment = dict(os.environ)
            environment.update(gateway_env)
            environment["BAIDU_QR_PATH"] = str(self.qr_path)
            self.qr_path.unlink(missing_ok=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = self.log_path.open("w", encoding="utf-8")
            command = [str(PROJECT_ROOT / ".venv" / "bin" / "python"), str(PROJECT_ROOT / "run_worker.py"), "--baidu-login", "--qr-output", str(self.qr_path)]
            runuser = shutil.which("runuser")
            if os.geteuid() == 0 and runuser:
                # Credentials must belong to the same unprivileged account as
                # the gateway service, otherwise its 0600 file is unreadable.
                command = [runuser, "--user", "autobook", "--preserve-environment", "--", *command]
            self.process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.status = "正在生成二维码"
            threading.Thread(target=self._watch, args=(self.process, log_file), daemon=True).start()

    def _watch(self, process: subprocess.Popen[str], log_file: Any) -> None:
        code = process.wait()
        log_file.close()
        with self._lock:
            self.status = "扫码登录成功，网关已重启" if code == 0 else f"扫码登录失败（退出码 {code}）"
        if code == 0:
            subprocess.run(["systemctl", "restart", MANAGED_SERVICES["gateway"]], timeout=60, check=False)

    def snapshot(self) -> tuple[str, bool, str]:
        with self._lock:
            if self.process and self.process.poll() is None and self.qr_path.exists():
                self.status = "等待使用百度网盘 App 扫码并确认"
            status = self.status
        log = ""
        try:
            log = "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
        except FileNotFoundError:
            pass
        return status, self.qr_path.is_file(), log


def service_status(service: str) -> dict[str, str]:
    unit = MANAGED_SERVICES[service]
    load = subprocess.run(
        ["systemctl", "show", unit, "--property=LoadState", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if load in {"not-found", ""}:
        return {"active": "not-installed", "enabled": "not-installed", "load": load or "not-found"}
    active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip()
    enabled = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=10).stdout.strip()
    return {"active": active or "unknown", "enabled": enabled or "unknown", "load": load}


def configuration_issues(role: str, gateway: dict[str, str], worker: dict[str, str]) -> list[str]:
    """Return actionable static configuration errors without making network calls."""
    issues: list[str] = []
    if role == "gateway":
        if not gateway.get("BAIDU_GATEWAY_TOKEN"):
            issues.append("未设置网关共享令牌")
        cert = Path(gateway.get("GATEWAY_TLS_CERT", ""))
        key = Path(gateway.get("GATEWAY_TLS_KEY", ""))
        if not cert.is_file():
            issues.append("网关 TLS 证书不存在")
        if not key.is_file():
            issues.append("网关 TLS 私钥不存在")
        bduss, stoken = gateway.get("BAIDU_BDUSS", ""), gateway.get("BAIDU_STOKEN", "")
        auth_file = Path(gateway.get("BAIDU_AUTH_FILE", str(PROJECT_ROOT / "runtime" / "baidu_credentials.json")))
        if bool(bduss) != bool(stoken):
            issues.append("BDUSS 与 STOKEN 必须同时填写")
        elif not bduss and not auth_file.is_file():
            issues.append("尚未完成百度网盘扫码登录")
        if not gateway.get("BAIDU_GROUP_GID") and not gateway.get("BAIDU_GROUP_NAME"):
            issues.append("未设置百度群 GID 或群名称")
    elif role == "worker":
        if not worker.get("WORKER_TOKEN"):
            issues.append("未设置任务网站 Worker Token")
        site = worker.get("SITE_BASE_URL", "")
        if not site.startswith(("http://", "https://")):
            issues.append("任务网站地址格式无效")
        gateway_url = worker.get("BAIDU_GATEWAY_URL", "")
        if not gateway_url.startswith("https://"):
            issues.append("未设置有效的 HTTPS 下载网关地址")
        if not worker.get("BAIDU_GATEWAY_TOKEN"):
            issues.append("未设置下载网关共享令牌")
        ca_file = worker.get("BAIDU_GATEWAY_CA_FILE", "")
        if ca_file and not Path(ca_file).is_file():
            issues.append("指定的网关 CA/证书文件不存在；公有证书应留空")
        if not worker.get("DRIVE_EMAIL"):
            issues.append("未设置结果网盘账号")
        if not worker.get("DRIVE_PASSWORD"):
            issues.append("未设置结果网盘密码")
        if not worker.get("DRIVE_BASE_URL", "").startswith("https://"):
            issues.append("结果网盘地址必须使用 HTTPS")
    return issues


def service_logs(service: str, lines: int = 80) -> str:
    unit = MANAGED_SERVICES[service]
    result = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(min(200, max(10, lines))), "--no-pager", "--output=short-iso"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
    )
    return re.sub(r"https?://\S*(?:dlink|download)\S*", "[下载链接已隐藏]", result.stdout[-60_000:])


STYLE = """
:root{color-scheme:light;--bg:#f4f7fb;--card:#fff;--line:#dfe6f0;--text:#172033;--muted:#64748b;--accent:#2563eb;--accent2:#eff6ff;--ok:#15803d;--okbg:#f0fdf4;--bad:#dc2626;--badbg:#fef2f2;--warn:#a16207;--warnbg:#fffbeb}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1120px;margin:auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px}.brand h1{font-size:24px;margin:0}.brand p,.muted{margin:3px 0;color:var(--muted)}nav{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 24px;border-bottom:1px solid var(--line);padding-bottom:10px}nav a{padding:8px 12px;border-radius:8px;color:#334155;text-decoration:none;font-weight:650}nav a:hover,nav a.current{background:var(--accent2);color:var(--accent)}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 4px 16px #0f172a0a}.card h2{margin:0 0 6px;font-size:19px}.card h3{margin:0 0 8px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}.field{margin:6px 0}.field label{display:flex;align-items:center;gap:8px;font-weight:680;margin-bottom:6px}.field small{display:block;color:var(--muted);min-height:20px;margin-top:4px}.field input,.field select,.field textarea{width:100%;background:#fff;color:var(--text);border:1px solid #cbd5e1;border-radius:9px;padding:10px 11px;font:inherit}.field input:focus,.field select:focus,.field textarea:focus{outline:2px solid #bfdbfe;border-color:var(--accent)}.field textarea{min-height:130px}button,.button{display:inline-block;border:0;border-radius:9px;padding:10px 15px;background:var(--accent);color:#fff;font-weight:720;text-decoration:none;cursor:pointer;font:inherit}button.secondary,.button.secondary{background:#e2e8f0;color:#243047}button.danger{background:var(--bad)}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:14px}.status,.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 9px;border-radius:99px;background:#f1f5f9;color:#475569;font-size:13px}.pill.ok,.status.active{color:var(--ok);background:var(--okbg)}.pill.bad{color:var(--bad);background:var(--badbg)}.dot{width:8px;height:8px;border-radius:50%;background:var(--bad)}.active .dot{background:var(--ok)}.notice{padding:12px 14px;border-radius:9px;background:var(--warnbg);border:1px solid #fde68a;color:var(--warn);margin:12px 0}.success{background:var(--okbg);border-color:#bbf7d0;color:var(--ok)}.error{background:var(--badbg);border-color:#fecaca;color:#991b1b}.steps{counter-reset:step}.step{position:relative;padding-left:44px;min-height:36px;margin:16px 0}.step:before{counter-increment:step;content:counter(step);position:absolute;left:0;top:0;width:30px;height:30px;display:grid;place-items:center;border-radius:50%;background:var(--accent2);color:var(--accent);font-weight:800}details{border-top:1px solid var(--line);padding:14px 0}details:first-of-type{border-top:0}summary{cursor:pointer;font-weight:750;font-size:16px}.issue-list{margin:10px 0 0;padding-left:20px;color:#991b1b}pre{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#e2e8f0;border-radius:9px;padding:14px;max-height:430px;overflow:auto}.login{max-width:430px;margin:9vh auto}.qr{max-width:320px;background:white;padding:12px;border:1px solid var(--line);border-radius:10px}.hero{display:flex;justify-content:space-between;align-items:center;gap:20px}.hero h2{font-size:22px}.kpi{font-size:28px;font-weight:800}.inline-form{display:inline}.secret-note{color:var(--warn)}@media(max-width:650px){main{padding:14px}.top,.hero{align-items:flex-start;flex-direction:column}.card{padding:15px}nav{overflow-x:auto;flex-wrap:nowrap}nav a{white-space:nowrap}}
"""


def page(title: str, content: str) -> bytes:
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{content}</main></body></html>"""
    return document.encode("utf-8")


def make_handler(settings: AdminSettings) -> type[BaseHTTPRequestHandler]:
    passwords = PasswordStore(settings.state_file)
    sessions = SessionStore(settings.session_seconds)
    limiter = LoginLimiter()
    qr_login = QrLoginManager(settings)

    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "autobook-admin/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("admin http: " + fmt, *args)

        def _headers(self, status: int, content_type: str, length: int, cookie: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", cookie: str = "") -> None:
            self._headers(status, content_type, len(body), cookie)
            self.wfile.write(body)

        def _redirect(self, location: str, cookie: str = "") -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256 * 1024:
                raise ValueError("请求体大小无效")
            parsed = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}

        def _session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""

        def _session(self) -> dict[str, Any] | None:
            return sessions.get(self._session_token())

        def _require_session(self) -> dict[str, Any] | None:
            session = self._session()
            if not session:
                self._redirect("/login")
                return None
            return session

        def _csrf(self, form: dict[str, str], session: dict[str, Any]) -> bool:
            return hmac.compare_digest(form.get("csrf", ""), str(session["csrf"]))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._send(200, b'{"status":"ok"}\n', "application/json; charset=utf-8")
                return
            if path == "/login":
                if self._session():
                    self._redirect("/")
                    return
                message = "<div class='notice'>首次安装默认账号和密码均为 admin。登录后请立即修改。</div>"
                content = f"<div class='login'><div class='card'><h1>linux-autobook</h1><p>系统管理面板</p>{message}<form method='post' action='/login'><div class='field'><label>用户名</label><input name='username' autocomplete='username' required></div><div class='field'><label>密码</label><input type='password' name='password' autocomplete='current-password' required></div><p><button type='submit'>登录</button></p></form></div></div>"
                self._send(200, page("登录", content))
                return
            if path == "/qr.png":
                if not self._require_session():
                    return
                if not qr_login.qr_path.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                body = qr_login.qr_path.read_bytes()
                self._send(200, body, "image/png")
                return
            session = self._require_session()
            if not session:
                return
            if path == "/":
                self._overview(session)
            elif path == "/setup":
                self._setup(session)
            elif path == "/advanced":
                self._advanced(session)
            elif path == "/tools":
                self._tools(session)
            elif path == "/account":
                self._account(session)
            elif path.startswith("/logs/"):
                name = path.rsplit("/", 1)[-1]
                if name not in MANAGED_SERVICES:
                    self._send(404, b"not found", "text/plain")
                    return
                logs = html.escape(service_logs(name))
                content = self._top(session, "") + f"<div class='card'><h2>{html.escape(name)} 日志</h2><p class='muted'>最近 80 行 systemd 日志，下载链接会自动隐藏。</p><pre>{logs}</pre><a class='button secondary' href='/'>返回概况</a></div>"
                self._send(200, page("服务日志", content))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/login":
                self._login()
                return
            session = self._require_session()
            if not session:
                return
            try:
                form = self._form()
            except ValueError as exc:
                self._send(400, page("错误", f"<div class='card error'>{html.escape(str(exc))}</div>"))
                return
            if not self._csrf(form, session):
                self._send(403, page("错误", "<div class='card error'>CSRF 校验失败，请刷新页面后重试。</div>"))
                return
            try:
                if path == "/save":
                    self._save_config(form)
                    start_role = form.get("start_role", "")
                    if start_role:
                        self._start_configured_role(start_role)
                    next_path = form.get("next", "/setup")
                    if next_path not in {"/", "/setup", "/advanced", "/tools"}:
                        next_path = "/setup"
                    suffix = "?saved=1&started=1" if start_role else "?saved=1"
                    self._redirect(next_path + suffix)
                elif path == "/service":
                    self._service_action(form)
                    self._redirect("/?service=1")
                elif path == "/check":
                    self._check_role(session, form)
                elif path == "/password":
                    self._change_password(form)
                    sessions.clear()
                    self._redirect("/login", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
                elif path == "/baidu-login":
                    if not settings.has_role("gateway"):
                        raise ValueError("当前节点未安装百度下载网关")
                    qr_login.start()
                    self._redirect("/?qr=1")
                elif path == "/password-dict":
                    self._save_password_dict(form)
                    self._redirect("/?dict=1")
                elif path == "/logout":
                    sessions.delete(self._session_token())
                    self._redirect("/login", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as exc:
                LOGGER.exception("管理操作失败 path=%s", path)
                self._send(400, page("操作失败", self._top(session, "") + f"<div class='card error'><h2>操作失败</h2><p>{html.escape(str(exc))}</p><a class='button secondary' href='/'>返回</a></div>"))

        def _login(self) -> None:
            address = self.client_address[0]
            if not limiter.allowed(address):
                self._send(429, page("请稍后重试", "<div class='login card error'>登录失败次数过多，请 10 分钟后重试。</div>"))
                return
            try:
                form = self._form()
            except ValueError:
                self._send(400, b"bad request", "text/plain")
                return
            if not passwords.authenticate(form.get("username", ""), form.get("password", "")):
                limiter.fail(address)
                time.sleep(0.4)
                self._send(401, page("登录失败", "<div class='login card error'><p>用户名或密码不正确。</p><a class='button secondary' href='/login'>返回</a></div>"))
                return
            limiter.clear(address)
            token, _ = sessions.create(passwords.username())
            cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={settings.session_seconds}; Secure; HttpOnly; SameSite=Strict"
            self._redirect("/", cookie)

        def _roles(self) -> list[str]:
            return [role for role in ("gateway", "worker") if settings.has_role(role)]

        def _top(self, session: dict[str, Any], current: str) -> str:
            links = (("/", "概况"), ("/setup", "快速设置"), ("/advanced", "高级设置"), ("/tools", "工具与诊断"), ("/account", "管理账号"))
            nav = "".join(f"<a class='{'current' if path == current else ''}' href='{path}'>{label}</a>" for path, label in links)
            role_label = {"all": "网关 + Worker", "gateway": "仅网关", "worker": "仅 Worker"}[settings.role]
            return f"<div class='top'><div class='brand'><h1>linux-autobook</h1><p>{role_label} · 当前用户 {html.escape(str(session['username']))}</p></div><form method='post' action='/logout'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><button class='secondary' type='submit'>退出</button></form></div><nav>{nav}</nav>"

        def _notices(self) -> str:
            query = parse_qs(urlparse(self.path).query)
            notices: list[str] = []
            if passwords.must_change():
                notices.append("<div class='notice'>当前仍使用默认密码 admin。完成基础配置后，请到“管理账号”立即修改。</div>")
            if query.get("saved"):
                text = "配置已保存。" + (" 服务也已启动。" if query.get("started") else "")
                notices.append(f"<div class='notice success'>{text}</div>")
            if query.get("service"):
                notices.append("<div class='notice success'>服务操作已完成。</div>")
            if query.get("qr"):
                notices.append("<div class='notice success'>二维码任务已启动，请在“工具与诊断”页面扫码。</div>")
            return "".join(notices)

        def _values(self) -> tuple[dict[str, str], dict[str, str]]:
            return read_env_file(settings.gateway_env), read_env_file(settings.worker_env)

        def _field(self, key: str, gateway: dict[str, str], worker: dict[str, str]) -> str:
            field = next(item for item in CONFIG_FIELDS if item.key == key)
            source = gateway if field.target == "gateway" else worker
            if field.target == "both":
                source = gateway if gateway.get(field.key) else worker
            current = source.get(field.key, field.default)
            configured = bool(current)
            badge = ""
            if field.secret:
                badge = f"<span class='pill {'ok' if configured else 'bad'}'>{'已配置' if configured else '必填'}</span>"
            help_text = field.help_text or ("已保存的敏感值不会回显；不修改请留空。" if field.secret else "")
            if key == "BAIDU_GATEWAY_CA_FILE":
                help_text = "自签名网关填写证书路径；Let’s Encrypt 等公有证书请留空。"
            if field.options:
                options = "".join(f"<option value='{html.escape(option)}'{' selected' if option == current else ''}>{html.escape(option)}</option>" for option in field.options)
                control = f"<select name='{field.key}'>{options}</select>"
            else:
                value = "" if field.secret else current
                input_type = "password" if field.secret else "text"
                placeholder = "已保存；留空保持" if field.secret and current else ("尚未配置" if field.secret else "")
                control = f"<input type='{input_type}' name='{field.key}' value='{html.escape(value, quote=True)}' placeholder='{html.escape(placeholder, quote=True)}' autocomplete='off'>"
            return f"<div class='field'><label>{html.escape(field.label)} {badge}</label>{control}<small>{html.escape(help_text)}</small></div>"

        def _role_issues(self, role: str) -> list[str]:
            gateway, worker = self._values()
            return configuration_issues(role, gateway, worker)

        def _service_card(self, session: dict[str, Any], role: str, issues: list[str]) -> str:
            state = service_status(role)
            active = state["active"] == "active"
            status_text = "运行中" if active else ("未安装" if state["active"] == "not-installed" else "已停止")
            issue_html = "" if not issues else "<ul class='issue-list'>" + "".join(f"<li>{html.escape(issue)}</li>" for issue in issues) + "</ul>"
            actions = ""
            if state["active"] != "not-installed":
                buttons = "".join(f"<button class='{('danger' if action == 'stop' else 'secondary')}' name='action' value='{action}'>{label}</button>" for action, label in (("start", "启动"), ("restart", "重启"), ("stop", "停止")))
                actions = f"<form class='actions' method='post' action='/service'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><input type='hidden' name='service' value='{role}'>{buttons}<a class='button secondary' href='/logs/{role}'>查看日志</a></form>"
            label = "百度下载网关" if role == "gateway" else "任务 Worker"
            ready = not issues
            return f"<div class='card'><div class='hero'><div><h2>{label}</h2><p class='status {'active' if active else ''}'><span class='dot'></span>{status_text}</p></div><span class='pill {'ok' if ready else 'bad'}'>{'配置完整' if ready else f'缺少 {len(issues)} 项'}</span></div>{issue_html}{actions}</div>"

        def _overview(self, session: dict[str, Any]) -> None:
            role_issues = {role: self._role_issues(role) for role in self._roles()}
            ready_count = sum(not issues for issues in role_issues.values())
            content = self._top(session, "/") + self._notices()
            content += f"<div class='card hero'><div><h2>系统概况</h2><p class='muted'>先完成快速设置，再做连通性检测，最后启动服务。</p></div><div><div class='kpi'>{ready_count}/{len(role_issues)}</div><div class='muted'>角色配置就绪</div></div></div>"
            content += "<div class='grid'>" + "".join(self._service_card(session, role, issues) for role, issues in role_issues.items()) + "</div>"
            content += "<div class='card steps'><h2>首次使用只需三步</h2><div class='step'><strong>填写必需信息</strong><div class='muted'>在快速设置中填写带“必填”标记的内容。</div></div><div class='step'><strong>扫码并检测</strong><div class='muted'>网关节点完成百度扫码；然后运行预检定位网络或凭据问题。</div></div><div class='step'><strong>启动服务</strong><div class='muted'>配置不完整时面板会阻止启动，避免 systemd 重启风暴。</div></div><div class='actions'><a class='button' href='/setup'>开始快速设置</a><a class='button secondary' href='/tools'>打开诊断工具</a></div></div>"
            self._send(200, page("系统概况", content))

        def _setup(self, session: dict[str, Any]) -> None:
            gateway, worker = self._values()
            content = self._top(session, "/setup") + self._notices()
            content += "<div class='card'><h2>快速设置</h2><p class='muted'>这里只保留启动所需项目。其余参数保持推荐默认值，可稍后在高级设置修改。</p></div>"
            if settings.has_role("gateway"):
                keys = ("BAIDU_GATEWAY_TOKEN", "BAIDU_GROUP_GID", "BAIDU_GROUP_NAME", "BAIDU_PROXY")
                fields = "".join(self._field(key, gateway, worker) for key in keys)
                issues = self._role_issues("gateway")
                issue_html = "" if not issues else "<div class='notice'>还需完成：" + "；".join(html.escape(x) for x in issues) + "</div>"
                content += f"<form method='post' action='/save'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><input type='hidden' name='next' value='/setup'><div class='card'><h2>百度下载网关</h2><p class='muted'>共享令牌必须与所有 Worker 一致。百度登录请在保存后前往工具页扫码。</p>{issue_html}<div class='grid'>{fields}</div><div class='actions'><button type='submit'>保存网关设置</button><a class='button secondary' href='/tools'>去扫码/检测</a></div></div></form>"
            if settings.has_role("worker"):
                keys = ("SITE_BASE_URL", "WORKER_TOKEN", "WORKER_ID", "CONCURRENCY", "BAIDU_GATEWAY_URL", "BAIDU_GATEWAY_TOKEN", "BAIDU_GATEWAY_CA_FILE", "DRIVE_EMAIL", "DRIVE_PASSWORD", "DRIVE_BASE_URL", "DRIVE_TARGET_DIR")
                fields = "".join(self._field(key, gateway, worker) for key in keys)
                issues = self._role_issues("worker")
                issue_html = "" if not issues else "<div class='notice'>还需完成：" + "；".join(html.escape(x) for x in issues) + "</div>"
                content += f"<form method='post' action='/save'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><input type='hidden' name='next' value='/setup'><div class='card'><h2>任务 Worker</h2><p class='muted'>填写任务网站、中心下载网关和结果网盘三组信息即可运行。</p>{issue_html}<div class='grid'>{fields}</div><div class='actions'><button type='submit'>仅保存</button><button class='secondary' type='submit' name='start_role' value='worker'>保存并启动 Worker</button></div></div></form>"
            self._send(200, page("快速设置", content))

        def _advanced(self, session: dict[str, Any]) -> None:
            gateway, worker = self._values()
            sections: dict[str, list[str]] = {}
            for field in CONFIG_FIELDS:
                if field.target == "gateway" and not settings.has_role("gateway"):
                    continue
                if field.target == "worker" and not settings.has_role("worker"):
                    continue
                sections.setdefault(field.section, []).append(self._field(field.key, gateway, worker))
            groups = "".join(f"<details><summary>{html.escape(section)}</summary><div class='grid'>{''.join(fields)}</div></details>" for section, fields in sections.items())
            content = self._top(session, "/advanced") + self._notices()
            content += f"<form method='post' action='/save'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><input type='hidden' name='next' value='/advanced'><div class='card'><h2>高级设置</h2><p class='muted'>一般无需修改。各分组默认折叠，敏感值不会回显。</p>{groups}<div class='actions'><button type='submit'>保存高级设置</button></div></div></form>"
            self._send(200, page("高级设置", content))

        def _tools(self, session: dict[str, Any]) -> None:
            gateway, worker = self._values()
            content = self._top(session, "/tools") + self._notices()
            checks = []
            for role in self._roles():
                label = "网关" if role == "gateway" else "Worker"
                checks.append(f"<div class='card'><h2>{label} 连通性检测</h2><p class='muted'>检查本地配置、TLS、远端接口和运行依赖，不启动常驻服务。</p><form method='post' action='/check'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><button name='role' value='{role}'>运行 {label} 预检</button></form></div>")
            content += "<div class='grid'>" + "".join(checks) + "</div>"
            if settings.has_role("gateway"):
                qr_status, has_qr, qr_log = qr_login.snapshot()
                qr_image = "<p><img class='qr' src='/qr.png' alt='百度登录二维码'></p>" if has_qr else ""
                content += f"<div class='card'><h2>百度网盘扫码登录</h2><p>{html.escape(qr_status)}</p>{qr_image}<form method='post' action='/baidu-login'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><button type='submit'>生成新二维码</button></form>{f'<pre>{html.escape(qr_log)}</pre>' if qr_log else ''}</div>"
            if settings.has_role("worker"):
                password_path = Path(worker.get("PASSWORD_DICT", str(PROJECT_ROOT / "password.txt")))
                try:
                    password_count = sum(1 for line in password_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
                except (FileNotFoundError, PermissionError):
                    password_count = 0
                content += f"<div class='card'><h2>解压密码字典</h2><p class='muted'>当前 {password_count} 个候选密码。留空提交不会覆盖。</p><form method='post' action='/password-dict'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><div class='field'><textarea name='passwords' placeholder='每行一个密码'></textarea></div><button type='submit'>替换密码字典</button></form></div>"
            self._send(200, page("工具与诊断", content))

        def _account(self, session: dict[str, Any]) -> None:
            content = self._top(session, "/account") + self._notices()
            content += f"<div class='card'><h2>修改管理账号</h2><p class='muted'>修改后所有已登录会话都会退出。</p><form method='post' action='/password'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><div class='grid'><div class='field'><label>新用户名</label><input name='username' value='{html.escape(passwords.username(), quote=True)}' required></div><div class='field'><label>当前密码</label><input type='password' name='current_password' required></div><div class='field'><label>新密码（至少 10 位）</label><input type='password' name='new_password' required></div></div><div class='actions'><button type='submit'>修改并重新登录</button></div></form></div>"
            self._send(200, page("管理账号", content))

        def _save_config(self, form: dict[str, str]) -> None:
            gateway = read_env_file(settings.gateway_env)
            worker = read_env_file(settings.worker_env)
            for field in CONFIG_FIELDS:
                if field.key not in form:
                    continue
                value = form[field.key].strip()
                if field.secret and value == "":
                    continue
                if "\n" in value or "\r" in value or "\x00" in value:
                    raise ValueError(f"{field.label} 含非法换行或空字符")
                if field.target in {"gateway", "both"} and settings.has_role("gateway"):
                    gateway[field.key] = value
                if field.target in {"worker", "both"} and settings.has_role("worker"):
                    worker[field.key] = value
            if settings.has_role("gateway"):
                apply_config_defaults(gateway, "gateway")
                write_env_file(settings.gateway_env, gateway, "gateway")
            if settings.has_role("worker"):
                apply_config_defaults(worker, "worker")
                write_env_file(settings.worker_env, worker, "worker")

        def _start_configured_role(self, role: str) -> None:
            if role not in self._roles():
                raise ValueError("当前节点未安装该角色")
            issues = self._role_issues(role)
            if issues:
                raise ValueError("配置尚未完成：" + "；".join(issues))
            if role == "worker" and settings.role == "all" and service_status("gateway")["active"] != "active":
                raise ValueError("请先在“工具与诊断”完成百度扫码并启动本机下载网关")
            unit = MANAGED_SERVICES[role]
            subprocess.run(["systemctl", "reset-failed", unit], capture_output=True, timeout=20)
            result = subprocess.run(["systemctl", "enable", "--now", unit], capture_output=True, text=True, timeout=90)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "systemctl 启动失败")[-1500:])

        def _run_preflight(self, role: str) -> str:
            if role not in self._roles():
                raise ValueError("当前节点未安装该角色")
            issues = self._role_issues(role)
            if issues:
                return "静态配置检查未通过：\n- " + "\n- ".join(issues)
            values = read_env_file(settings.gateway_env if role == "gateway" else settings.worker_env)
            environment = dict(os.environ)
            environment.update(values)
            script = PROJECT_ROOT / ("run_gateway.py" if role == "gateway" else "run_worker.py")
            command = [str(PROJECT_ROOT / ".venv" / "bin" / "python"), str(script), "--check"]
            runuser = shutil.which("runuser")
            if os.name != "nt" and os.geteuid() == 0 and runuser:
                command = [runuser, "--user", "autobook", "--preserve-environment", "--", *command]
            result = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, errors="replace", timeout=180)
            output = (result.stdout + "\n" + result.stderr).strip()
            for field in CONFIG_FIELDS:
                if field.secret:
                    secret = values.get(field.key, "")
                    if len(secret) >= 4:
                        output = output.replace(secret, "[敏感值已隐藏]")
            output = re.sub(r"(?i)(bduss|stoken|password|token)=\S+", r"\1=[敏感值已隐藏]", output)
            if result.returncode != 0:
                return f"预检失败（退出码 {result.returncode}）：\n{output or '没有输出'}"
            return "预检通过。\n" + (output or "所有检查均正常。")

        def _check_role(self, session: dict[str, Any], form: dict[str, str]) -> None:
            role = form.get("role", "")
            result = self._run_preflight(role)
            success = result.startswith("预检通过")
            label = "网关" if role == "gateway" else "Worker"
            content = self._top(session, "/tools") + f"<div class='card'><h2>{label} 预检结果</h2><div class='notice {'success' if success else 'error'}'>{'通过' if success else '需要处理'}</div><pre>{html.escape(result)}</pre><div class='actions'><a class='button secondary' href='/tools'>返回诊断工具</a></div></div>"
            self._send(200, page("预检结果", content))

        def _service_action(self, form: dict[str, str]) -> None:
            name, action = form.get("service", ""), form.get("action", "")
            if name not in self._roles() or action not in {"start", "stop", "restart"}:
                raise ValueError("不允许的服务操作")
            if action == "start":
                self._start_configured_role(name)
                return
            if action == "restart":
                issues = self._role_issues(name)
                if issues:
                    raise ValueError("配置尚未完成：" + "；".join(issues))
            result = subprocess.run(["systemctl", action, MANAGED_SERVICES[name]], capture_output=True, text=True, timeout=90)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "systemctl 失败")[-1000:])

        def _change_password(self, form: dict[str, str]) -> None:
            if not passwords.authenticate(passwords.username(), form.get("current_password", "")):
                raise ValueError("当前密码不正确")
            passwords.set_credentials(form.get("username", ""), form.get("new_password", ""))

        def _save_password_dict(self, form: dict[str, str]) -> None:
            raw = form.get("passwords", "")
            if not raw.strip():
                return
            worker = read_env_file(settings.worker_env)
            path = Path(worker.get("PASSWORD_DICT", str(PROJECT_ROOT / "password.txt")))
            try:
                path.resolve().relative_to(PROJECT_ROOT.resolve())
            except ValueError as exc:
                raise ValueError("密码字典必须位于项目目录内") from exc
            values = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(values) > 5000:
                raise ValueError("密码字典最多 5000 行")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write("\n".join(values) + "\n")
                os.replace(tmp, path)
                os.chmod(path, 0o600)
                if os.name != "nt":
                    shutil.chown(path, user="autobook", group="autobook")
            finally:
                tmp.unlink(missing_ok=True)

    return AdminHandler


def serve_admin(settings: AdminSettings) -> None:
    if not settings.tls_cert.is_file() or not settings.tls_key.is_file():
        raise RuntimeError("管理面板必须配置有效的 ADMIN_TLS_CERT 和 ADMIN_TLS_KEY")
    server = ThreadingHTTPServer((settings.bind, settings.port), make_handler(settings))
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(settings.tls_cert), str(settings.tls_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    LOGGER.info("管理面板监听 https://%s:%d", settings.bind, settings.port)
    server.serve_forever(poll_interval=0.5)
