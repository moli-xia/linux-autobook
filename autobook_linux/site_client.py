"""Client for the 544544.xyz document-delivery task API.

Protocol (verified against the site's le_doc_delivery plugin):
  POST {site}/index.php?doc_delivery-claim-ajax-1
       form: worker_token, worker_id, worker_queue(all|pdf|ocr)
       -> {kong_status:1, task:{id, token, book_title, ssno, keyword, queue_stage, ...}}
  POST {site}/index.php?doc_delivery-progress-ajax-1
       form: worker_token, task_token, message
  POST {site}/doc_delivery-complete-ajax-1 (same index.php query style)
       form: worker_token, task_token, worker_status(completed|failed),
             result_url, result_file, message, raw_output
"""
from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class SiteClient:
    def __init__(self, base_url: str, worker_token: str, worker_id: str, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_token = worker_token
        self.worker_id = worker_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "autobook-linux-worker/1.0"})
        self.timeout = timeout

    def _post(self, query: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = dict(body)
        payload["worker_token"] = self.worker_token
        resp = self.session.post(
            f"{self.base_url}/index.php?{query}",
            data=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def claim(self, worker_queue: str = "pdf") -> dict[str, Any] | None:
        """Claim one pending task; returns task dict or None when queue is empty."""
        resp = self._post(
            "doc_delivery-claim-ajax-1",
            {"worker_id": self.worker_id, "worker_queue": worker_queue},
        )
        if not resp.get("kong_status"):
            raise RuntimeError(f"claim 失败: {resp.get('message') or resp}")
        return resp.get("task")

    def progress(self, task_token: str, message: str) -> None:
        try:
            self._post(
                "doc_delivery-progress-ajax-1",
                {"task_token": task_token, "message": message[:600]},
            )
        except Exception as exc:  # progress is best-effort
            LOGGER.warning("progress 上报失败: %s", exc)

    def complete(
        self,
        task_token: str,
        worker_status: str,
        result_url: str = "",
        result_file: str = "",
        message: str = "",
        raw_output: str = "",
    ) -> dict[str, Any]:
        resp = self._post(
            "doc_delivery-complete-ajax-1",
            {
                "task_token": task_token,
                "worker_status": worker_status,
                "result_url": result_url,
                "result_file": result_file,
                "message": message,
                "raw_output": raw_output[-60000:],
            },
        )
        if not resp.get("kong_status"):
            raise RuntimeError(f"complete 失败: {resp.get('message') or resp}")
        return resp
