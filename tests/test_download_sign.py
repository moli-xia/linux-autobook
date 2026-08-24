"""Tests for the signed download link that replaces the rejected filemetas one."""
from __future__ import annotations

import base64
import unittest

from autobook_linux.baidu_pan import SIGN_CACHE_SECONDS, BaiduPanClient, calculate_download_sign


class SignAlgorithmTests(unittest.TestCase):
    def test_output_is_base64(self) -> None:
        signature = calculate_download_sign("a" * 40, "b" * 32)
        # Must decode cleanly and be the same length as the input it covers.
        self.assertEqual(len(base64.b64decode(signature)), 40)

    def test_signature_depends_on_both_inputs(self) -> None:
        base = calculate_download_sign("a" * 40, "b" * 32)
        self.assertNotEqual(base, calculate_download_sign("a" * 40, "c" * 32))
        self.assertNotEqual(base, calculate_download_sign("z" + "a" * 39, "b" * 32))

    def test_deterministic(self) -> None:
        self.assertEqual(
            calculate_download_sign("sign-one", "key-three"),
            calculate_download_sign("sign-one", "key-three"),
        )

    def test_empty_sign1_yields_empty_signature(self) -> None:
        self.assertEqual(calculate_download_sign("", "b" * 32), "")

    def test_non_ascii_key_does_not_crash(self) -> None:
        self.assertTrue(calculate_download_sign("abcd", "密钥密钥"))


class FakeClient(BaiduPanClient):
    """A client whose HTTP layer is replaced by scripted responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self._sign_cache = None
        self._bdstoken = "token"
        self._uk = "uk"

    def _get(self, path, params):  # type: ignore[override]
        self.calls.append((path, params))
        value = self.responses.get(path)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list):
            return value.pop(0)
        return value


SIGN_MATERIAL = {
    "errno": 0,
    "result": {"sign1": "s" * 40, "sign3": "k" * 32, "timestamp": "1787571034"},
}


class DownloadLinkTests(unittest.TestCase):
    def test_signed_endpoint_is_used(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": SIGN_MATERIAL,
            "/api/download": {"errno": 0, "dlink": [{"dlink": "https://d.pcs.baidu.com/signed"}]},
        })
        self.assertEqual(client.get_download_link(123), "https://d.pcs.baidu.com/signed")
        paths = [path for path, _ in client.calls]
        self.assertIn("/api/download", paths)
        self.assertNotIn("/api/filemetas", paths)

    def test_download_request_carries_signature_fields(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": SIGN_MATERIAL,
            "/api/download": {"errno": 0, "dlink": [{"dlink": "x"}]},
        })
        client.get_download_link(456)
        params = dict(client.calls[-1][1])
        self.assertEqual(params["fidlist"], "[456]")
        self.assertEqual(params["type"], "dlink")
        self.assertEqual(params["timestamp"], "1787571034")
        self.assertTrue(params["sign"])

    def test_material_is_cached_across_downloads(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": SIGN_MATERIAL,
            "/api/download": {"errno": 0, "dlink": [{"dlink": "x"}]},
        })
        client.get_download_link(1)
        client.get_download_link(2)
        material_calls = [p for p, _ in client.calls if p == "/api/gettemplatevariable"]
        self.assertEqual(len(material_calls), 1)
        self.assertGreater(SIGN_CACHE_SECONDS, 0)

    def test_falls_back_to_filemetas_when_signing_fails(self) -> None:
        # Baidu changing the signing endpoint must degrade, not break.
        client = FakeClient({
            "/api/gettemplatevariable": {"errno": -6},
            "/api/filemetas": {"errno": 0, "info": [{"dlink": "https://d.pcs.baidu.com/unsigned"}]},
        })
        self.assertEqual(client.get_download_link(7), "https://d.pcs.baidu.com/unsigned")
        self.assertIn("/api/filemetas", [path for path, _ in client.calls])

    def test_falls_back_when_download_endpoint_returns_no_link(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": SIGN_MATERIAL,
            "/api/download": {"errno": 0, "dlink": []},
            "/api/filemetas": {"errno": 0, "info": [{"dlink": "fallback"}]},
        })
        self.assertEqual(client.get_download_link(9), "fallback")

    def test_cache_is_dropped_after_a_signing_failure(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": SIGN_MATERIAL,
            "/api/download": {"errno": 0, "dlink": [{"dlink": "x"}]},
        })
        client.get_download_link(1)
        self.assertIsNotNone(client._sign_cache)
        client.responses["/api/download"] = RuntimeError("boom")
        client.responses["/api/filemetas"] = {"errno": 0, "info": [{"dlink": "fallback"}]}
        self.assertEqual(client.get_download_link(2), "fallback")
        self.assertIsNone(client._sign_cache)

    def test_both_paths_failing_raises(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": {"errno": -6},
            "/api/filemetas": {"errno": -9, "info": []},
        })
        with self.assertRaises(RuntimeError):
            client.get_download_link(11)

    def test_incomplete_material_is_rejected(self) -> None:
        client = FakeClient({
            "/api/gettemplatevariable": {"errno": 0, "result": {"sign1": "a", "sign3": ""}},
            "/api/filemetas": {"errno": 0, "info": [{"dlink": "fallback"}]},
        })
        self.assertEqual(client.get_download_link(3), "fallback")


if __name__ == "__main__":
    unittest.main()
