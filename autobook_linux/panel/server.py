"""HTTP layer: static single-page front-end plus a small JSON API."""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import ssl
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from autobook_linux.panel import (
    PANEL_VERSION, diagnostics, maintenance, passwords as password_dict, schema, services, sysinfo,
)
from autobook_linux.panel.auth import LoginLimiter, PasswordStore, SessionStore
from autobook_linux.panel.baidu import QrLoginManager
from autobook_linux.panel.envfile import read_env_file, write_env_file
from autobook_linux.panel.jobs import JobManager
from autobook_linux.panel.settings import PanelSettings, in_container, supervisor_backend

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "autobook_panel"
MAX_BODY = 1024 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class PanelError(Exception):
    """User-facing error carrying an HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def make_handler(settings: PanelSettings) -> type[BaseHTTPRequestHandler]:
    passwords = PasswordStore(settings.state_file)
    sessions = SessionStore(settings.session_seconds)
    limiter = LoginLimiter()
    qr_login = QrLoginManager(settings)
    jobs = JobManager()

    def load_values(target: str) -> dict[str, str]:
        return schema.apply_defaults(read_env_file(settings.env_path(target)), target)

    def role_values(role: str) -> dict[str, str]:
        """Effective values for a role, including keys shared with the other file."""
        values = load_values(role)
        other = "worker" if role == "gateway" else "gateway"
        for item in schema.FIELDS:
            if item.target == "both" and not values.get(item.key):
                shared = read_env_file(settings.env_path(other)).get(item.key, "")
                if shared:
                    values[item.key] = shared
        return values

    def role_issues(role: str) -> list[dict[str, str]]:
        return [
            {"key": issue.key, "message": issue.message, "level": issue.level,
             "hint": issue.hint, "group": issue.group}
            for issue in diagnostics.readiness(role, role_values(role))
        ]

    class Handler(BaseHTTPRequestHandler):
        server_version = f"autobook-panel/{PANEL_VERSION}"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- plumbing
        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("%s - " + fmt, self.client_address[0], *args)

        def _send(self, status: int, body: bytes, content_type: str, cookie: str = "", cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: dict[str, Any] | list[Any], status: int = 200, cookie: str = "") -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", cookie)

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise PanelError("请求体过大", 413)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PanelError("请求格式无效") from exc
            if not isinstance(payload, dict):
                raise PanelError("请求格式无效")
            return payload

        def _query(self) -> dict[str, str]:
            parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}

        def _token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""

        def _session(self) -> dict[str, Any] | None:
            return sessions.get(self._token())

        def _require(self) -> dict[str, Any]:
            session = self._session()
            if not session:
                raise PanelError("请先登录", 401)
            return session

        def _require_csrf(self, session: dict[str, Any]) -> None:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, str(session["csrf"])):
                raise PanelError("会话校验失败，请刷新页面后重试", 403)

        def _cookie(self, token: str, clear: bool = False) -> str:
            if clear:
                return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
            return (
                f"{SESSION_COOKIE}={token}; Path=/; Max-Age={settings.session_seconds}; "
                "Secure; HttpOnly; SameSite=Strict"
            )

        # ------------------------------------------------------------ routes
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self._json({"status": "ok", "version": PANEL_VERSION})
                    return
                if path.startswith("/api/"):
                    self._api_get(path)
                    return
                self._static(path)
            except PanelError as exc:
                self._error(str(exc), exc.status)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("GET %s 失败", path)
                self._error(f"服务器内部错误: {exc}", 500)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if not path.startswith("/api/"):
                    raise PanelError("未找到", 404)
                self._api_post(path)
            except PanelError as exc:
                self._error(str(exc), exc.status)
            except (ValueError, RuntimeError) as exc:
                # Domain errors already carry an operator-facing message.
                self._error(str(exc), 400)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("POST %s 失败", path)
                self._error(f"操作失败: {exc}", 500)

        # ------------------------------------------------------------ static
        def _static(self, path: str) -> None:
            name = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            if "/" in name or name.startswith("."):
                raise PanelError("未找到", 404)
            target = STATIC_DIR / name
            if not target.is_file():
                raise PanelError("未找到", 404)
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or name.endswith((".js", ".css", ".svg")):
                content_type += "; charset=utf-8"
            self._send(200, target.read_bytes(), content_type, cache="no-cache")

        # --------------------------------------------------------- API (GET)
        def _api_get(self, path: str) -> None:
            if path == "/api/session":
                session = self._session()
                self._json(
                    {
                        "authenticated": bool(session),
                        "username": session["username"] if session else "",
                        "csrf": session["csrf"] if session else "",
                        "must_change_password": passwords.must_change(),
                        "version": PANEL_VERSION,
                        "role": settings.current_role(),
                        "roles": settings.roles(),
                        "public_host": settings.public_host,
                        "panel_port": settings.port,
                        "container": in_container(),
                        "supervisor": supervisor_backend(),
                    }
                )
                return
            session = self._require()
            if path == "/api/overview":
                self._json(self._overview())
            elif path == "/api/config":
                self._json(self._config_payload())
            elif path == "/api/logs":
                query = self._query()
                name = query.get("service", "worker")
                if name not in services.SERVICES:
                    raise PanelError("未知服务")
                self._json(
                    {
                        "service": name,
                        "text": services.logs(
                            name,
                            int(query.get("lines", "200") or 200),
                            query.get("priority", ""),
                            query.get("grep", "")[:80],
                        ),
                    }
                )
            elif path == "/api/activity":
                self._json({"tasks": maintenance.worker_activity()})
            elif path == "/api/baidu/status":
                self._json(qr_login.status())
            elif path == "/api/baidu/qr.png":
                status = qr_login.status()
                if not status["has_qr"]:
                    raise PanelError("二维码尚未生成", 404)
                self._send(200, qr_login.read_qr(), "image/png")
            elif path == "/api/config/token":
                # Operators must be able to copy this secret from the gateway to
                # every worker; it is their own root-owned configuration.
                values = role_values("gateway" if settings.has_role("gateway") else "worker")
                self._json({"token": values.get("BAIDU_GATEWAY_TOKEN", "")})
            elif path == "/api/passwords":
                self._json(password_dict.snapshot(settings))
            elif path == "/api/jobs":
                self._json({"jobs": jobs.recent()})
            elif path.startswith("/api/jobs/"):
                job = jobs.get(path.rsplit("/", 1)[-1])
                if not job:
                    raise PanelError("任务不存在", 404)
                self._json(job.snapshot(int(self._query().get("offset", "0") or 0)))
            else:
                raise PanelError("未找到", 404)
            _ = session

        # -------------------------------------------------------- API (POST)
        def _api_post(self, path: str) -> None:
            if path == "/api/login":
                self._login()
                return
            session = self._require()
            self._require_csrf(session)
            payload = self._body()
            if path == "/api/logout":
                sessions.delete(self._token())
                self._json({"ok": True}, cookie=self._cookie("", clear=True))
            elif path == "/api/config":
                self._json(self._save_config(payload))
            elif path == "/api/config/token":
                token = maintenance.rotate_gateway_token(settings)
                self._json({"ok": True, "token": token,
                            "message": "已生成新的共享令牌，请同步到所有 Worker 并重启网关"})
            elif path == "/api/service":
                self._json(self._service(payload))
            elif path == "/api/check":
                role = str(payload.get("role", "")).strip()
                if role not in settings.roles():
                    raise PanelError("本机未安装该角色")
                started = time.time()
                results = diagnostics.run_checks(role, role_values(role), settings.install_dir)
                self._json({"role": role, "results": results, "elapsed": round(time.time() - started, 1)})
            elif path == "/api/baidu/start":
                if not settings.has_role("gateway"):
                    raise PanelError("本机未安装百度下载网关")
                qr_login.start()
                self._json({"ok": True})
            elif path == "/api/baidu/cancel":
                qr_login.cancel()
                self._json({"ok": True})
            elif path == "/api/passwords":
                self._json(self._passwords(payload))
            elif path == "/api/account":
                self._change_account(payload)
                sessions.clear()
                self._json({"ok": True, "message": "账号已更新，请使用新密码重新登录"},
                           cookie=self._cookie("", clear=True))
            elif path == "/api/maintenance":
                self._json(self._maintenance(payload))
            else:
                raise PanelError("未找到", 404)

        # ---------------------------------------------------------- handlers
        def _login(self) -> None:
            address = self.client_address[0]
            if not limiter.allowed(address):
                wait = limiter.remaining_seconds(address)
                raise PanelError(f"登录失败次数过多，请 {wait // 60 + 1} 分钟后再试", 429)
            payload = self._body()
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            if not passwords.authenticate(username, password):
                limiter.fail(address)
                time.sleep(0.4)
                raise PanelError("用户名或密码不正确", 401)
            limiter.clear(address)
            session = sessions.create(passwords.username(), address)
            self._json(
                {
                    "ok": True,
                    "username": session["username"],
                    "csrf": session["csrf"],
                    "must_change_password": passwords.must_change(),
                },
                cookie=self._cookie(str(session["token"])),
            )

        def _overview(self) -> dict[str, Any]:
            roles = settings.roles()
            service_names = [name for name, meta in services.SERVICES.items()
                             if meta["role"] in roles or name == "admin"]
            service_states = [services.status(name, roles) for name in service_names]
            issues = {role: role_issues(role) for role in roles}
            worker_values = load_values("worker") if "worker" in roles else {}
            system = sysinfo.snapshot(settings.install_dir)
            runtime_dirs = []
            if "worker" in roles:
                for key in ("WORK_ROOT", "DOWNLOAD_ROOT"):
                    if worker_values.get(key):
                        runtime_dirs.append(sysinfo.directory_usage(Path(worker_values[key])))
            return {
                "role": settings.current_role(),
                "roles": roles,
                "version": PANEL_VERSION,
                "public_host": settings.public_host,
                "services": service_states,
                "issues": issues,
                "ready": {role: not items for role, items in issues.items()},
                "system": system,
                "binaries": sysinfo.binaries(["7z", "aria2c", "openssl", "journalctl"]),
                "runtime_dirs": runtime_dirs,
                "baidu": qr_login.status() if "gateway" in roles else {},
                "jobs": jobs.recent(5),
                "must_change_password": passwords.must_change(),
                "sessions": sessions.count(),
            }

        def _config_payload(self) -> dict[str, Any]:
            roles = settings.roles()
            values: dict[str, str] = {}
            secrets_set: dict[str, bool] = {}
            for role in roles:
                for key, value in role_values(role).items():
                    if key in schema.SECRET_KEYS:
                        secrets_set[key] = bool(value)
                        continue
                    values[key] = value
            return {
                "schema": schema.public_schema(roles),
                "values": values,
                "secrets_set": secrets_set,
                "issues": {role: role_issues(role) for role in roles},
                "roles": roles,
            }

        def _save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
            supplied = payload.get("values")
            if not isinstance(supplied, dict):
                raise PanelError("缺少配置内容")
            roles = settings.roles()
            targets = {target: read_env_file(settings.env_path(target)) for target in roles}
            changed: list[str] = []
            for key, raw in supplied.items():
                item = schema.FIELDS_BY_KEY.get(key)
                if item is None:
                    continue
                value = str(raw).strip()
                if any(char in value for char in "\r\n\x00"):
                    raise PanelError(f"「{item.label}」不能包含换行或空字符")
                if item.kind == "password" and value == "":
                    continue    # blank means "keep the stored secret"
                if item.kind == "number" and value:
                    if not value.lstrip("-").isdigit():
                        raise PanelError(f"「{item.label}」必须是数字")
                    number = int(value)
                    if item.min_value is not None and number < item.min_value:
                        raise PanelError(f"「{item.label}」不能小于 {item.min_value}")
                    if item.max_value is not None and number > item.max_value:
                        raise PanelError(f"「{item.label}」不能大于 {item.max_value}")
                if item.kind == "url" and value and not value.startswith(("http://", "https://")):
                    raise PanelError(f"「{item.label}」必须以 http:// 或 https:// 开头")
                for target, values in targets.items():
                    if item.target in {target, "both"}:
                        if values.get(item.key) != value:
                            changed.append(item.key)
                        values[item.key] = value
            for target, values in targets.items():
                schema.apply_defaults(values, target)
                write_env_file(settings.env_path(target), values, schema.key_order(target))
            issues = {role: role_issues(role) for role in roles}
            affected = sorted({schema.FIELDS_BY_KEY[key].target for key in changed if key in schema.FIELDS_BY_KEY})
            return {
                "ok": True,
                "changed": sorted(set(changed)),
                "issues": issues,
                "restart_needed": [
                    name for name in roles
                    if services.status(name, roles)["running"]
                    and (name in affected or "both" in affected)
                ],
                "message": f"已保存 {len(set(changed))} 项修改" if changed else "配置无变化",
            }

        def _service(self, payload: dict[str, Any]) -> dict[str, Any]:
            name = str(payload.get("service", ""))
            action = str(payload.get("action", ""))
            force = bool(payload.get("force"))
            if name not in services.SERVICES:
                raise PanelError("未知服务")
            if name != "admin" and not settings.has_role(services.SERVICES[name]["role"]):
                raise PanelError("本机未安装该服务")
            if name == "admin" and action in {"stop", "disable"}:
                raise PanelError("不能从面板内部停用面板自身")
            if action in {"start", "restart", "enable"} and name in {"gateway", "worker"} and not force:
                pending = role_issues(name)
                if pending:
                    raise PanelError("配置尚未完成：" + "；".join(item["message"] for item in pending))
            if name == "admin" and action == "restart":
                maintenance.restart_panel()
                return {"ok": True, "message": "面板将在数秒后重启，请稍候刷新页面"}
            output = services.control(name, action)
            return {"ok": True, "message": f"{services.SERVICES[name]['label']} 操作成功", "output": output}

        def _passwords(self, payload: dict[str, Any]) -> dict[str, Any]:
            action = str(payload.get("action", "replace"))
            if action == "replace":
                content = payload.get("content")
                entries = (
                    [str(item) for item in content] if isinstance(content, list)
                    else str(content or "").splitlines()
                )
                count = password_dict.save(settings, entries)
                message = f"已保存 {count} 条候选密码"
            elif action == "add":
                count, message = password_dict.add(settings, str(payload.get("value", "")))
            elif action == "update":
                count, message = password_dict.update(
                    settings, str(payload.get("value", "")), str(payload.get("new_value", ""))
                )
            elif action == "delete":
                count, message = password_dict.remove(settings, str(payload.get("value", "")))
            elif action == "restore_defaults":
                count, message = password_dict.restore_defaults(settings)
            elif action == "merge_defaults":
                count, message = password_dict.merge_defaults(settings)
            else:
                raise PanelError("未知的密码字典操作")
            snapshot = password_dict.snapshot(settings)
            snapshot.update({"ok": True, "count": count, "message": message})
            return snapshot

        def _change_account(self, payload: dict[str, Any]) -> None:
            current = str(payload.get("current_password", ""))
            if not passwords.authenticate(passwords.username(), current):
                raise PanelError("当前密码不正确")
            username = str(payload.get("username", "") or passwords.username())
            new_password = str(payload.get("new_password", ""))
            passwords.set_credentials(username, new_password)

        def _maintenance(self, payload: dict[str, Any]) -> dict[str, Any]:
            action = str(payload.get("action", ""))
            if action == "dependencies":
                job = maintenance.fix_dependencies(jobs)
            elif action == "permissions":
                job = maintenance.fix_permissions(settings, jobs)
            elif action == "gateway_cert":
                if not settings.has_role("gateway"):
                    raise PanelError("本机未安装网关")
                host = settings.public_host or "localhost"
                job = maintenance.regenerate_gateway_cert(settings, jobs, host)
            elif action == "backup":
                job = maintenance.backup_config(settings, jobs)
            elif action == "update":
                job = maintenance.update_application(settings, jobs)
            elif action == "role":
                role = str(payload.get("role", ""))
                if in_container():
                    return {"ok": True, "message": maintenance.switch_role_in_container(settings, role)}
                job = maintenance.switch_role(settings, jobs, role)
            elif action == "restart_panel":
                maintenance.restart_panel()
                return {"ok": True, "message": "面板将在数秒后重启"}
            else:
                raise PanelError("未知的维护操作")
            return {"ok": True, "job_id": job.id, "title": job.title}

    return Handler


def serve(settings: PanelSettings) -> None:
    if not settings.tls_cert.is_file() or not settings.tls_key.is_file():
        raise RuntimeError("管理面板需要有效的 ADMIN_TLS_CERT 与 ADMIN_TLS_KEY")
    server = ThreadingHTTPServer((settings.bind, settings.port), make_handler(settings))
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(settings.tls_cert), str(settings.tls_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    LOGGER.info("管理面板 v%s 监听 https://%s:%d", PANEL_VERSION, settings.bind, settings.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("收到中断信号，管理面板退出")
    finally:
        server.server_close()


__all__ = ["serve", "make_handler", "PanelSettings", "HTTPStatus"]
