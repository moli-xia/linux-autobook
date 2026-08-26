"""On-demand Wine/Docker fallback for PDG conversions the open decoder cannot finish."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import shutil
import socket
import stat
import struct
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Any
from urllib.parse import quote

import pikepdf

from autobook_linux.config import Config
LOGGER = logging.getLogger(__name__)


class PdgFallbackError(RuntimeError):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: int) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


class DockerEngine:
    """Small Docker Engine API client; avoids installing the Docker CLI."""

    api_prefix = "/v1.41"

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body or b""))}
        connection = _UnixHTTPConnection(self.socket_path, timeout)
        try:
            connection.request(method, f"{self.api_prefix}{path}", body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        if response.status not in expected:
            try:
                detail = str(json.loads(data.decode("utf-8")).get("message") or "")
            except Exception:
                detail = data.decode("utf-8", errors="replace")[:500]
            raise PdgFallbackError(f"Docker API HTTP {response.status}: {detail or response.reason}")
        return data

    def create(self, name: str, payload: dict[str, Any]) -> str:
        data = self._request("POST", f"/containers/create?name={quote(name)}", payload=payload, expected=(201,))
        container_id = str(json.loads(data.decode("utf-8")).get("Id") or "")
        if not container_id:
            raise PdgFallbackError("Docker API 创建容器后未返回容器 ID")
        return container_id

    def start(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/start", expected=(204,))

    def wait(self, container_id: str, timeout: int) -> int:
        data = self._request(
            "POST",
            f"/containers/{container_id}/wait?condition=not-running",
            timeout=timeout,
            expected=(200,),
        )
        return int(json.loads(data.decode("utf-8")).get("StatusCode", -1))

    def logs(self, container_id: str) -> str:
        data = self._request(
            "GET",
            f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=120",
            expected=(200,),
        )
        chunks: list[bytes] = []
        offset = 0
        while offset + 8 <= len(data):
            size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            offset += 8
            if offset + size > len(data):
                break
            chunks.append(data[offset:offset + size])
            offset += size
        if not chunks:
            chunks = [data]
        return b"".join(chunks).decode("utf-8", errors="replace")[-4000:]

    def remove(self, container_id: str) -> None:
        self._request("DELETE", f"/containers/{container_id}?force=1&v=1", expected=(204, 404))


def _safe_extract_zip(archive: Path, target: Path, max_unpacked_bytes: int) -> int:
    target.mkdir(parents=True, exist_ok=False)
    total = 0
    files = 0
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 20_000:
            raise PdgFallbackError("PDG 兜底包文件数量过多")
        for member in members:
            name = PurePosixPath(member.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise PdgFallbackError("PDG 兜底包包含不安全路径")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PdgFallbackError("PDG 兜底包不能包含符号链接")
            destination = target.joinpath(*name.parts)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            total += int(member.file_size)
            files += 1
            if total > max_unpacked_bytes:
                raise PdgFallbackError("PDG 兜底包解压后体积超限")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as reader, destination.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
    if files == 0 or not any(
        path.is_file() and not path.is_symlink() and path.suffix.lower() == ".pdg"
        for path in target.rglob("*")
    ):
        raise PdgFallbackError("PDG 兜底包内没有 PDG 页面")
    return files


def _pdg_page_count(target: Path) -> int:
    return sum(
        1
        for page in target.rglob("*")
        if page.is_file() and not page.is_symlink() and page.suffix.lower() == ".pdg"
    )


@dataclass(frozen=True)
class PdgFallbackResult:
    job_dir: Path
    pdf: Path
    size: int
    sha256: str
    pages: int


class PdgFallbackService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.enabled = config.pdg_fallback_enabled
        self.root = config.pdg_fallback_job_root.resolve()
        self.engine = DockerEngine(config.pdg_fallback_docker_socket)
        self._slot = threading.Semaphore(1)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "docker_socket": self.config.pdg_fallback_docker_socket.exists(),
            "image": self.config.pdg_fallback_image if self.enabled else "",
        }

    def _receive(self, stream: BinaryIO, length: int, target: Path, expected_sha256: str) -> str:
        digest = hashlib.sha256()
        remaining = length
        with target.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise PdgFallbackError("PDG 兜底上传未完整接收")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        actual = digest.hexdigest()
        if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.lower()):
            raise PdgFallbackError("PDG 兜底上传 SHA-256 校验失败")
        return actual

    def _container_payload(self, job_dir: Path, input_dir: Path, output: Path) -> dict[str, Any]:
        host_config: dict[str, Any] = {
            "AutoRemove": False,
            "NetworkMode": "none",
            "Memory": self.config.pdg_fallback_memory_mb * 1024 * 1024,
            "NanoCpus": self.config.pdg_fallback_cpus * 1_000_000_000,
            "PidsLimit": 256,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        }
        if self.config.pdg_fallback_runtime_volume:
            # Mount only the shared runtime volume. VolumesFrom would also
            # expose /etc/linux-autobook and its credentials to Wine.
            host_config["Binds"] = [
                f"{self.config.pdg_fallback_runtime_volume}:/opt/autobook-linux/runtime:rw"
            ]
            source_arg = str(input_dir)
            output_arg = str(output)
        else:
            host_config["Binds"] = [f"{job_dir}:/shared:rw"]
            source_arg = "/shared/input"
            output_arg = "/shared/output.pdf"
        return {
            "Image": self.config.pdg_fallback_image,
            "Cmd": [source_arg, output_arg],
            "Env": [f"PDG2PIC_TIMEOUT_SECONDS={self.config.pdg_fallback_timeout_seconds}"],
            "HostConfig": host_config,
            "Labels": {
                "xyz.544544.autobook.component": "pdg2pic-fallback",
                "xyz.544544.autobook.ephemeral": "true",
            },
        }

    def _run_container(self, job_dir: Path, input_dir: Path, output: Path) -> None:
        name = f"autobook-pdg2pic-{job_dir.name}"
        container_id = ""
        try:
            container_id = self.engine.create(name, self._container_payload(job_dir, input_dir, output))
            self.engine.start(container_id)
            exit_code = self.engine.wait(container_id, self.config.pdg_fallback_timeout_seconds + 120)
            if exit_code != 0:
                detail = self.engine.logs(container_id)
                raise PdgFallbackError(f"Pdg2Pic Wine 容器退出码 {exit_code}: {detail[-1200:]}")
        finally:
            if container_id:
                try:
                    self.engine.remove(container_id)
                except Exception as exc:
                    LOGGER.warning("清理 Pdg2Pic 临时容器失败 %s: %s", container_id[:12], exc)

    def convert(self, stream: BinaryIO, length: int, expected_sha256: str = "") -> PdgFallbackResult:
        if not self.enabled:
            raise PdgFallbackError("网关未启用 Pdg2Pic Wine 兜底")
        if not self.config.pdg_fallback_docker_socket.exists():
            raise PdgFallbackError("网关无法访问 Docker Engine socket")
        limit = self.config.pdg_fallback_max_upload_mb * 1024 * 1024
        if length <= 0 or length > limit:
            raise PdgFallbackError(f"PDG 兜底上传大小无效或超过 {self.config.pdg_fallback_max_upload_mb} MB")

        with self._slot:
            job_dir = (self.root / uuid.uuid4().hex).resolve()
            if job_dir.parent != self.root:
                raise PdgFallbackError("PDG 兜底任务路径无效")
            job_dir.mkdir(mode=0o700, parents=True)
            archive = job_dir / "input.zip"
            input_dir = job_dir / "input"
            output = job_dir / "output.pdf"
            try:
                self._receive(stream, length, archive, expected_sha256)
                file_count = _safe_extract_zip(archive, input_dir, limit * 8)
                pdg_pages = _pdg_page_count(input_dir)
                if pdg_pages == 0:
                    raise PdgFallbackError("PDG 兜底包内没有 PDG 页面")
                LOGGER.info(
                    "Pdg2Pic 兜底开始 job=%s files=%d pdg_pages=%d",
                    job_dir.name,
                    file_count,
                    pdg_pages,
                )
                self._run_container(job_dir, input_dir, output)
                if not output.is_file() or output.stat().st_size <= 8 or not output.read_bytes()[:5] == b"%PDF-":
                    raise PdgFallbackError("Pdg2Pic Wine 容器未生成有效 PDF")
                with pikepdf.open(output) as document:
                    pages = len(document.pages)
                if pages <= 0:
                    raise PdgFallbackError("Pdg2Pic Wine 输出 PDF 没有页面")
                if pages != pdg_pages:
                    raise PdgFallbackError(f"Pdg2Pic Wine 输出页数不完整: 期望 {pdg_pages}，实际 {pages}")
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                LOGGER.info("Pdg2Pic 兜底完成 job=%s pages=%d size=%d", job_dir.name, pages, output.stat().st_size)
                return PdgFallbackResult(job_dir, output, output.stat().st_size, digest, pages)
            except Exception:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise

    @staticmethod
    def cleanup(result: PdgFallbackResult) -> None:
        shutil.rmtree(result.job_dir, ignore_errors=True)
