from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import requests

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.baidu_auth import (
    BaiduCredentialStore,
    BaiduCredentials,
    BaiduQrLogin,
    BaiduQrLoginError,
    resolve_baidu_credentials,
)


class _Response:
    def __init__(self, *, text: str = "", content: bytes = b"", payload=None) -> None:
        self.text = text
        self.content = content
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.responses.pop(0)


class CredentialStoreTests(unittest.TestCase):
    def test_round_trip_keeps_required_group_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            store = BaiduCredentialStore(path)
            store.save(
                BaiduCredentials(
                    bduss="bduss-value",
                    stoken="stoken-value",
                    baiduid="baiduid-value",
                    cookies={"EXTRA": "cookie"},
                    created_at=123,
                )
            )
            loaded = store.load()

        self.assertEqual(loaded.bduss, "bduss-value")
        self.assertEqual(loaded.stoken, "stoken-value")
        self.assertEqual(loaded.baiduid, "baiduid-value")
        self.assertEqual(loaded.cookies["EXTRA"], "cookie")

    def test_partial_environment_override_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BaiduCredentialStore(Path(tmp) / "missing.json")
            with self.assertRaisesRegex(BaiduQrLoginError, "必须同时设置"):
                resolve_baidu_credentials("only-bduss", "", "", store)

    def test_store_does_not_accept_oauth_token_in_place_of_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text(json.dumps({"access_token": "oauth-only"}), encoding="utf-8")
            with self.assertRaisesRegex(BaiduQrLoginError, "BDUSS/STOKEN"):
                BaiduCredentialStore(path).load()


class QrLoginTests(unittest.TestCase):
    def test_generate_parses_jsonp_and_saves_baidu_png(self) -> None:
        session = _Session(
            [
                _Response(text='tangram_guid_1({"imgurl":"/v2/api/qrcode?sign=abc123&lp=pc"})'),
                _Response(content=b"\x89PNG\r\n\x1a\nimage"),
            ]
        )
        login = BaiduQrLogin(session=session)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "login.png"
            challenge = login.generate(output)
            saved = output.read_bytes()

        self.assertEqual(challenge.sign, "abc123")
        self.assertEqual(saved, b"\x89PNG\r\n\x1a\nimage")
        self.assertIn("sign=abc123", challenge.image_url)
        self.assertIn("lp=mobile", challenge.image_url)

    def test_poll_distinguishes_waiting_scanned_and_confirmed(self) -> None:
        session = _Session(
            [
                _Response(payload={"channel_v": '{"status": 0}'}),
                _Response(payload={"channel_v": '{"status": 1}'}),
                _Response(payload={"channel_v": '{"status": 0, "v": "verify-code"}'}),
                _Response(payload={"errno": 1}),
            ]
        )
        login = BaiduQrLogin(session=session)

        self.assertEqual(login.poll("sign"), ("waiting", ""))
        self.assertEqual(login.poll("sign"), ("scanned", ""))
        self.assertEqual(login.poll("sign"), ("confirmed", "verify-code"))
        self.assertEqual(login.poll("sign"), ("waiting", ""))

    def test_confirm_extracts_cookie_jar_without_printing_secrets(self) -> None:
        session = _Session([_Response(), _Response()])
        session.cookies.set("BDUSS", "bduss-value", domain=".baidu.com")
        session.cookies.set("STOKEN", "stoken-value", domain=".baidu.com")
        session.cookies.set("BAIDUID", "baiduid-value", domain=".baidu.com")
        login = BaiduQrLogin(session=session)

        credentials = login._confirm("temporary-verify-code")

        self.assertEqual(credentials.bduss, "bduss-value")
        self.assertEqual(credentials.stoken, "stoken-value")
        self.assertEqual(credentials.baiduid, "baiduid-value")
        self.assertTrue(session.calls[0][1]["allow_redirects"])

    def test_jsonp_parser_rejects_invalid_payload(self) -> None:
        with self.assertRaisesRegex(BaiduQrLoginError, "无法解析"):
            BaiduQrLogin._parse_jsonp("not-json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
