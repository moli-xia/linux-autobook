"""Baidu Passport QR login and local credential persistence.

The official OAuth flow used by projects such as bp3 returns an access token,
but the document-delivery worker also needs the web-only ``mbox`` group APIs.
Those APIs authenticate with Passport cookies, so this module implements the
same QR login used by Baidu's web client and stores the resulting cookies.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

import requests

PASSPORT_BASE = "https://passport.baidu.com"
PAN_BASE = "https://pan.baidu.com"
APP_TEMPLATE = "netdisk"
API_VERSION = "v3"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BaiduQrLoginError(RuntimeError):
    """Raised when the Passport QR login cannot be completed."""


@dataclass(frozen=True)
class BaiduCredentials:
    bduss: str
    stoken: str
    baiduid: str = ""
    ptoken: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    created_at: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BaiduCredentials":
        raw_cookies = value.get("cookies")
        cookies = {
            str(key): str(cookie_value)
            for key, cookie_value in (raw_cookies.items() if isinstance(raw_cookies, Mapping) else [])
            if key and cookie_value
        }
        credentials = cls(
            bduss=str(value.get("bduss") or cookies.get("BDUSS") or "").strip(),
            stoken=str(value.get("stoken") or cookies.get("STOKEN") or "").strip(),
            baiduid=str(value.get("baiduid") or cookies.get("BAIDUID") or "").strip(),
            ptoken=str(value.get("ptoken") or cookies.get("PTOKEN") or "").strip(),
            cookies=cookies,
            created_at=int(value.get("created_at") or 0),
        )
        credentials.require_group_access()
        return credentials

    def require_group_access(self) -> None:
        missing = []
        if not self.bduss:
            missing.append("BDUSS")
        if not self.stoken:
            missing.append("STOKEN")
        if missing:
            raise BaiduQrLoginError("百度登录凭据缺少 " + "/".join(missing))

    def cookie_dict(self) -> dict[str, str]:
        values = dict(self.cookies)
        values["BDUSS"] = self.bduss
        values["STOKEN"] = self.stoken
        if self.baiduid:
            values["BAIDUID"] = self.baiduid
        if self.ptoken:
            values["PTOKEN"] = self.ptoken
        values.setdefault("PANWEB", "1")
        return values

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "created_at": self.created_at or int(time.time()),
            "bduss": self.bduss,
            "stoken": self.stoken,
            "baiduid": self.baiduid,
            "ptoken": self.ptoken,
            "cookies": self.cookie_dict(),
        }


class BaiduCredentialStore:
    """Read/write a single-account cookie file with owner-only permissions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> BaiduCredentials:
        if not self.path.is_file():
            raise BaiduQrLoginError(
                f"百度登录凭据不存在: {self.path}；请先运行 run_worker.py --baidu-login"
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BaiduQrLoginError(f"无法读取百度登录凭据: {self.path}") from exc
        if not isinstance(payload, dict):
            raise BaiduQrLoginError(f"百度登录凭据格式无效: {self.path}")
        return BaiduCredentials.from_mapping(payload)

    def save(self, credentials: BaiduCredentials) -> None:
        credentials.require_group_access()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(credentials.to_mapping(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def resolve_baidu_credentials(
    bduss: str,
    stoken: str,
    baiduid: str,
    store: BaiduCredentialStore,
) -> BaiduCredentials:
    """Prefer an explicit environment cookie pair, otherwise use the store."""
    if bduss or stoken:
        if not (bduss and stoken):
            raise BaiduQrLoginError("BAIDU_BDUSS 与 BAIDU_STOKEN 必须同时设置")
        return BaiduCredentials(bduss=bduss, stoken=stoken, baiduid=baiduid)
    return store.load()


@dataclass(frozen=True)
class BaiduQrChallenge:
    sign: str
    image_url: str
    image_path: Path
    created_at: int


class BaiduQrLogin:
    """Perform Baidu web QR login without a browser or desktop client."""

    def __init__(
        self,
        proxy: str = "",
        session: requests.Session | None = None,
        request_timeout: int = 45,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.request_timeout = request_timeout

    def generate(self, image_path: Path) -> BaiduQrChallenge:
        timestamp = int(time.time() * 1000)
        callback = f"tangram_guid_{timestamp}"
        params = {
            "lp": "pc",
            "qrloginfrom": "pc",
            "gid": str(uuid.uuid4()),
            "callback": callback,
            "apiver": API_VERSION,
            "tt": timestamp,
            "tpl": APP_TEMPLATE,
            "_": timestamp,
        }
        response = self.session.get(
            f"{PASSPORT_BASE}/v2/api/getqrcode",
            params=params,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = self._parse_jsonp(response.text)
        image_url = str(payload.get("imgurl") or "")
        sign = self._extract_sign(image_url)
        image_url = (
            f"{PASSPORT_BASE}/v2/api/qrcode?sign={quote(sign)}"
            f"&lp=mobile&qrloginfrom=mobile&tpl={APP_TEMPLATE}"
        )

        image_response = self.session.get(image_url, timeout=self.request_timeout)
        image_response.raise_for_status()
        if not image_response.content:
            raise BaiduQrLoginError("百度返回了空二维码")
        image_path = Path(image_path).resolve()
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image_response.content)
        return BaiduQrChallenge(
            sign=sign,
            image_url=image_url,
            image_path=image_path,
            created_at=int(time.time()),
        )

    def wait_for_login(
        self,
        challenge: BaiduQrChallenge,
        timeout: int = 120,
        poll_interval: float = 2.0,
        status_callback: Callable[[str], None] | None = None,
    ) -> BaiduCredentials:
        deadline = time.monotonic() + timeout
        last_status = ""
        while time.monotonic() < deadline:
            status, value = self.poll(challenge.sign)
            if status != last_status and status_callback:
                status_callback(status)
                last_status = status
            if status == "confirmed":
                return self._confirm(value)
            if status == "expired":
                raise BaiduQrLoginError("百度登录二维码已过期，请重新运行扫码登录")
            if status == "failed":
                raise BaiduQrLoginError(value or "百度扫码登录失败")
            time.sleep(poll_interval)
        raise BaiduQrLoginError("等待百度扫码确认超时，请重新运行扫码登录")

    def poll(self, sign: str) -> tuple[str, str]:
        response = self.session.get(
            f"{PASSPORT_BASE}/channel/unicast",
            params={
                "channel_id": sign,
                "tpl": APP_TEMPLATE,
                "apiver": API_VERSION,
                "tt": int(time.time() * 1000),
            },
            # This endpoint deliberately long-polls for about 30 seconds when
            # the QR code has not been scanned yet.
            timeout=max(40, self.request_timeout),
        )
        response.raise_for_status()
        payload = response.json()
        channel_value = payload.get("channel_v") or {}
        if isinstance(channel_value, str):
            try:
                channel_value = json.loads(channel_value)
            except json.JSONDecodeError:
                channel_value = {}
        if not isinstance(channel_value, Mapping):
            channel_value = {}
        verify_code = str(channel_value.get("v") or "")
        status = int(channel_value.get("status") or 0)
        if verify_code:
            return "confirmed", verify_code
        if status == 1:
            return "scanned", ""
        if status in (-1, -2):
            return "expired", ""
        if status == 2:
            return "failed", "百度返回确认状态但未提供登录凭据"
        errno = int(payload.get("errno") or 0)
        # errno=1 is the normal long-poll timeout and means "still waiting".
        if errno and errno != 1:
            return "failed", str(payload.get("msg") or f"百度登录错误 errno={errno}")
        return "waiting", ""

    def _confirm(self, verify_code: str) -> BaiduCredentials:
        timestamp = int(time.time() * 1000)
        response = self.session.get(
            f"{PASSPORT_BASE}/v3/login/main/qrbdusslogin",
            params={
                "v": timestamp,
                "bduss": verify_code,
                "u": f"{PAN_BASE}/disk/main",
                "tpl": APP_TEMPLATE,
                "qrcode": 1,
                "apiver": API_VERSION,
                "tt": timestamp,
            },
            timeout=self.request_timeout,
            allow_redirects=True,
        )
        response.raise_for_status()

        # Visiting the web home page lets Passport finish propagating cookies
        # such as STOKEN before we persist the jar.
        warmup = self.session.get(f"{PAN_BASE}/disk/home", timeout=self.request_timeout)
        warmup.raise_for_status()
        cookies = self._cookie_mapping()
        credentials = BaiduCredentials(
            bduss=cookies.get("BDUSS", ""),
            stoken=cookies.get("STOKEN", ""),
            baiduid=cookies.get("BAIDUID", ""),
            ptoken=cookies.get("PTOKEN", ""),
            cookies=cookies,
            created_at=int(time.time()),
        )
        credentials.require_group_access()
        return credentials

    def _cookie_mapping(self) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for cookie in self.session.cookies:
            if cookie.value:
                cookies[cookie.name] = cookie.value
        return cookies

    @staticmethod
    def _parse_jsonp(text: str) -> dict[str, object]:
        match = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text.strip(), re.DOTALL)
        raw = match.group(1) if match else text.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BaiduQrLoginError("无法解析百度二维码响应") from exc
        if not isinstance(payload, dict):
            raise BaiduQrLoginError("百度二维码响应格式无效")
        return payload

    @staticmethod
    def _extract_sign(image_url: str) -> str:
        match = re.search(r"(?:[?&]|&amp;)sign=([^&]+)", image_url)
        if not match:
            raise BaiduQrLoginError("百度二维码响应中缺少 sign")
        return match.group(1)
