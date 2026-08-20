"""Readiness rules and live connectivity checks.

``readiness`` is cheap and drives the dashboard badges.  ``run_checks`` performs
real network calls so an operator can tell *which* credential or address is
wrong instead of reading a stack trace in journald.
"""
from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autobook_linux.panel import schema

USER_AGENT = "autobook-admin-panel/2.0"


@dataclass
class Issue:
    key: str
    message: str
    level: str = "error"     # error | warn
    hint: str = ""
    group: str = ""


@dataclass
class CheckResult:
    id: str
    title: str
    status: str              # ok | warn | fail | skip
    detail: str = ""
    hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "data": self.data,
        }


# --------------------------------------------------------------------- static


def readiness(role: str, values: dict[str, str]) -> list[Issue]:
    """Configuration problems that would stop the service from starting."""
    issues: list[Issue] = []

    def missing(key: str, hint: str = "") -> None:
        item = schema.FIELDS_BY_KEY[key]
        issues.append(Issue(key, f"未填写「{item.label}」", "error", hint or item.help, item.group))

    if role == "worker":
        for key in ("SITE_BASE_URL", "WORKER_TOKEN", "WORKER_ID", "BAIDU_GATEWAY_URL",
                    "BAIDU_GATEWAY_TOKEN", "DRIVE_BASE_URL", "DRIVE_EMAIL", "DRIVE_PASSWORD"):
            if not values.get(key):
                missing(key)
        site = values.get("SITE_BASE_URL", "")
        if site and not site.startswith(("http://", "https://")):
            issues.append(Issue("SITE_BASE_URL", "任务网站地址必须以 https:// 开头", "error", group="site"))
        gateway_url = values.get("BAIDU_GATEWAY_URL", "")
        if gateway_url and not gateway_url.startswith("https://"):
            issues.append(Issue("BAIDU_GATEWAY_URL", "网关地址必须使用 https://", "error", group="gateway_client"))
        ca_file = values.get("BAIDU_GATEWAY_CA_FILE", "")
        if ca_file and not Path(ca_file).is_file():
            issues.append(Issue("BAIDU_GATEWAY_CA_FILE", "指定的网关证书文件不存在", "error",
                                "网关用公有证书时应留空；自签名证书需先把 gateway.crt 复制到本机。", "gateway_client"))
        drive = values.get("DRIVE_BASE_URL", "")
        if drive and not drive.startswith("https://"):
            issues.append(Issue("DRIVE_BASE_URL", "结果网盘地址必须使用 https://", "error", group="drive"))
        for name, key in (("7z", "SEVEN_ZIP_BIN"), ("aria2c", "ARIA2C_BIN")):
            command = values.get(key) or name
            if not shutil.which(command):
                issues.append(Issue(key, f"系统里找不到命令 {command}", "error",
                                    "在「维护」页面执行依赖修复，或手工安装 p7zip-full 与 aria2。", "paths"))
    elif role == "gateway":
        if not values.get("BAIDU_GATEWAY_TOKEN"):
            missing("BAIDU_GATEWAY_TOKEN")
        cert = Path(values.get("GATEWAY_TLS_CERT", ""))
        key_file = Path(values.get("GATEWAY_TLS_KEY", ""))
        if not cert.is_file():
            issues.append(Issue("GATEWAY_TLS_CERT", "网关 TLS 证书不存在", "error",
                                "可在「维护」页面重新生成自签名证书。", "gateway_server"))
        if not key_file.is_file():
            issues.append(Issue("GATEWAY_TLS_KEY", "网关 TLS 私钥不存在", "error",
                                "可在「维护」页面重新生成自签名证书。", "gateway_server"))
        bduss, stoken = values.get("BAIDU_BDUSS", ""), values.get("BAIDU_STOKEN", "")
        auth_file = Path(values.get("BAIDU_AUTH_FILE", ""))
        if bool(bduss) != bool(stoken):
            issues.append(Issue("BAIDU_BDUSS", "BDUSS 与 STOKEN 必须同时填写", "error", group="baidu_account"))
        elif not bduss and not auth_file.is_file():
            issues.append(Issue("BAIDU_AUTH_FILE", "尚未完成百度网盘扫码登录", "error",
                                "打开「百度登录」页面扫码即可，无需手工填 Cookie。", "baidu_account"))
        if not values.get("BAIDU_GROUP_GID") and not values.get("BAIDU_GROUP_NAME"):
            issues.append(Issue("BAIDU_GROUP_GID", "群 GID 与群名称至少填一项", "error", group="baidu_account"))
    return issues


# ----------------------------------------------------------------- http utils


def _context(ca_file: str = "") -> ssl.SSLContext:
    if ca_file and Path(ca_file).is_file():
        context = ssl.create_default_context(cafile=ca_file)
        context.check_hostname = False   # self-signed gateway certs pin on the CA only
        return context
    return ssl.create_default_context()


def _http(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    ca_file: str = "",
    timeout: int = 20,
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_context(ca_file)) as response:
            return response.status, response.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000).decode("utf-8", "replace")


def _form(payload: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(payload).encode("utf-8")


# ------------------------------------------------------------- shared checks


def check_binaries(values: dict[str, str]) -> list[CheckResult]:
    results = []
    for label, command in (("解压工具 7-Zip", values.get("SEVEN_ZIP_BIN") or "7z"),
                           ("下载工具 aria2c", values.get("ARIA2C_BIN") or "aria2c")):
        path = shutil.which(command)
        results.append(
            CheckResult(
                f"bin.{command}",
                label,
                "ok" if path else "fail",
                f"已找到 {path}" if path else f"系统 PATH 里没有 {command}",
                "" if path else "在「维护」页面点击「修复系统依赖」自动安装。",
            )
        )
    return results


def check_disk(path: Path) -> CheckResult:
    try:
        usage = shutil.disk_usage(str(path if Path(path).exists() else "/"))
    except OSError as exc:
        return CheckResult("disk", "磁盘空间", "warn", f"无法读取磁盘信息: {exc}")
    free_gb = usage.free / (1024 ** 3)
    status = "ok" if free_gb >= 10 else ("warn" if free_gb >= 3 else "fail")
    return CheckResult(
        "disk",
        "磁盘空间",
        status,
        f"剩余 {free_gb:.1f} GB / 共 {usage.total / (1024 ** 3):.1f} GB",
        "" if status == "ok" else "转换大部头书籍需要临时空间，建议至少保留 10 GB。",
    )


# ------------------------------------------------------------- worker checks


def check_site(values: dict[str, str]) -> list[CheckResult]:
    base = (values.get("SITE_BASE_URL") or "").rstrip("/")
    if not base:
        return [CheckResult("site", "任务网站", "skip", "未配置任务网站地址")]
    results: list[CheckResult] = []
    try:
        status_code, _ = _http(base, timeout=20)
        reachable = status_code < 500
        results.append(
            CheckResult(
                "site.reach",
                "任务网站可达性",
                "ok" if reachable else "fail",
                f"HTTP {status_code}",
                "" if reachable else "网站返回服务端错误，请确认地址是否正确。",
            )
        )
    except Exception as exc:
        results.append(
            CheckResult("site.reach", "任务网站可达性", "fail", f"{type(exc).__name__}: {exc}",
                        "检查服务器出站网络、DNS 解析和网站地址拼写。")
        )
        return results

    # A heartbeat with an empty task token never mutates anything, but the site
    # answers differently for a wrong worker token, which verifies the secret.
    token = values.get("WORKER_TOKEN", "")
    if not token:
        results.append(CheckResult("site.token", "Worker 令牌", "skip", "未填写 Worker 令牌"))
        return results
    try:
        _, body = _http(
            f"{base}/index.php?doc_delivery-heartbeat-ajax-1",
            method="POST",
            data=_form({"worker_token": token, "worker_id": values.get("WORKER_ID", ""), "task_token": ""}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=25,
        )
        payload = json.loads(body) if body.strip().startswith("{") else {}
        message = str(payload.get("message", ""))
        if "worker token" in message.lower():
            results.append(CheckResult("site.token", "Worker 令牌", "fail", "网站拒绝了这个令牌",
                                       "到网站后台重新复制 Worker 令牌，注意首尾不要带空格。"))
        elif payload:
            results.append(CheckResult("site.token", "Worker 令牌", "ok", "令牌有效，网站已接受本机身份"))
        else:
            results.append(CheckResult("site.token", "Worker 令牌", "warn", "网站返回了非 JSON 内容",
                                       "确认任务网站地址指向的是安装了文献传递插件的站点。"))
    except Exception as exc:
        results.append(CheckResult("site.token", "Worker 令牌", "warn", f"{type(exc).__name__}: {exc}"))
    return results


def check_gateway_client(values: dict[str, str]) -> CheckResult:
    url = (values.get("BAIDU_GATEWAY_URL") or "").rstrip("/")
    token = values.get("BAIDU_GATEWAY_TOKEN", "")
    if not url:
        return CheckResult("gateway.client", "下载网关连接", "skip", "未配置网关地址")
    ca_file = values.get("BAIDU_GATEWAY_CA_FILE", "")
    try:
        status_code, body = _http(
            f"{url}/health",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            ca_file=ca_file,
            timeout=20,
        )
    except ssl.SSLError as exc:
        return CheckResult("gateway.client", "下载网关连接", "fail", f"TLS 校验失败: {exc}",
                           "自签名网关需要把 gateway.crt 复制到本机并填入「网关证书文件」。")
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return CheckResult("gateway.client", "下载网关连接", "fail", f"无法连接: {exc}",
                           "确认网关服务已启动，且网关机器防火墙放行了该端口。")
    if status_code == 401 or status_code == 403:
        return CheckResult("gateway.client", "下载网关连接", "fail", f"网关拒绝了共享令牌（HTTP {status_code}）",
                           "在网关机器的面板里复制「网关共享令牌」，两边必须完全一致。")
    if status_code != 200:
        return CheckResult("gateway.client", "下载网关连接", "fail", f"HTTP {status_code}: {body[:200]}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("gateway.client", "下载网关连接", "warn", "网关返回了无法解析的内容")
    jobs = payload.get("jobs", {})
    return CheckResult("gateway.client", "下载网关连接", "ok",
                       f"网关正常，当前任务 {jobs}", data=payload)


def check_drive(values: dict[str, str]) -> CheckResult:
    base = (values.get("DRIVE_BASE_URL") or "").rstrip("/")
    email = values.get("DRIVE_EMAIL", "")
    password = values.get("DRIVE_PASSWORD", "")
    if not (base and email and password):
        return CheckResult("drive", "结果网盘登录", "skip", "网盘地址、账号或密码未填写完整")
    try:
        status_code, body = _http(
            f"{base}/api/v4/session/token",
            method="POST",
            data=json.dumps({"email": email, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as exc:
        return CheckResult("drive", "结果网盘登录", "fail", f"无法连接: {exc}",
                           "确认网盘地址正确并且服务器可以访问该域名。")
    if status_code != 200:
        return CheckResult("drive", "结果网盘登录", "fail", f"HTTP {status_code}: {body[:200]}",
                           "多为账号或密码错误，请重新填写「网盘密码」。")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("drive", "结果网盘登录", "warn", "网盘返回了无法解析的内容")
    if payload.get("code") not in (0, None):
        return CheckResult("drive", "结果网盘登录", "fail", str(payload.get("msg") or payload)[:200],
                           "多为账号或密码错误，请重新填写「网盘密码」。")
    return CheckResult("drive", "结果网盘登录", "ok", "账号密码有效，可以上传成品")


def check_paths(values: dict[str, str], service_user: str = "autobook") -> CheckResult:
    """Check that the service account can write its working directories.

    This must never create the directories itself: the panel runs as root, so a
    directory it created would be root-owned and unusable by the service.
    """
    problems: list[str] = []
    pending: list[str] = []
    for key in ("WORK_ROOT", "DOWNLOAD_ROOT"):
        raw = values.get(key)
        if not raw:
            continue
        target = Path(raw)
        probe_path = target if target.is_dir() else target.parent
        if not probe_path.is_dir():
            problems.append(f"{raw}: 上级目录不存在")
            continue
        probe = subprocess.run(
            ["runuser", "--user", service_user, "--", "test", "-w", str(probe_path)],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode != 0:
            problems.append(f"{raw}: 服务账号 {service_user} 无写入权限")
        elif not target.is_dir():
            pending.append(raw)
    if problems:
        return CheckResult("paths", "工作目录权限", "fail", "；".join(problems),
                           "在「维护」页面点击「修复目录权限」即可自动纠正属主。")
    if pending:
        return CheckResult("paths", "工作目录权限", "ok",
                           "可写；" + "、".join(pending) + " 将在首次运行时自动创建")
    return CheckResult("paths", "工作目录权限", "ok", "工作目录与下载目录均可写")


# ------------------------------------------------------------ gateway checks


def check_gateway_tls(values: dict[str, str]) -> CheckResult:
    cert = Path(values.get("GATEWAY_TLS_CERT", ""))
    key_file = Path(values.get("GATEWAY_TLS_KEY", ""))
    if not cert.is_file() or not key_file.is_file():
        return CheckResult("gateway.tls", "网关证书", "fail", "证书或私钥文件不存在",
                           "在「维护」页面点击「重新生成网关证书」。")
    try:
        info = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-enddate", "-subject", "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        cert_mod = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-modulus"],
                                  capture_output=True, text=True, timeout=15).stdout.strip()
        key_mod = subprocess.run(["openssl", "rsa", "-in", str(key_file), "-noout", "-modulus"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as exc:
        return CheckResult("gateway.tls", "网关证书", "warn", f"无法读取证书: {exc}")
    if cert_mod and key_mod and cert_mod != key_mod:
        return CheckResult("gateway.tls", "网关证书", "fail", "证书与私钥不匹配",
                           "在「维护」页面重新生成网关证书，并把新的 gateway.crt 分发给所有 Worker。")
    return CheckResult("gateway.tls", "网关证书", "ok", info.replace("\n", " · ")[:300])


def check_gateway_local(values: dict[str, str]) -> CheckResult:
    port = values.get("GATEWAY_PORT") or "8765"
    token = values.get("BAIDU_GATEWAY_TOKEN", "")
    cert = values.get("GATEWAY_TLS_CERT", "")
    try:
        status_code, body = _http(
            f"https://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            ca_file=cert,
            timeout=12,
        )
    except Exception as exc:
        return CheckResult("gateway.local", "网关本机自检", "fail", f"本机端口无响应: {exc}",
                           "网关服务可能未启动，请在「服务」卡片点击启动后再试。")
    if status_code != 200:
        return CheckResult("gateway.local", "网关本机自检", "warn", f"HTTP {status_code}: {body[:160]}")
    return CheckResult("gateway.local", "网关本机自检", "ok", f"本机 {port} 端口的网关服务响应正常")


def check_baidu(values: dict[str, str]) -> list[CheckResult]:
    """Verify the stored Baidu session and that the target group is visible."""
    results: list[CheckResult] = []
    try:
        from autobook_linux.baidu_auth import BaiduCredentialStore, resolve_baidu_credentials
        from autobook_linux.baidu_pan import BaiduPanClient
    except Exception as exc:
        return [CheckResult("baidu.login", "百度登录态", "warn", f"无法加载百度模块: {exc}")]
    try:
        credentials = resolve_baidu_credentials(
            values.get("BAIDU_BDUSS", ""),
            values.get("BAIDU_STOKEN", ""),
            values.get("BAIDU_BAIDUID", ""),
            BaiduCredentialStore(Path(values.get("BAIDU_AUTH_FILE", ""))),
        )
    except Exception as exc:
        return [CheckResult("baidu.login", "百度登录态", "fail", str(exc),
                            "打开「百度登录」页面用百度网盘 App 扫码即可。")]
    client = BaiduPanClient(
        bduss=credentials.bduss,
        stoken=credentials.stoken,
        baiduid=credentials.baiduid,
        ptoken=credentials.ptoken,
        cookies=credentials.cookies,
        panweb=values.get("BAIDU_PANWEB", "1"),
    )
    try:
        client.check_login()
        results.append(CheckResult("baidu.login", "百度登录态", "ok", "Cookie 有效，账号已登录"))
    except Exception as exc:
        results.append(CheckResult("baidu.login", "百度登录态", "fail", str(exc),
                                   "登录态已过期，请重新扫码登录。"))
        return results
    gid = values.get("BAIDU_GROUP_GID", "")
    try:
        if not gid:
            gid = client.resolve_gid(values.get("BAIDU_GROUP_NAME", ""))
        page = next(client.iter_group_shares(gid), None)
        count = len(page[1]) if page else 0
        results.append(
            CheckResult("baidu.group", "目标群文件库", "ok" if count else "warn",
                        f"gid={gid}，首页可见 {count} 个文件",
                        "" if count else "群文件库为空，确认该账号已加入正确的群。",
                        {"gid": gid})
        )
    except Exception as exc:
        results.append(CheckResult("baidu.group", "目标群文件库", "fail", str(exc),
                                   "确认扫码账号已加入目标群，或直接填写群 GID。"))
    return results


# ----------------------------------------------------------------- entrypoint


def run_checks(role: str, values: dict[str, str], install_dir: Path) -> list[dict[str, Any]]:
    results: list[CheckResult] = []
    static_issues = readiness(role, values)
    results.append(
        CheckResult(
            "config",
            "配置完整性",
            "ok" if not static_issues else "fail",
            "所有必填项均已填写" if not static_issues else "；".join(issue.message for issue in static_issues),
            "" if not static_issues else "到「配置」页面补齐标红的项目。",
        )
    )
    results.append(check_disk(install_dir))
    if role == "worker":
        results.extend(check_binaries(values))
        results.extend(check_site(values))
        results.append(check_gateway_client(values))
        results.append(check_drive(values))
        results.append(check_paths(values))
    else:
        results.append(check_gateway_tls(values))
        results.extend(check_baidu(values))
        results.append(check_gateway_local(values))
    return [item.as_dict() for item in results]
