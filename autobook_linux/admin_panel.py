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

    @classmethod
    def load(cls) -> "AdminSettings":
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
        )


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
        if field.target in {target, "both"} and field.default and not values.get(field.key):
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
    active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip()
    enabled = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=10).stdout.strip()
    return {"active": active or "unknown", "enabled": enabled or "unknown"}


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
:root{color-scheme:dark;--bg:#0b1020;--card:#151c31;--line:#2a3554;--text:#eef3ff;--muted:#9ba8c7;--accent:#6ea8fe;--ok:#4ade80;--bad:#fb7185;--warn:#fbbf24}*{box-sizing:border-box}body{margin:0;background:linear-gradient(140deg,#090d18,#111b35);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1180px;margin:auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}.brand h1{font-size:24px;margin:0}.brand p{margin:3px 0;color:var(--muted)}.card{background:rgba(21,28,49,.95);border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 14px 40px #0004}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}.field label{display:block;font-weight:650;margin-bottom:6px}.field small{display:block;color:var(--muted);min-height:20px}.field input,.field select,.field textarea{width:100%;background:#0c1325;color:var(--text);border:1px solid #35415f;border-radius:8px;padding:10px 11px}.field textarea{min-height:130px}button,.button{display:inline-block;border:0;border-radius:8px;padding:10px 15px;background:var(--accent);color:#07101f;font-weight:750;text-decoration:none;cursor:pointer}button.secondary,.button.secondary{background:#273451;color:var(--text)}button.danger{background:var(--bad)}.actions{display:flex;flex-wrap:wrap;gap:8px}.status{display:inline-flex;align-items:center;gap:7px;padding:5px 9px;border-radius:99px;background:#0c1325}.dot{width:9px;height:9px;border-radius:50%;background:var(--bad)}.active .dot{background:var(--ok)}.notice{padding:12px 14px;border-radius:9px;background:#33270a;border:1px solid #725b18;color:#ffe8a3}.success{background:#0e3020;border-color:#23633f;color:#b9f6cf}.error{background:#3a121a;border-color:#7b2939;color:#ffd0d8}.tabs{display:flex;gap:8px;flex-wrap:wrap}.section h2{margin-top:0}.secret-note{color:var(--warn)}pre{white-space:pre-wrap;word-break:break-word;background:#070b14;border-radius:8px;padding:14px;max-height:430px;overflow:auto}.login{max-width:430px;margin:9vh auto}.qr{max-width:320px;background:white;padding:12px;border-radius:10px}@media(max-width:600px){main{padding:14px}.top{align-items:flex-start;flex-direction:column}.card{padding:15px}}
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
                self._dashboard(session)
            elif path.startswith("/logs/"):
                name = path.rsplit("/", 1)[-1]
                if name not in MANAGED_SERVICES:
                    self._send(404, b"not found", "text/plain")
                    return
                logs = html.escape(service_logs(name))
                content = self._top(session) + f"<div class='card'><h2>{html.escape(name)} 日志</h2><pre>{logs}</pre><a class='button secondary' href='/'>返回</a></div>"
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
                    self._redirect("/?saved=1")
                elif path == "/service":
                    self._service_action(form)
                    self._redirect("/?service=1")
                elif path == "/password":
                    self._change_password(form)
                    sessions.clear()
                    self._redirect("/login", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
                elif path == "/baidu-login":
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
                self._send(400, page("操作失败", self._top(session) + f"<div class='card error'><h2>操作失败</h2><p>{html.escape(str(exc))}</p><a class='button secondary' href='/'>返回</a></div>"))

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

        def _top(self, session: dict[str, Any]) -> str:
            return f"<div class='top'><div class='brand'><h1>linux-autobook 管理面板</h1><p>当前用户：{html.escape(str(session['username']))}</p></div><form method='post' action='/logout'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><button class='secondary' type='submit'>退出登录</button></form></div>"

        def _dashboard(self, session: dict[str, Any]) -> None:
            query = parse_qs(urlparse(self.path).query)
            notices = []
            if passwords.must_change():
                notices.append("<div class='notice'>当前仍在使用默认密码 admin。请先在页面底部修改管理账号密码。</div>")
            if query.get("saved"):
                notices.append("<div class='notice success'>配置已原子保存。请重启相关服务使其生效。</div>")
            statuses = {name: service_status(name) for name in MANAGED_SERVICES}
            service_cards = []
            for name, state in statuses.items():
                active_class = "active" if state["active"] == "active" else ""
                buttons = "".join(
                    f"<button class='{('danger' if action == 'stop' else 'secondary')}' name='action' value='{action}'>{label}</button>"
                    for action, label in (("start", "启动"), ("restart", "重启"), ("stop", "停止"))
                )
                service_cards.append(f"<div class='card'><h2>{html.escape(name)}</h2><p class='status {active_class}'><span class='dot'></span>{html.escape(state['active'])} / {html.escape(state['enabled'])}</p><form class='actions' method='post' action='/service'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><input type='hidden' name='service' value='{name}'>{buttons}<a class='button secondary' href='/logs/{name}'>日志</a></form></div>")

            gateway_values = read_env_file(settings.gateway_env)
            worker_values = read_env_file(settings.worker_env)
            sections: dict[str, list[str]] = {}
            for field in CONFIG_FIELDS:
                source = gateway_values if field.target == "gateway" else worker_values
                if field.target == "both":
                    source = gateway_values if gateway_values.get(field.key) else worker_values
                current = source.get(field.key, field.default)
                help_text = field.help_text or ("敏感值留空表示保持不变。" if field.secret else "")
                if field.options:
                    options = "".join(f"<option value='{html.escape(option)}'{' selected' if option == current else ''}>{html.escape(option)}</option>" for option in field.options)
                    control = f"<select name='{field.key}'>{options}</select>"
                else:
                    value = "" if field.secret else current
                    input_type = "password" if field.secret else "text"
                    placeholder = "已设置；留空保持" if field.secret and current else ("未设置" if field.secret else "")
                    control = f"<input type='{input_type}' name='{field.key}' value='{html.escape(value, quote=True)}' placeholder='{html.escape(placeholder, quote=True)}' autocomplete='off'>"
                sections.setdefault(field.section, []).append(f"<div class='field'><label>{html.escape(field.label)}</label>{control}<small>{html.escape(help_text)}</small></div>")
            config_html = "".join(f"<div class='card section'><h2>{html.escape(section)}</h2><div class='grid'>{''.join(fields)}</div></div>" for section, fields in sections.items())

            qr_status, has_qr, qr_log = qr_login.snapshot()
            qr_image = "<p><img class='qr' src='/qr.png' alt='百度登录二维码'></p>" if has_qr else ""
            password_path = Path(worker_values.get("PASSWORD_DICT", str(PROJECT_ROOT / "password.txt")))
            try:
                password_count = sum(1 for line in password_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
            except FileNotFoundError:
                password_count = 0

            content = self._top(session) + "".join(notices)
            content += "<div class='grid'>" + "".join(service_cards) + "</div>"
            content += f"<form method='post' action='/save'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'>{config_html}<div class='card'><button type='submit'>保存全部配置</button></div></form>"
            content += f"<div class='card'><h2>百度扫码登录</h2><p>{html.escape(qr_status)}</p>{qr_image}<form method='post' action='/baidu-login'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><button type='submit'>生成新二维码</button></form><pre>{html.escape(qr_log)}</pre></div>"
            content += f"<div class='card'><h2>解压密码字典</h2><p>当前共 {password_count} 个非空候选。留空提交不会覆盖。</p><form method='post' action='/password-dict'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><div class='field'><textarea name='passwords' placeholder='每行一个密码；留空保持现有字典'></textarea></div><p><button type='submit'>替换密码字典</button></p></form></div>"
            content += f"<div class='card'><h2>修改管理账号</h2><form method='post' action='/password'><input type='hidden' name='csrf' value='{html.escape(str(session['csrf']))}'><div class='grid'><div class='field'><label>新用户名</label><input name='username' value='{html.escape(passwords.username(), quote=True)}' required></div><div class='field'><label>当前密码</label><input type='password' name='current_password' required></div><div class='field'><label>新密码（至少 10 位）</label><input type='password' name='new_password' required></div></div><p><button type='submit'>修改并退出登录</button></p></form></div>"
            self._send(200, page("linux-autobook 管理面板", content))

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
                if field.target in {"gateway", "both"}:
                    gateway[field.key] = value
                if field.target in {"worker", "both"}:
                    worker[field.key] = value
            apply_config_defaults(gateway, "gateway")
            apply_config_defaults(worker, "worker")
            write_env_file(settings.gateway_env, gateway, "gateway")
            write_env_file(settings.worker_env, worker, "worker")

        def _service_action(self, form: dict[str, str]) -> None:
            name, action = form.get("service", ""), form.get("action", "")
            if name not in MANAGED_SERVICES or action not in {"start", "stop", "restart"}:
                raise ValueError("不允许的服务操作")
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
