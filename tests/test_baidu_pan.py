from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.baidu_pan import BaiduPanClient


class _SearchClient(BaiduPanClient):
    def __init__(self) -> None:
        super().__init__("bduss", "stoken")
        self.params: dict = {}

    def _my_uk(self) -> str:
        return "2771790677"

    def _get(self, path: str, params: dict) -> dict:
        self.params = {"path": path, **params}
        return {
            "errno": 0,
            "result": [
                {
                    "groupId": "498636198303058255",
                    "msgId": "3534478550637766002",
                    "uk": "2224089590",
                    "fsid": "372831732256875",
                    "server_filename": "12607753_系统医学新视野.pdf",
                    "path": "%2Fduxiu%2F12607753_%E7%B3%BB%E7%BB%9F.pdf",
                    "size": "17584393",
                    "is_dir": "0",
                    "cTime": "1758174598002",
                    "dlink": "https://example.invalid/book.pdf",
                },
                {
                    "groupId": "some-other-group",
                    "fsid": "1",
                    "server_filename": "12607753_wrong.pdf",
                },
            ],
        }


class BaiduGroupSearchTests(unittest.TestCase):
    def test_signature_matches_desktop_client_vector(self) -> None:
        self.assertEqual(
            BaiduPanClient.group_search_sign("2771790677", "12607753"),
            "MjBhY2E5YmY4MWE4NzEzMDYzN2IxMTcwODZlNTA1Y2M=",
        )

    def test_search_maps_and_filters_results_by_gid(self) -> None:
        client = _SearchClient()
        rows = client.search_group_files("498636198303058255", "12607753")

        self.assertEqual(client.params["path"], "/basembox/group/multisearch")
        self.assertEqual(client.params["type"], 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "12607753_系统医学新视野.pdf")
        self.assertEqual(rows[0].path, "/duxiu/12607753_系统.pdf")
        self.assertEqual(rows[0].dlink, "https://example.invalid/book.pdf")


class _RangeResponse:
    def __init__(self, payload: bytes, start: int, end: int, total: int) -> None:
        self.status_code = 206
        self.payload = payload[start:end + 1]
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset:offset + chunk_size]


class _RangeSession:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requests: list[str] = []

    def get(self, _url: str, *, headers: dict, **_kwargs):
        range_header = headers["Range"]
        self.requests.append(range_header)
        start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
        return _RangeResponse(self.payload, int(start_text), int(end_text), len(self.payload))


class BaiduRangeDownloadTests(unittest.TestCase):
    def test_sequential_range_fallback_reassembles_exact_file(self) -> None:
        payload = b"0123456789"
        client = BaiduPanClient("bduss", "stoken")
        session = _RangeSession(payload)
        client.session = session

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "book.uvz"
            result = client._download_by_ranges(
                "https://example.invalid/book.uvz", target, len(payload), chunk_size=4
            )
            self.assertEqual(result.read_bytes(), payload)
            self.assertFalse(target.with_name("book.uvz.requests.part").exists())

        self.assertEqual(session.requests, ["bytes=0-3", "bytes=4-7", "bytes=8-9"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
