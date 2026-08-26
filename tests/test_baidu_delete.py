"""The delete call must report Baidu's verdict, not just the HTTP status."""
from __future__ import annotations

import logging
import unittest

from autobook_linux.baidu_pan import DELETE_ERRNO_HINTS, BaiduPanClient


class Recorder(BaiduPanClient):
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []

    def _post(self, path, data, params=None):
        self.calls.append((path, params, data))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class DeleteResultTests(unittest.TestCase):
    def test_errno_zero_is_success(self) -> None:
        client = Recorder({"errno": 0})
        self.assertTrue(client.delete_own_files(["/inbox/a.pdf"]))

    def test_a_200_with_a_bad_errno_is_a_failure(self) -> None:
        # errno=132: the account needs a security verification first.
        client = Recorder({"errno": 132})
        with self.assertLogs("autobook_linux.baidu_pan", level=logging.WARNING) as logs:
            self.assertFalse(client.delete_own_files(["/inbox/a.pdf"]))
        self.assertIn("132", "".join(logs.output))

    def test_the_security_check_gets_an_actionable_message(self) -> None:
        client = Recorder({"errno": 132})
        with self.assertLogs("autobook_linux.baidu_pan", level=logging.WARNING) as logs:
            client.delete_own_files(["/inbox/a.pdf"])
        self.assertIn("安全验证", "".join(logs.output))
        self.assertIn(132, DELETE_ERRNO_HINTS)

    def test_a_network_error_is_a_failure_not_a_crash(self) -> None:
        client = Recorder(RuntimeError("connection reset"))
        with self.assertLogs("autobook_linux.baidu_pan", level=logging.WARNING):
            self.assertFalse(client.delete_own_files(["/inbox/a.pdf"]))

    def test_an_empty_list_is_a_no_op(self) -> None:
        client = Recorder({"errno": 0})
        self.assertTrue(client.delete_own_files([]))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
