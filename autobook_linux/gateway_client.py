"""Authenticated HTTPS client for the central Baidu download gateway."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    pass


class BaiduGatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        ca_file: Path | None,
        timeout_seconds: int = 7200,
        poll_seconds: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        # An omitted CA file means the gateway uses a publicly trusted
        # certificate (for example Let's Encrypt) and requests should use the
        # operating-system trust store.  A path keeps certificate pinning for
        # self-signed/private deployments.
        self.verify: bool | str = str(ca_file) if ca_file else True
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": "autobook-linux-worker/2.0",
            }
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            verify=self.verify,
            timeout=60,
            **kwargs,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or response.text[:500]
            except ValueError:
                detail = response.text[:500]
            raise GatewayError(f"下载网关 HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError("下载网关返回了无效 JSON") from exc

    def check(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def fetch(self, ssno: str, request_id: str, target_dir: Path) -> tuple[Path, str]:
        """Submit/reuse a gateway job, wait for it, then atomically download it."""
        job = self._json("POST", "/v1/fetch", json={"ssno": ssno, "request_id": request_id})
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise GatewayError("下载网关没有返回 job_id")

        deadline = time.monotonic() + self.timeout_seconds
        filename = ""
        expected_size = 0
        expected_sha256 = ""
        while time.monotonic() < deadline:
            state = self._json("GET", f"/v1/jobs/{job_id}")
            status = state.get("status")
            if status == "failed":
                raise GatewayError(str(state.get("error") or "百度下载网关任务失败"))
            if status == "ready":
                filename = str(state.get("filename") or f"{ssno}.bin")
                expected_size = int(state.get("size") or 0)
                expected_sha256 = str(state.get("sha256") or "")
                break
            time.sleep(self.poll_seconds)
        else:
            raise GatewayError(f"百度下载网关等待超时: SS={ssno}")

        safe_name = Path(filename).name
        if safe_name in {"", ".", ".."}:
            safe_name = f"{ssno}.bin"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        partial = target.with_name(f"{target.name}.{job_id}.part")
        digest = hashlib.sha256()
        try:
            with self.session.get(
                f"{self.base_url}/v1/jobs/{job_id}/content",
                verify=self.verify,
                timeout=(60, self.timeout_seconds),
                stream=True,
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
                            digest.update(chunk)
            actual_size = partial.stat().st_size
            if expected_size and actual_size != expected_size:
                raise GatewayError(f"网关文件大小不符: 期望 {expected_size}, 实际 {actual_size}")
            if expected_sha256 and digest.hexdigest() != expected_sha256:
                raise GatewayError("网关文件 SHA-256 校验失败")
            partial.replace(target)
            return target, filename
        finally:
            partial.unlink(missing_ok=True)
            try:
                self._json("DELETE", f"/v1/jobs/{job_id}")
            except Exception as exc:
                LOGGER.warning("网关任务清理失败 job=%s: %s", job_id, exc)
