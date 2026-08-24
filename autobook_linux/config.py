"""Configuration loading for the Linux document-delivery worker.

All values come from environment variables; a local ``.env`` file next to the
project root is loaded first (without overriding real environment variables).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    env_file = path or PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    # --- task site ---
    site_base_url: str
    worker_token: str
    worker_id: str
    worker_queue: str          # pdf | ocr | all
    poll_seconds: int
    concurrency: int
    lease_heartbeat_seconds: int

    # --- central Baidu download gateway (worker side) ---
    baidu_gateway_url: str
    baidu_gateway_token: str
    baidu_gateway_ca_file: Path | None
    baidu_gateway_timeout_seconds: int
    baidu_gateway_poll_seconds: int

    # --- baidu netdisk web api ---
    bduss: str
    stoken: str
    baiduid: str
    panweb: str
    baidu_group_name: str
    baidu_group_gid: str       # optional: skip group-name resolution
    baidu_save_dir: str        # own-drive inbox dir used as transfer target
    baidu_auth_file: Path      # persisted cookies created by --baidu-login
    baidu_proxy: str           # optional HTTP/SOCKS proxy used for QR login
    baidu_qr_path: Path
    baidu_qr_timeout_seconds: int
    download_ua: str
    aria2_split: int
    aria2_max_connection: int
    download_timeout_seconds: int

    # --- local paths ---
    work_root: Path
    download_root: Path
    index_db: Path
    password_dict: Path
    seven_zip_bin: str
    aria2c_bin: str

    # --- result drive (Cloudreve) ---
    drive_email: str
    drive_password: str
    drive_base_url: str
    drive_policy_id: str
    drive_target_dir: str
    drive_expire_days: int
    drive_require_upload_date_verify: bool

    # --- automatic cleanup ---
    cleanup_enabled: bool
    cleanup_interval_hours: int
    drive_cleanup_grace_days: int
    baidu_inbox_orphan_hours: int

    # --- misc ---
    pdg_dpi: int
    full_sync_max_pages: int

    # --- gateway server ---
    gateway_bind: str
    gateway_port: int
    gateway_tls_cert: Path
    gateway_tls_key: Path
    gateway_concurrency: int
    gateway_cache_ttl_seconds: int
    gateway_job_root: Path

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        work_root = Path(_get("WORK_ROOT", str(PROJECT_ROOT / "runtime" / "work")))
        download_root = Path(_get("DOWNLOAD_ROOT", str(PROJECT_ROOT / "runtime" / "downloads")))
        cfg = cls(
            site_base_url=_get("SITE_BASE_URL", "https://544544.xyz").rstrip("/"),
            worker_token=_get("WORKER_TOKEN"),
            worker_id=_get("WORKER_ID", "linux-worker-1"),
            worker_queue=_get("WORKER_QUEUE", "pdf").lower() or "pdf",
            poll_seconds=_get_int("POLL_SECONDS", 15),
            concurrency=max(1, _get_int("CONCURRENCY", 3)),
            lease_heartbeat_seconds=max(10, _get_int("LEASE_HEARTBEAT_SECONDS", 60)),
            baidu_gateway_url=_get("BAIDU_GATEWAY_URL").rstrip("/"),
            baidu_gateway_token=_get("BAIDU_GATEWAY_TOKEN"),
            baidu_gateway_ca_file=(Path(_get("BAIDU_GATEWAY_CA_FILE")) if _get("BAIDU_GATEWAY_CA_FILE") else None),
            baidu_gateway_timeout_seconds=max(60, _get_int("BAIDU_GATEWAY_TIMEOUT_SECONDS", 7200)),
            baidu_gateway_poll_seconds=max(1, _get_int("BAIDU_GATEWAY_POLL_SECONDS", 3)),
            bduss=_get("BAIDU_BDUSS"),
            stoken=_get("BAIDU_STOKEN"),
            baiduid=_get("BAIDU_BAIDUID"),
            panweb=_get("BAIDU_PANWEB", "1"),
            baidu_group_name=_get("BAIDU_GROUP_NAME", "读秀12群"),
            baidu_group_gid=_get("BAIDU_GROUP_GID"),
            baidu_save_dir=_get("BAIDU_SAVE_DIR", "/autobook_inbox"),
            baidu_auth_file=Path(
                _get("BAIDU_AUTH_FILE", str(PROJECT_ROOT / "runtime" / "baidu_credentials.json"))
            ),
            baidu_proxy=_get("BAIDU_PROXY"),
            baidu_qr_path=Path(
                _get("BAIDU_QR_PATH", str(PROJECT_ROOT / "runtime" / "baidu-login-qr.png"))
            ),
            baidu_qr_timeout_seconds=max(30, _get_int("BAIDU_QR_TIMEOUT_SECONDS", 120)),
            download_ua=_get(
                "DOWNLOAD_UA",
                "netdisk;P2SP;3.0.20.56;netdisk;7.36.0.6;PC;PC-Windows;10.0.22621;WindowsBaiduYunGuanJia",
            ),
            aria2_split=_get_int("ARIA2_SPLIT", 16),
            aria2_max_connection=_get_int("ARIA2_MAX_CONNECTION", 16),
            download_timeout_seconds=_get_int("DOWNLOAD_TIMEOUT_SECONDS", 1800),
            work_root=work_root,
            download_root=download_root,
            index_db=Path(_get("INDEX_DB", str(PROJECT_ROOT / "runtime" / "library_index.sqlite3"))),
            password_dict=Path(_get("PASSWORD_DICT", str(PROJECT_ROOT / "password.txt"))),
            seven_zip_bin=_get("SEVEN_ZIP_BIN", "7z"),
            aria2c_bin=_get("ARIA2C_BIN", "aria2c"),
            drive_email=_get("DRIVE_EMAIL"),
            drive_password=_get("DRIVE_PASSWORD"),
            drive_base_url=_get("DRIVE_BASE_URL", "https://drive.netupdown.com").rstrip("/"),
            drive_policy_id=_get("DRIVE_POLICY_ID"),
            drive_target_dir=_get("DRIVE_TARGET_DIR", "transfer"),
            drive_expire_days=_get_int("DRIVE_EXPIRE_DAYS", 7),
            drive_require_upload_date_verify=_get("DRIVE_REQUIRE_UPLOAD_DATE_VERIFY", "1") not in {"0", "false", "no"},
            cleanup_enabled=_get("CLEANUP_ENABLED", "1") not in {"0", "false", "no"},
            cleanup_interval_hours=_get_int("CLEANUP_INTERVAL_HOURS", 6),
            drive_cleanup_grace_days=_get_int("DRIVE_CLEANUP_GRACE_DAYS", 1),
            baidu_inbox_orphan_hours=_get_int("BAIDU_INBOX_ORPHAN_HOURS", 6),
            pdg_dpi=_get_int("PDG_DPI", 200),
            full_sync_max_pages=_get_int("FULL_SYNC_MAX_PAGES", 2000),
            gateway_bind=_get("GATEWAY_BIND", "127.0.0.1"),
            gateway_port=max(1, _get_int("GATEWAY_PORT", 8765)),
            gateway_tls_cert=Path(_get("GATEWAY_TLS_CERT", str(PROJECT_ROOT / "runtime" / "gateway.crt"))),
            gateway_tls_key=Path(_get("GATEWAY_TLS_KEY", str(PROJECT_ROOT / "runtime" / "gateway.key"))),
            gateway_concurrency=max(1, _get_int("GATEWAY_CONCURRENCY", 3)),
            gateway_cache_ttl_seconds=max(60, _get_int("GATEWAY_CACHE_TTL_SECONDS", 3600)),
            gateway_job_root=Path(_get("GATEWAY_JOB_ROOT", str(PROJECT_ROOT / "runtime" / "gateway" / "jobs"))),
        )
        return cfg

    def validate(self) -> None:
        missing = []
        if not self.worker_token:
            missing.append("WORKER_TOKEN")
        if self.baidu_gateway_url:
            if not self.baidu_gateway_url.startswith("https://"):
                missing.append("BAIDU_GATEWAY_URL 必须使用 https://")
            if not self.baidu_gateway_token:
                missing.append("BAIDU_GATEWAY_TOKEN")
            if self.baidu_gateway_ca_file and not self.baidu_gateway_ca_file.is_file():
                missing.append("BAIDU_GATEWAY_CA_FILE（指定的网关证书不存在）")
        elif bool(self.bduss) != bool(self.stoken):
            missing.append("BAIDU_BDUSS 与 BAIDU_STOKEN 必须同时设置")
        elif not self.bduss and not self.baidu_auth_file.is_file():
            missing.append("百度登录凭据（请运行 --baidu-login）")
        if not self.drive_email:
            missing.append("DRIVE_EMAIL")
        if not self.drive_password:
            missing.append("DRIVE_PASSWORD")
        if missing:
            raise RuntimeError("缺少配置: " + ", ".join(missing))

    def validate_gateway(self) -> None:
        missing = []
        if not self.baidu_gateway_token:
            missing.append("BAIDU_GATEWAY_TOKEN")
        if not self.gateway_tls_cert.is_file():
            missing.append("GATEWAY_TLS_CERT")
        if not self.gateway_tls_key.is_file():
            missing.append("GATEWAY_TLS_KEY")
        if bool(self.bduss) != bool(self.stoken):
            missing.append("BAIDU_BDUSS 与 BAIDU_STOKEN 必须同时设置")
        elif not self.bduss and not self.baidu_auth_file.is_file():
            missing.append("百度登录凭据（请运行 --baidu-login）")
        if missing:
            raise RuntimeError("缺少网关配置: " + ", ".join(missing))
