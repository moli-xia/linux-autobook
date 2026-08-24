"""Baidu Netdisk web-API client used to replace the Windows GUI automation.

Replaces the following desktop-client steps:
  消息 -> 会话 -> 读秀12群 -> 文件库 -> 按 SS 号搜索 -> 下载

Endpoints (web channel, verified against public reverse-engineered references,
e.g. PeterDing/BaiduPCS-Py issue #73 and common mbox tooling):
  GET  /disk/home                                -> bdstoken
  GET  /mbox/msg/historysession                  -> joined groups (gid, name)
  GET  /mbox/group/listshare                     -> group library share messages
  GET  /mbox/msg/shareinfo                       -> files inside one share message
  POST /mbox/msg/transfer                        -> save share files to own drive
  GET  /api/list                                 -> list own drive directory
  GET  /api/gettemplatevariable                  -> download signature material
  GET  /api/download?sign=..&timestamp=..        -> signed dlink for own files
  GET  /api/filemetas?dlink=1                    -> unsigned dlink (fallback only)
  POST /api/filemanager?opera=delete             -> remove temp files
  POST /api/sharedownload                        -> direct group-file dlink (fallback)

Authentication is cookie based (BDUSS + STOKEN). Downloads use aria2c with a
netdisk User-Agent for full speed on (S)VIP accounts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, unquote

import requests

LOGGER = logging.getLogger(__name__)

# The signature material is stable for a while; refetching it per download
# would double the request count for no benefit.
SIGN_CACHE_SECONDS = 300
# A transfer + download rarely exceeds minutes; anything this old in the inbox
# is a leftover, not a file some running task still needs.
INBOX_ORPHAN_HOURS = 6
INBOX_DELETE_BATCH = 50


def calculate_download_sign(sign1: str, sign3: str) -> str:
    """Sign a download request the way the pan.baidu.com web client does.

    An RC4-style keystream derived from sign3 is XORed over sign1 and the
    result is base64-encoded.  Without it the CDN rejects the link with
    error_code 31362 "sign error".
    """
    box = list(range(256))
    key = [ord(sign3[index % len(sign3)]) for index in range(256)] if sign3 else [0] * 256
    swap = 0
    for index in range(256):
        swap = (swap + box[index] + key[index]) % 256
        box[index], box[swap] = box[swap], box[index]
    out = []
    i = swap = 0
    for char in sign1:
        i = (i + 1) % 256
        swap = (swap + box[i]) % 256
        box[i], box[swap] = box[swap], box[i]
        out.append(chr(ord(char) ^ box[(box[i] + box[swap]) % 256]))
    return base64.b64encode("".join(out).encode("latin-1")).decode("ascii")


WEB_PARAMS = {"channel": "chunlei", "web": 1, "app_id": 250528, "clienttype": 0}
GROUP_SEARCH_SALT = "D3BA5E6D3B16D9202E10DE5D662CFC15"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class GroupShareFile:
    gid: str
    msg_id: str
    from_uk: str
    fs_id: int
    name: str
    path: str
    size: int
    is_dir: bool
    server_mtime: int = 0
    dlink: str = ""


class BaiduPanClient:
    def __init__(
        self,
        bduss: str,
        stoken: str,
        baiduid: str = "",
        ptoken: str = "",
        cookies: dict[str, str] | None = None,
        panweb: str = "1",
        download_ua: str = "netdisk;P2SP;3.0.20.56",
        aria2c_bin: str = "aria2c",
        aria2_split: int = 16,
        aria2_max_connection: int = 16,
        download_timeout_seconds: int = 1800,
    ) -> None:
        self.download_ua = download_ua
        self.aria2c_bin = aria2c_bin
        self.aria2_split = aria2_split
        self.aria2_max_connection = aria2_max_connection
        self.download_timeout_seconds = download_timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BROWSER_UA})
        cookie_values = dict(cookies or {})
        cookie_values.update({"BDUSS": bduss, "STOKEN": stoken, "PANWEB": panweb})
        if baiduid:
            cookie_values["BAIDUID"] = baiduid
        if ptoken:
            cookie_values["PTOKEN"] = ptoken
        for key, value in cookie_values.items():
            if value:
                self.session.cookies.set(key, value, domain=".baidu.com")
        self._bdstoken: str | None = None
        self._uk: str | None = None
        self._sign_cache: tuple[float, tuple[str, str, str]] | None = None

    # ------------------------------------------------------------------
    # low level helpers
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        merged = {**WEB_PARAMS, **params}
        if self.bdstoken:
            merged.setdefault("bdstoken", self.bdstoken)
        resp = self.session.get(f"https://pan.baidu.com{path}", params=merged, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged_params = {**WEB_PARAMS, **(params or {})}
        if self.bdstoken:
            merged_params.setdefault("bdstoken", self.bdstoken)
        resp = self.session.post(
            f"https://pan.baidu.com{path}",
            params=merged_params,
            data=data,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    @property
    def bdstoken(self) -> str:
        if self._bdstoken is None:
            resp = self.session.get("https://pan.baidu.com/disk/home", timeout=60)
            resp.raise_for_status()
            match = re.search(r'"bdstoken":"([0-9a-z]+)"', resp.text)
            if not match:
                match = re.search(r"bdstoken[\"']?\s*[:=]\s*[\"']([0-9a-z]+)[\"']", resp.text)
            if not match:
                raise RuntimeError("无法获取 bdstoken（BDUSS 可能已失效）")
            self._bdstoken = match.group(1)
        return self._bdstoken

    def check_login(self) -> dict[str, Any]:
        """Return basic account info; raises when cookies are invalid."""
        data = self._get("/api/loginStatus", {"t": int(time.time() * 1000)})
        if data.get("errno") != 0:
            # fallback probe
            data = self._get("/api/quota", {"t": int(time.time() * 1000)})
        if data.get("errno") != 0:
            raise RuntimeError(f"百度网盘登录态无效: {data}")
        return data

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------
    def list_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        data = self._get("/mbox/msg/historysession", {"t": int(time.time() * 1000)})
        for record in data.get("records") or []:
            gid = record.get("gid")
            if gid:
                groups.append({"gid": str(gid), "name": record.get("name", ""), "uk": record.get("uk")})
        return groups

    def resolve_gid(self, group_name: str) -> str:
        groups = self.list_groups()
        for group in groups:
            if group["name"] == group_name:
                return group["gid"]
        for group in groups:  # tolerate surrounding whitespace / partial match
            if group_name in group["name"] or group["name"] in group_name:
                return group["gid"]
        names = "、".join(g["name"] for g in groups) or "<无群组>"
        raise RuntimeError(f"未找到群组「{group_name}」，当前账号加入的群组: {names}")

    @staticmethod
    def group_search_sign(vuk: str, keyword: str) -> str:
        """Return the signature used by the desktop client's group search."""
        source = f"{GROUP_SEARCH_SALT}_{vuk}{keyword}".encode("utf-8")
        digest_hex = hashlib.md5(source).hexdigest().encode("ascii")
        return base64.b64encode(digest_hex).decode("ascii")

    def search_group_files(self, gid: str, keyword: str) -> list[GroupShareFile]:
        """Search the server-side group library without crawling its folders.

        Baidu's public OpenAPI does not document this endpoint, but the desktop
        client uses it for the fast search box in a group file library.
        """
        keyword = str(keyword).strip()
        if not keyword:
            return []
        vuk = self._my_uk()
        data = self._get(
            "/basembox/group/multisearch",
            {
                "key_word": keyword,
                "type": 2,
                "sign": self.group_search_sign(vuk, keyword),
            },
        )
        if data.get("errno") != 0:
            raise RuntimeError(
                "群文件库服务端搜索失败: "
                f"errno={data.get('errno')} show_msg={data.get('show_msg', '')}"
            )

        matches: list[GroupShareFile] = []
        for record in data.get("result") or []:
            record_gid = str(record.get("groupId") or record.get("group_id") or "")
            name = str(record.get("server_filename") or record.get("displayName") or "")
            if record_gid != str(gid) or keyword not in name:
                continue
            try:
                matches.append(
                    GroupShareFile(
                        gid=record_gid,
                        msg_id=str(record.get("msgId") or record.get("msg_id") or ""),
                        from_uk=str(record.get("uk") or ""),
                        fs_id=int(record.get("fsid") or record.get("fs_id") or 0),
                        name=name,
                        path=unquote(str(record.get("path") or record.get("server_path") or "")),
                        size=int(record.get("size") or 0),
                        is_dir=bool(int(record.get("is_dir") or 0)),
                        server_mtime=int(
                            record.get("cTime")
                            or record.get("server_mtime")
                            or record.get("msg_ctime")
                            or 0
                        ),
                        dlink=str(record.get("dlink") or ""),
                    )
                )
            except (TypeError, ValueError):
                LOGGER.warning("忽略无法解析的群文件搜索结果: %r", record)
        return matches

    def iter_group_shares(self, gid: str, start_page: int = 1, limit: int = 50) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Yield (page, msg_list) from the group file library, newest first."""
        page = start_page
        while True:
            data = self._get(
                "/mbox/group/listshare",
                {"gid": gid, "limit": limit, "desc": 1, "type": 2, "page": page},
            )
            if data.get("errno") != 0:
                raise RuntimeError(f"listshare 失败 page={page}: errno={data.get('errno')}")
            records = data.get("records") or {}
            msg_list = records.get("msg_list") or []
            if not msg_list:
                return
            yield page, msg_list
            if not data.get("has_more"):
                return
            page += 1
            time.sleep(0.4)  # be gentle with QPS

    def shareinfo_files(self, gid: str, msg_id: str, from_uk: str, fs_id: int) -> list[dict[str, Any]]:
        """List all immediate children inside one group-library directory."""
        records: list[dict[str, Any]] = []
        for page_records in self.iter_shareinfo_pages(gid, msg_id, from_uk, fs_id):
            records.extend(page_records)
        return records

    def iter_shareinfo_pages(
        self,
        gid: str,
        msg_id: str,
        from_uk: str,
        fs_id: int,
        num: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield every page of immediate children for one shared directory."""
        page = 1
        while True:
            data = self._get(
                "/mbox/msg/shareinfo",
                {
                    "msg_id": msg_id,
                    "page": page,
                    "from_uk": from_uk,
                    "gid": gid,
                    "type": 2,
                    "fs_id": fs_id,
                    "num": num,
                },
            )
            if data.get("errno") != 0:
                raise RuntimeError(
                    f"shareinfo 失败 msg_id={msg_id} fs_id={fs_id} page={page}: "
                    f"errno={data.get('errno')}"
                )
            records = data.get("records") or []
            if not records:
                return
            yield records
            if not data.get("has_more"):
                return
            page += 1
            time.sleep(0.2)

    # ------------------------------------------------------------------
    # transfer group file -> own drive
    # ------------------------------------------------------------------
    def transfer_to_own_drive(self, item: GroupShareFile, save_dir: str) -> None:
        data = {
            "from_uk": item.from_uk,
            "msg_id": item.msg_id,
            "path": save_dir,
            "ondup": "newcopy",
            "async": 1,
            "type": 2,
            "gid": item.gid,
            "fs_ids": f"[{item.fs_id}]",
        }
        last_errno: Any = None
        for attempt in range(6):
            resp = self._post("/mbox/msg/transfer", data=data)
            errno = resp.get("errno")
            if errno == 0:
                return
            last_errno = errno
            # errno=4: QPS limit (often still succeeds) - verify via listing later
            LOGGER.warning("transfer errno=%s (attempt %d/6)", errno, attempt + 1)
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"转存失败 errno={last_errno}: {item.name}")

    def ensure_remote_dir(self, path: str) -> None:
        resp = self._post("/api/create", data={"path": path, "isdir": 1}, params={"a": "commit"})
        if resp.get("errno") not in (0, -8):  # -8: already exists (name taken)
            raise RuntimeError(f"创建网盘目录失败 {path}: errno={resp.get('errno')}")

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        data = self._get(
            "/api/list",
            {"order": "time", "desc": 1, "showempty": 0, "page": 1, "num": 1000, "dir": path},
        )
        if data.get("errno") == -9:
            return []
        if data.get("errno") != 0:
            raise RuntimeError(f"list 失败 {path}: errno={data.get('errno')}")
        return data.get("list") or []

    def wait_transferred_file(self, save_dir: str, name_prefix: str, timeout: int = 120) -> dict[str, Any]:
        """Wait until a file whose name starts with name_prefix appears in save_dir."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                entries = self.list_dir(save_dir)
            except RuntimeError:
                entries = []
            matches = [
                entry for entry in entries
                if not entry.get("isdir") and self._stem_of(entry.get("server_filename", "")).startswith(name_prefix)
            ]
            if matches:
                # Transfers use ondup=newcopy, so a concurrent worker's older
                # copy may sit here under the plain name while ours landed as
                # "name(1)".  Take the newest: it is the one we just created,
                # and it is the one our caller will delete afterwards.
                return max(matches, key=lambda entry: int(entry.get("server_mtime") or 0))
            time.sleep(2)
        raise TimeoutError(f"转存文件未在 {save_dir} 出现: {name_prefix}*")

    @staticmethod
    def _stem_of(filename: str) -> str:
        return filename.rsplit(".", 1)[0] if "." in filename else filename

    def _sign_material(self) -> tuple[str, str, str]:
        """Fetch (and briefly cache) the values the download signature needs."""
        now = time.time()
        if self._sign_cache and now - self._sign_cache[0] < SIGN_CACHE_SECONDS:
            return self._sign_cache[1]
        data = self._get(
            "/api/gettemplatevariable",
            {"fields": json.dumps(["sign1", "sign3", "timestamp"])},
        )
        if data.get("errno") != 0:
            raise RuntimeError(f"gettemplatevariable 失败: errno={data.get('errno')}")
        result = data.get("result") or {}
        material = (
            str(result.get("sign1") or ""),
            str(result.get("sign3") or ""),
            str(result.get("timestamp") or ""),
        )
        if not all(material):
            raise RuntimeError("gettemplatevariable 未返回完整的签名素材")
        self._sign_cache = (now, material)
        return material

    def get_download_link(self, fs_id: int) -> str:
        """Signed download link for a file in our own drive.

        The dlink that /api/filemetas returns is unsigned: the CDN answers it
        with HTTP 403 {"error_code":31362,"error_msg":"sign error"}.  The web
        client instead signs a request to /api/download, which is what this
        does.  filemetas is kept as a fallback in case the signing endpoint
        changes shape again.
        """
        try:
            sign1, sign3, timestamp = self._sign_material()
            data = self._get("/api/download", {
                "sign": calculate_download_sign(sign1, sign3),
                "timestamp": timestamp,
                "fidlist": f"[{fs_id}]",
                "type": "dlink",
                "web": 1,
                "app_id": 250528,
                "channel": "chunlei",
                "clienttype": 0,
            })
            if data.get("errno") == 0:
                entries = data.get("dlink") or []
                if entries and entries[0].get("dlink"):
                    return str(entries[0]["dlink"])
            LOGGER.warning("签名下载接口未返回直链 fs_id=%s errno=%s", fs_id, data.get("errno"))
        except Exception as exc:
            LOGGER.warning("获取签名直链失败 fs_id=%s: %s", fs_id, exc)
            self._sign_cache = None

        data = self._get("/api/filemetas", {"fsids": f"[{fs_id}]", "dlink": 1})
        if data.get("errno") != 0 or not data.get("info"):
            raise RuntimeError(f"filemetas 失败 fs_id={fs_id}: {data}")
        dlink = data["info"][0].get("dlink")
        if not dlink:
            raise RuntimeError(f"未取得下载链接 fs_id={fs_id}")
        return dlink

    def group_file_dlink(self, item: GroupShareFile) -> str:
        """Direct dlink for a group-library file (works for small files)."""
        extra = json.dumps({"type": "group", "gid": str(item.gid), "from_uk": str(item.from_uk)})
        data = {
            "encrypt": 0,
            "uk": self._my_uk(),
            "product": "mbox",
            "primaryid": item.msg_id,
            "fid_list": f"[{item.fs_id}]",
            "extra": extra,
        }
        resp = self._post("/api/sharedownload", data=data, params={"sign": "", "timestamp": ""})
        if resp.get("errno") != 0 or not resp.get("list"):
            raise RuntimeError(f"sharedownload 失败: errno={resp.get('errno')}")
        return resp["list"][0]["dlink"]

    def _my_uk(self) -> str:
        if self._uk:
            return self._uk
        data = self._get("/api/loginStatus", {"t": int(time.time() * 1000)})
        login_info = data.get("login_info") or {}
        uk = login_info.get("uk") or data.get("uk")
        if not uk:
            raise RuntimeError("无法获取当前账号 uk")
        self._uk = str(uk)
        return self._uk

    # ------------------------------------------------------------------
    # downloading
    # ------------------------------------------------------------------
    def _download_by_ranges(
        self,
        dlink: str,
        target: Path,
        expected_size: int,
        chunk_size: int = 4 * 1024 * 1024,
    ) -> Path:
        """Download sequential byte ranges when Baidu rejects a whole-file GET.

        Some Baidu CDN nodes return HTTP 403 for a full request (and even for
        aria2's large ranges) while accepting ranges below 8 MiB.  Four MiB
        chunks stay below that observed limit and make the fallback predictable.
        """
        if expected_size <= 0:
            raise RuntimeError("分段下载需要已知文件大小")

        partial = target.with_name(f"{target.name}.requests.part")
        partial.unlink(missing_ok=True)
        written = 0
        try:
            with partial.open("wb") as output:
                while written < expected_size:
                    end = min(written + chunk_size - 1, expected_size - 1)
                    last_error = ""
                    for attempt in range(1, 6):
                        try:
                            with self.session.get(
                                dlink,
                                headers={
                                    "User-Agent": self.download_ua,
                                    "Referer": "https://pan.baidu.com/",
                                    "Range": f"bytes={written}-{end}",
                                },
                                stream=True,
                                allow_redirects=True,
                                timeout=(30, 120),
                            ) as response:
                                if response.status_code != 206:
                                    raise RuntimeError(f"HTTP {response.status_code}")
                                content_range = response.headers.get("Content-Range", "")
                                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                                if not match:
                                    raise RuntimeError(f"Content-Range 无效: {content_range or '<空>'}")
                                actual_start, actual_end, total = map(int, match.groups())
                                if (actual_start, actual_end, total) != (written, end, expected_size):
                                    raise RuntimeError(
                                        "Content-Range 不匹配: "
                                        f"期望 bytes {written}-{end}/{expected_size}, 实际 {content_range}"
                                    )
                                before = output.tell()
                                for data in response.iter_content(chunk_size=1024 * 1024):
                                    if data:
                                        output.write(data)
                                received = output.tell() - before
                                if received != end - written + 1:
                                    raise RuntimeError(
                                        f"分段大小不符: 期望 {end - written + 1}, 实际 {received}"
                                    )
                            written = end + 1
                            break
                        except Exception as exc:
                            output.seek(written)
                            output.truncate()
                            last_error = str(exc)
                            if attempt < 5:
                                time.sleep(attempt)
                    else:
                        raise RuntimeError(
                            f"分段下载失败 bytes={written}-{end}，重试 5 次: {last_error}"
                        )

            if partial.stat().st_size != expected_size:
                raise RuntimeError(
                    f"分段下载大小不符: 期望 {expected_size}, 实际 {partial.stat().st_size}"
                )
            partial.replace(target)
            return target
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def download(self, dlink: str, target: Path, expected_size: int = 0) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        cookie = "; ".join(
            f"{k}={v}" for k, v in self.session.cookies.items() if k in {"BDUSS", "STOKEN", "BAIDUID", "PANWEB"}
        )
        command = [
            self.aria2c_bin,
            "-x", str(self.aria2_max_connection),
            "-s", str(self.aria2_split),
            "-k", "1M",
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=30",
            "--timeout=120",
            "--console-log-level=warn",
            "--summary-interval=10",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "-d", str(target.parent),
            "-o", target.name,
            "--header", f"User-Agent: {self.download_ua}",
            "--header", f"Cookie: {cookie}",
            dlink,
        ]
        LOGGER.info("aria2 下载: %s", target.name)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.download_timeout_seconds,
        )
        if result.returncode != 0:
            tail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-15:])
            tail = re.sub(r"https?://\S+", "[下载链接已隐藏]", tail)
            LOGGER.warning("aria2 下载失败 rc=%s，改用 4 MiB 顺序分段下载: %s", result.returncode, tail)
            target.with_name(f"{target.name}.aria2").unlink(missing_ok=True)
            return self._download_by_ranges(dlink, target, expected_size)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"下载后文件不存在或为空: {target}")
        if expected_size and abs(target.stat().st_size - expected_size) > 0:
            raise RuntimeError(
                f"下载大小不符: {target.name} 期望 {expected_size}, 实际 {target.stat().st_size}"
            )
        return target

    # ------------------------------------------------------------------
    # cleanup of own drive
    # ------------------------------------------------------------------
    def delete_own_files(self, paths: list[str]) -> None:
        if not paths:
            return
        try:
            self._post("/api/filemanager", params={"opera": "delete"}, data={"filelist": json.dumps(paths, ensure_ascii=False)})
        except Exception as exc:
            LOGGER.warning("删除网盘临时文件失败: %s", exc)

    def clear_dir(self, path: str) -> None:
        try:
            entries = self.list_dir(path)
        except RuntimeError:
            return
        targets = [entry["path"] for entry in entries]
        self.delete_own_files(targets)

    def sweep_inbox(
        self,
        save_dir: str,
        older_than_hours: int = INBOX_ORPHAN_HOURS,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete transfer leftovers that no running task can still be using.

        ``fetch_group_file`` deletes its own copy as it finishes, but a transfer
        that timed out, a duplicate left by a retried transfer, or a worker that
        died mid-task all strand a file here.  Anything older than a few hours
        cannot belong to a live task, so it is safe to remove.
        """
        cutoff = time.time() - max(1, int(older_than_hours)) * 3600
        report: dict[str, Any] = {
            "scanned": 0, "deleted": 0, "freed_bytes": 0,
            "dry_run": dry_run, "samples": [],
        }
        try:
            entries = self.list_dir(save_dir)
        except RuntimeError as exc:
            LOGGER.warning("扫描百度转存目录失败: %s", exc)
            report["error"] = str(exc)[:200]
            return report

        stale: list[str] = []
        for entry in entries:
            if entry.get("isdir"):
                continue
            report["scanned"] += 1
            mtime = int(entry.get("server_mtime") or 0)
            # A missing mtime means we cannot prove the file is old; leave it.
            if not mtime or mtime > cutoff:
                continue
            stale.append(entry["path"])
            report["freed_bytes"] += int(entry.get("size") or 0)
            if len(report["samples"]) < 5:
                report["samples"].append(str(entry.get("server_filename") or ""))

        report["deleted"] = len(stale)
        if stale and not dry_run:
            for start in range(0, len(stale), INBOX_DELETE_BATCH):
                self.delete_own_files(stale[start:start + INBOX_DELETE_BATCH])
        LOGGER.info(
            "百度转存目录清理: 扫描 %s 个，%s %s 个，释放 %.1f MB",
            report["scanned"], "可清理" if dry_run else "已删除",
            report["deleted"], report["freed_bytes"] / 1024 / 1024,
        )
        return report

    # ------------------------------------------------------------------
    # high level: fetch one group library file to local disk
    # ------------------------------------------------------------------
    def fetch_group_file(
        self,
        item: GroupShareFile,
        save_dir: str,
        target_dir: Path,
        filename: str | None = None,
    ) -> Path:
        """Transfer a group library file into own drive, download it, clean up."""
        local_name = filename or item.name
        target = target_dir / local_name

        # Server-side group search already returns the same short-lived dlink
        # used by the desktop client.  It avoids a transfer/list/delete cycle
        # and is therefore the preferred path for concurrent workers.
        if item.dlink:
            try:
                return self.download(item.dlink, target, expected_size=item.size)
            except Exception as exc:
                LOGGER.warning("群文件直链下载失败，回退到转存流程: %s", exc)

        self.ensure_remote_dir(save_dir)
        self.transfer_to_own_drive(item, save_dir)
        own_entry = self.wait_transferred_file(save_dir, self._stem_of(item.name))
        own_path = own_entry["path"]
        own_fs_id = int(own_entry["fs_id"])
        try:
            dlink = self.get_download_link(own_fs_id)
            expected = int(own_entry.get("size") or item.size or 0)
            return self.download(dlink, target, expected_size=expected)
        finally:
            self.delete_own_files([own_path])
