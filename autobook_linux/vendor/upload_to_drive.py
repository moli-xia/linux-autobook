#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# This helper lives under ``autobook_linux/vendor`` while the deployment
# environment file is stored at the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_utf8_stdio() -> None:
    """Keep redirected Windows worker output independent of the system code page."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


configure_utf8_stdio()


def load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
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


def read_legacy_defaults() -> dict[str, str]:
    legacy = Path(__file__).with_name("upload_drive_temp.py")
    if not legacy.exists():
        return {}
    text = legacy.read_text(encoding="utf-8", errors="ignore")
    defaults: dict[str, str] = {}
    m = re.search(r"EMAIL,\s*PASS,\s*BASE\s*=\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", text)
    if m:
        defaults["DRIVE_EMAIL"] = m.group(1)
        defaults["DRIVE_PASSWORD"] = m.group(2)
        defaults["DRIVE_BASE_URL"] = m.group(3)
    m = re.search(r"POLICY_ID,\s*TARGET_DIR\s*=\s*'([^']*)',\s*'([^']*)'", text)
    if m:
        defaults["DRIVE_POLICY_ID"] = m.group(1)
        defaults["DRIVE_TARGET_DIR"] = m.group(2)
    return defaults


def env_value(name: str, defaults: dict[str, str], fallback: str = "") -> str:
    return os.environ.get(name) or defaults.get(name) or fallback


def optional_env_value(name: str, defaults: dict[str, str], fallback: str = "") -> str:
    if name in os.environ:
        return os.environ.get(name, fallback)
    return defaults.get(name, fallback)


def env_bool(name: str, defaults: dict[str, str], fallback: bool) -> bool:
    raw = env_value(name, defaults, "1" if fallback else "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def safe_filename(value: str) -> str:
    value = value.strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120] or f"ebook_{int(time.time())}"


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def timestamp_year(value: Any) -> int | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", stripped):
            timestamp = float(stripped)
        else:
            normalized = stripped.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized).year
            except ValueError:
                match = re.search(r"(19|20)\d{2}", stripped)
                return int(match.group(0)) if match else None
    else:
        return None

    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).year
    except (OSError, OverflowError, ValueError):
        return None


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def dict_mentions_uploaded_file(item: dict[str, Any], uri: str, file_name: str) -> bool:
    expected = file_name.casefold()
    uri_folded = uri.casefold()
    for key in ("name", "filename", "file_name", "display_name"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.casefold() == expected:
            return True
    for key in ("uri", "path", "source", "src"):
        raw = item.get(key)
        if isinstance(raw, str):
            folded = raw.casefold()
            if folded == uri_folded or folded.endswith("/" + expected):
                return True
    return False


def extract_file_date_years(payload: Any, uri: str, file_name: str) -> list[int]:
    return extract_date_years(payload, uri, file_name, require_file_match=True)


def extract_date_years(payload: Any, uri: str, file_name: str, require_file_match: bool) -> list[int]:
    date_keys = {
        "created_at",
        "updated_at",
        "created",
        "updated",
        "ctime",
        "mtime",
        "created_time",
        "updated_time",
        "modified_time",
        "last_modified",
        "date",
    }
    years: list[int] = []
    for item in iter_dicts(payload):
        if require_file_match and not dict_mentions_uploaded_file(item, uri, file_name):
            continue
        for key, value in item.items():
            normalized_key = key.lower()
            if normalized_key in date_keys or "time" in normalized_key or "date" in normalized_key:
                year = timestamp_year(value)
                if year is not None:
                    years.append(year)
    return years


def cloudreve_parent_uri(uri: str) -> str:
    if "/" not in uri:
        return uri
    return uri.rsplit("/", 1)[0]


def fetch_upload_metadata(session: requests.Session, base: str, headers: dict[str, str], uri: str) -> list[tuple[str, Any]]:
    parent_uri = cloudreve_parent_uri(uri)
    endpoints = (
        ("/api/v4/file/info", {"uri": uri}),
        ("/api/v4/file", {"uri": uri}),
        ("/api/v4/file/list", {"uri": parent_uri}),
        ("/api/v4/directory", {"uri": parent_uri}),
        ("/api/v4/directory/list", {"uri": parent_uri}),
    )
    payloads: list[tuple[str, Any]] = []
    for endpoint, params in endpoints:
        try:
            response = session.get(f"{base}{endpoint}", headers=headers, params=params, timeout=60)
            if response.status_code >= 400:
                continue
            payloads.append((endpoint, response.json()))
        except Exception:
            continue
    return payloads


def verify_upload_date(payloads: list[tuple[str, Any]], uri: str, file_name: str) -> dict[str, Any]:
    valid: list[tuple[str, int]] = []
    invalid: list[tuple[str, int]] = []
    for source, payload in payloads:
        years = extract_file_date_years(payload, uri, file_name)
        if not years and source in {"/api/v4/file/info", "/api/v4/file"}:
            years = extract_date_years(payload, uri, file_name, require_file_match=False)
        for year in years:
            if year <= 1971:
                invalid.append((source, year))
            elif 2000 <= year <= 2100:
                valid.append((source, year))

    if invalid:
        source, year = invalid[0]
        raise RuntimeError(f"网盘返回的上传日期异常: {source} 显示年份 {year}，疑似 1970 时间戳问题。")
    if valid:
        source, year = valid[0]
        return {"upload_date_verified": True, "upload_date_year": year, "upload_date_source": source}
    return {"upload_date_verified": False, "upload_date_year": None, "upload_date_source": ""}


def upload_file(file_path: Path, title: str, target_dir: str, expire_days: int) -> dict[str, Any]:
    load_dotenv()
    defaults = read_legacy_defaults()

    email = env_value("DRIVE_EMAIL", defaults)
    password = env_value("DRIVE_PASSWORD", defaults)
    base = env_value("DRIVE_BASE_URL", defaults, "https://drive.netupdown.com").rstrip("/")
    policy_id = optional_env_value("DRIVE_POLICY_ID", defaults).strip()
    target_dir = target_dir or env_value("DRIVE_TARGET_DIR", defaults, "transfer")
    require_upload_date_verify = env_bool("DRIVE_REQUIRE_UPLOAD_DATE_VERIFY", defaults, True)

    if not email or not password:
        raise RuntimeError("缺少网盘配置，请在 .env 设置 DRIVE_EMAIL、DRIVE_PASSWORD。")
    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError(f"文件不存在: {file_path}")

    file_size = file_path.stat().st_size
    ts = int(time.time())
    last_modified_ms = int(time.time() * 1000)
    last_modified_year = timestamp_year(last_modified_ms)
    if last_modified_year is None or last_modified_year <= 1971:
        raise RuntimeError("本机时间异常，生成的上传时间戳会导致网盘显示 1970 年。")
    stem = safe_filename(title or file_path.stem)
    upload_file_name = f"{stem}.pdf"
    uri = f"cloudreve://my/{target_dir}/{upload_file_name}"

    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    resp = session.post(f"{base}/api/v4/session/token", json={"email": email, "password": password}, timeout=60)
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"网盘登录失败: {payload.get('msg') or payload}")

    token = payload["data"]["token"]["access_token"]
    json_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def request_credential(upload_uri: str, include_policy: bool) -> dict[str, Any]:
        payload = {
            "uri": upload_uri,
            "size": file_size,
            "last_modified": last_modified_ms,
            "mime_type": "application/pdf",
        }
        if include_policy and policy_id:
            payload["policy_id"] = policy_id
        return session.put(
            f"{base}/api/v4/file/upload",
            headers=json_headers,
            timeout=60,
            json=payload,
        ).json()

    cred = request_credential(uri, include_policy=bool(policy_id))
    if cred.get("code") != 0 and "unknown policy id" in str(cred.get("msg", "")).lower():
        cred = request_credential(uri, include_policy=False)
    if cred.get("code") != 0 and "Object existed" in str(cred.get("msg", "")):
        upload_file_name = f"{stem}_{ts}.pdf"
        uri = f"cloudreve://my/{target_dir}/{upload_file_name}"
        cred = request_credential(uri, include_policy=bool(policy_id))
        if cred.get("code") != 0 and "unknown policy id" in str(cred.get("msg", "")).lower():
            cred = request_credential(uri, include_policy=False)
    if cred.get("code") != 0:
        raise RuntimeError(f"获取上传凭证失败: {cred.get('msg') or cred}")

    session_id = cred["data"]["session_id"]
    chunk_size = int(cred["data"]["chunk_size"])
    total_chunks = max(1, math.ceil(file_size / chunk_size))
    upload_payloads: list[tuple[str, Any]] = [("upload_credential", cred)]

    with file_path.open("rb") as fh:
        for index in range(total_chunks):
            chunk = fh.read(chunk_size)
            upload_resp = session.post(
                f"{base}/api/v4/file/upload/{session_id}/{index}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(chunk)),
                },
                data=chunk,
                params={"session_id": session_id},
                timeout=1200,
            )
            try:
                chunk_payload = upload_resp.json()
            except Exception as exc:
                if upload_resp.status_code == 200:
                    continue
                raise RuntimeError(f"上传分片 {index + 1}/{total_chunks} 失败: HTTP {upload_resp.status_code}") from exc
            upload_payloads.append((f"upload_chunk_{index + 1}", chunk_payload))
            if chunk_payload.get("code") != 0:
                raise RuntimeError(f"上传分片 {index + 1}/{total_chunks} 失败: {chunk_payload.get('msg') or chunk_payload}")

    verification = {"upload_date_verified": False, "upload_date_year": None, "upload_date_source": ""}
    for attempt in range(5):
        metadata_payloads = fetch_upload_metadata(session, base, json_headers, uri)
        verification = verify_upload_date(upload_payloads + metadata_payloads, uri, upload_file_name)
        if verification["upload_date_verified"] or not require_upload_date_verify:
            break
        if attempt < 4:
            time.sleep(2)
    if require_upload_date_verify and not verification["upload_date_verified"]:
        raise RuntimeError(
            "已上传但无法从网盘接口确认文件上传日期。为避免 1970 年日期问题，"
            "请检查 drive.netupdown.com 文件列表或设置 DRIVE_REQUIRE_UPLOAD_DATE_VERIFY=0 临时跳过。"
        )

    share = session.put(
        f"{base}/api/v4/share",
        headers=json_headers,
        timeout=60,
        json={
            "uri": uri,
            "permissions": {"anonymous": "AQ==", "everyone": "AQ=="},
            "expire": max(1, expire_days) * 24 * 3600,
            "preview_enabled": True,
        },
    ).json()
    if share.get("code") != 0:
        raise RuntimeError(f"创建分享链接失败: {share.get('msg') or share}")

    share_url = str(share["data"])
    if share_url.startswith("/"):
        share_url = base + share_url
    return {
        "status": "completed",
        "share_url": share_url,
        "uri": uri,
        "file": str(file_path),
        "size": file_size,
        "expire_days": max(1, expire_days),
        "last_modified_ms": last_modified_ms,
        "storage_policy_id": str(cred.get("data", {}).get("storage_policy", {}).get("id", "")),
        "storage_policy_name": str(cred.get("data", {}).get("storage_policy", {}).get("name", "")),
        **verification,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload generated PDF to drive and create a share link.")
    parser.add_argument("--file", required=True, help="PDF 文件路径")
    parser.add_argument("--title", default="", help="分享文件名")
    parser.add_argument("--target-dir", default="", help="网盘目标目录，默认读取 DRIVE_TARGET_DIR 或 transfer")
    parser.add_argument("--expire-days", type=int, default=7, help="分享有效天数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    try:
        result = upload_file(Path(args.file), args.title, args.target_dir, args.expire_days)
        if args.json:
            emit_json(result)
        else:
            print(f"Share URL: {result['share_url']}")
        return 0
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
        if args.json:
            emit_json(payload)
        else:
            print(payload["message"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
