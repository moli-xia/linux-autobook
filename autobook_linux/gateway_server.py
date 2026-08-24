"""Small authenticated HTTPS service that owns the sole Baidu login session."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import shutil
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from autobook_linux.baidu_auth import BaiduCredentialStore, resolve_baidu_credentials
from autobook_linux.baidu_pan import BaiduPanClient
from autobook_linux.config import Config
from autobook_linux.library_index import pick_best_file
from autobook_linux.lookup import Lookup, LookupError, validate

LOGGER = logging.getLogger(__name__)
_SSNO_RE = re.compile(r"^\d{8}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _safe_filename(value: str, ssno: str) -> str:
    name = re.sub(r"[/\\\x00-\x1f]+", "_", str(value)).strip(". ")
    return name[:240] or f"{ssno}.bin"


@dataclass
class GatewayJob:
    job_id: str
    request_id: str
    ssno: str
    kind: str = "ss"
    status: str = "pending"
    filename: str = ""
    artifact: Path | None = None
    size: int = 0
    sha256: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class GatewayManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.gateway_job_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._credentials = resolve_baidu_credentials(
            config.bduss,
            config.stoken,
            config.baiduid,
            BaiduCredentialStore(config.baidu_auth_file),
        )
        self._jobs: dict[str, GatewayJob] = {}
        self._requests: dict[str, str] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.gateway_concurrency,
            thread_name_prefix="baidu-gateway",
        )
        self._gid: str | None = config.baidu_group_gid or None

    def _client(self) -> BaiduPanClient:
        credentials = self._credentials
        return BaiduPanClient(
            bduss=credentials.bduss,
            stoken=credentials.stoken,
            baiduid=credentials.baiduid,
            ptoken=credentials.ptoken,
            cookies=credentials.cookies,
            panweb=self.config.panweb,
            download_ua=self.config.download_ua,
            aria2c_bin=self.config.aria2c_bin,
            aria2_split=self.config.aria2_split,
            aria2_max_connection=self.config.aria2_max_connection,
            download_timeout_seconds=self.config.download_timeout_seconds,
        )

    def preflight(self) -> None:
        client = self._client()
        client.check_login()
        if not self._gid:
            self._gid = client.resolve_gid(self.config.baidu_group_name)
        first_page = next(client.iter_group_shares(self._gid), None)
        if first_page is None:
            LOGGER.warning("目标群文件库为空 gid=%s", self._gid)
        self.root.mkdir(parents=True, exist_ok=True)
        LOGGER.info("百度下载网关预检成功 gid=%s", self._gid)

    def submit(self, ssno: str, request_id: str, kind: str = "ss") -> GatewayJob:
        lookup = validate(kind, ssno)
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id 格式无效")
        self.cleanup_expired()
        with self._lock:
            existing_id = self._requests.get(request_id)
            if existing_id and existing_id in self._jobs:
                existing = self._jobs[existing_id]
                if existing.ssno != lookup.value or existing.kind != lookup.kind:
                    raise ValueError("同一 request_id 不能用于不同的检索条件")
                existing.updated_at = time.time()
                return existing
            job_id = uuid.uuid4().hex
            job = GatewayJob(job_id=job_id, request_id=request_id,
                             ssno=lookup.value, kind=lookup.kind)
            self._jobs[job_id] = job
            self._requests[request_id] = job_id
            self._executor.submit(self._run_job, job_id)
            return job

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.updated_at = time.time()
        job_dir = (self.root / job_id).resolve()
        try:
            if job_dir.parent != self.root:
                raise RuntimeError("非法网关任务路径")
            job_dir.mkdir(parents=True, exist_ok=True)
            client = self._client()
            gid = self._gid
            if not gid:
                gid = client.resolve_gid(self.config.baidu_group_name)
                self._gid = gid
            candidates = client.search_group_files(gid, job.ssno)
            item = pick_best_file(candidates, job.ssno, job.kind)
            if item is None:
                raise RuntimeError(
                    f"群文件库中未找到 {Lookup(job.kind, job.ssno).label()} 对应的文件")
            remote_dir = f"{self.config.baidu_save_dir.rstrip('/')}/gateway/{job_id}"
            safe_name = _safe_filename(item.name, job.ssno)
            artifact = client.fetch_group_file(
                item,
                save_dir=remote_dir,
                target_dir=job_dir,
                filename=safe_name,
            )
            digest = hashlib.sha256()
            with artifact.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    return
                current.status = "ready"
                current.filename = safe_name
                current.artifact = artifact
                current.size = artifact.stat().st_size
                current.sha256 = digest.hexdigest()
                current.updated_at = time.time()
            LOGGER.info("网关任务完成 job=%s SS=%s file=%s", job_id, job.ssno, item.name)
        except Exception as exc:
            message = re.sub(r"https?://\S+", "[链接已隐藏]", str(exc))[:1000]
            LOGGER.exception("网关任务失败 job=%s SS=%s: %s", job_id, job.ssno, message)
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current.status = "failed"
                    current.error = message
                    current.updated_at = time.time()

    def get(self, job_id: str, touch: bool = True) -> GatewayJob | None:
        self.cleanup_expired()
        with self._lock:
            job = self._jobs.get(job_id)
            if job and touch:
                job.updated_at = time.time()
            return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in {"pending", "running"}:
                return False
            self._jobs.pop(job_id, None)
            if self._requests.get(job.request_id) == job_id:
                self._requests.pop(job.request_id, None)
        self._remove_job_dir(job_id)
        return True

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.config.gateway_cache_ttl_seconds
        expired: list[tuple[str, str]] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status in {"ready", "failed"} and job.updated_at < cutoff:
                    expired.append((job_id, job.request_id))
                    self._jobs.pop(job_id, None)
            for job_id, request_id in expired:
                if self._requests.get(request_id) == job_id:
                    self._requests.pop(request_id, None)
        for job_id, _ in expired:
            self._remove_job_dir(job_id)

    def _remove_job_dir(self, job_id: str) -> None:
        path = (self.root / job_id).resolve()
        if path.parent == self.root:
            shutil.rmtree(path, ignore_errors=True)

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = {"pending": 0, "running": 0, "ready": 0, "failed": 0}
            for job in self._jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
            return counts


def _public_job(job: GatewayJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "ssno": job.ssno,
        "kind": job.kind,
        "status": job.status,
        "filename": job.filename if job.status == "ready" else "",
        "size": job.size if job.status == "ready" else 0,
        "sha256": job.sha256 if job.status == "ready" else "",
        "error": job.error if job.status == "failed" else "",
    }


def make_handler(manager: GatewayManager, token: str) -> type[BaseHTTPRequestHandler]:
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "autobook-gateway/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("gateway http: " + fmt, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._send_json(401, {"error": "unauthorized"})
            return False

        def _job_path(self) -> tuple[str, bool] | None:
            match = re.fullmatch(r"/v1/jobs/([0-9a-f]{32})(/content)?", urlparse(self.path).path)
            if not match:
                return None
            return match.group(1), bool(match.group(2))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(200, {"status": "ok", "jobs": manager.stats()})
                return
            if not self._require_auth():
                return
            parsed = self._job_path()
            if not parsed:
                self._send_json(404, {"error": "not found"})
                return
            job_id, content = parsed
            job = manager.get(job_id)
            if not job:
                self._send_json(404, {"error": "job not found"})
                return
            if not content:
                self._send_json(200, _public_job(job))
                return
            if job.status != "ready" or not job.artifact or not job.artifact.is_file():
                self._send_json(409, {"error": "job is not ready"})
                return
            filename = Path(job.filename).name
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(job.size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with job.artifact.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            if urlparse(self.path).path != "/v1/fetch":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16 * 1024:
                    raise ValueError("请求体大小无效")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                job = manager.submit(
                    str(payload.get("ssno") or ""),
                    str(payload.get("request_id") or ""),
                    str(payload.get("kind") or "ss"),
                )
                self._send_json(202, _public_job(job))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                LOGGER.exception("提交网关任务失败")
                self._send_json(500, {"error": str(exc)[:500]})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = self._job_path()
            if not parsed or parsed[1]:
                self._send_json(404, {"error": "not found"})
                return
            removed = manager.delete(parsed[0])
            self._send_json(200 if removed else 409, {"deleted": removed})

    return GatewayHandler


def serve_gateway(config: Config, manager: GatewayManager) -> None:
    server = ThreadingHTTPServer(
        (config.gateway_bind, config.gateway_port),
        make_handler(manager, config.baidu_gateway_token),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.gateway_tls_cert), str(config.gateway_tls_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    LOGGER.info("百度下载网关监听 https://%s:%d", config.gateway_bind, config.gateway_port)
    server.serve_forever(poll_interval=0.5)
