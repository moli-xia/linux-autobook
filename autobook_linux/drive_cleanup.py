"""Delete delivered files from the result drive once their share has expired.

Every task uploads a PDF to Cloudreve and creates a share that expires after
``DRIVE_EXPIRE_DAYS``.  The share expiring does not remove the file, so the
drive grows without bound.  This sweeps the delivery directory and deletes what
nobody can reach any more.

Only the configured delivery directory is ever touched, and only entries older
than the share lifetime plus a grace period, so a link that is still live is
never broken.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 200
DELETE_BATCH = 50
REQUEST_TIMEOUT = 60
# Clocks and share creation are not perfectly aligned; keep a day of slack so a
# link that is still valid is never deleted early.
DEFAULT_GRACE_DAYS = 1


class DriveCleanupError(RuntimeError):
    pass


@dataclass
class CleanupResult:
    scanned: int = 0
    expired: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    dry_run: bool = True
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        action = "可清理" if self.dry_run else "已删除"
        return (f"扫描 {self.scanned} 个文件，过期 {self.expired} 个，"
                f"{action} {self.deleted} 个，释放 {self.freed_bytes / 1024 / 1024:.1f} MB")


def parse_timestamp(value: str) -> datetime | None:
    """Cloudreve returns RFC3339 with an offset, e.g. 2026-08-24T20:27:02+08:00."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def file_age_reference(entry: dict[str, Any]) -> datetime | None:
    """When the share clock started for this file.

    ``created_at`` is when it landed in the drive, which is when the task
    created its share; ``updated_at`` can be older (it carries the source
    file's modification time), so it must not be used.
    """
    return parse_timestamp(entry.get("created_at", ""))


class DriveCleaner:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        target_dir: str,
        expire_days: int,
        grace_days: int = DEFAULT_GRACE_DAYS,
        session: requests.Session | None = None,
    ) -> None:
        self.base = (base_url or "").rstrip("/")
        self.email = email
        self.password = password
        self.target_dir = (target_dir or "").strip("/")
        self.expire_days = max(1, int(expire_days))
        self.grace_days = max(0, int(grace_days))
        self.session = session or requests.Session()
        self._token = ""

    # ------------------------------------------------------------------
    @property
    def uri(self) -> str:
        return f"cloudreve://my/{self.target_dir}" if self.target_dir else "cloudreve://my"

    def login(self) -> None:
        if not (self.base and self.email and self.password):
            raise DriveCleanupError("结果网盘地址、账号或密码未配置")
        response = self.session.post(
            f"{self.base}/api/v4/session/token",
            json={"email": self.email, "password": self.password},
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise DriveCleanupError(f"结果网盘登录失败: {payload.get('msg') or payload}")
        token = (payload.get("data") or {}).get("token")
        access = token.get("access_token") if isinstance(token, dict) else token
        if not access:
            raise DriveCleanupError("结果网盘未返回访问令牌")
        self._token = str(access)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    def iter_files(self) -> Iterator[dict[str, Any]]:
        """Page through the delivery directory, yielding file entries only."""
        token = ""
        seen_pages = 0
        while True:
            params: dict[str, Any] = {"uri": self.uri, "page_size": PAGE_SIZE}
            if token:
                params["next_page_token"] = token
            response = self.session.get(
                f"{self.base}/api/v4/file", headers=self.headers,
                params=params, timeout=REQUEST_TIMEOUT,
            )
            payload = response.json()
            if payload.get("code") != 0:
                raise DriveCleanupError(f"列目录失败: {payload.get('msg') or payload}")
            data = payload.get("data") or {}
            for entry in data.get("files") or []:
                # type 1 is a directory in Cloudreve v4; never recurse or delete one.
                if int(entry.get("type", 0)) == 1:
                    continue
                yield entry
            pagination = data.get("pagination") or {}
            token = str(pagination.get("next_token") or "")
            seen_pages += 1
            if not token or seen_pages > 10_000:
                return

    def delete(self, uris: list[str]) -> None:
        if not uris:
            return
        response = self.session.request(
            "DELETE", f"{self.base}/api/v4/file", headers=self.headers,
            json={"uris": uris}, timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise DriveCleanupError(f"删除失败: {payload.get('msg') or payload}")

    # ------------------------------------------------------------------
    def run(self, dry_run: bool = True, limit: int = 0) -> CleanupResult:
        result = CleanupResult(dry_run=dry_run)
        self.login()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.expire_days + self.grace_days)
        LOGGER.info("清理结果网盘 %s，删除早于 %s 的文件", self.uri, cutoff.isoformat())

        batch: list[str] = []
        batch_bytes = 0
        for entry in self.iter_files():
            result.scanned += 1
            created = file_age_reference(entry)
            if created is None or created > cutoff:
                continue
            result.expired += 1
            name = str(entry.get("name") or "")
            path = str(entry.get("path") or f"{self.uri}/{name}")
            size = int(entry.get("size") or 0)
            if len(result.samples) < 5:
                result.samples.append(f"{name} ({created.date()})")
            if dry_run:
                result.deleted += 1
                result.freed_bytes += size
            else:
                batch.append(path)
                batch_bytes += size
                if len(batch) >= DELETE_BATCH:
                    self._flush(batch, batch_bytes, result)
                    batch, batch_bytes = [], 0
            if limit and result.expired >= limit:
                break

        if batch:
            self._flush(batch, batch_bytes, result)
        LOGGER.info("结果网盘清理完成: %s", result.summary())
        return result

    def _flush(self, batch: list[str], batch_bytes: int, result: CleanupResult) -> None:
        try:
            self.delete(batch)
        except DriveCleanupError as exc:
            # Another node may have deleted the same expired file already.
            result.errors.append(str(exc)[:200])
            LOGGER.warning("删除一批过期文件失败: %s", exc)
            return
        result.deleted += len(batch)
        result.freed_bytes += batch_bytes


def from_config(config, grace_days: int = DEFAULT_GRACE_DAYS) -> DriveCleaner:
    """Build a cleaner from the worker configuration."""
    return DriveCleaner(
        base_url=config.drive_base_url,
        email=config.drive_email,
        password=config.drive_password,
        target_dir=config.drive_target_dir,
        expire_days=config.drive_expire_days,
        grace_days=grace_days,
    )
