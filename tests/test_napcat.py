from __future__ import annotations

import unittest
from typing import Any

from tests.helpers import make_config
from webhook_to_napcat import napcat
from webhook_to_napcat.napcat import NapCatTarget


class NapCatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_post_json = napcat.post_json

    def tearDown(self) -> None:
        napcat.post_json = self.original_post_json

    def test_header_token_is_added_to_message_request(self) -> None:
        cfg = make_config(napcat_token="token-1", napcat_token_mode="header")

        url, headers, payload = napcat.build_napcat_request(cfg, NapCatTarget("private", 123))

        self.assertEqual(url, "http://127.0.0.1:3001/send_private_msg")
        self.assertEqual(headers, {"Authorization": "Bearer token-1"})
        self.assertEqual(payload, {"user_id": 123})

    def test_query_token_is_added_to_message_request(self) -> None:
        cfg = make_config(napcat_token="token 1", napcat_token_mode="query")

        url, headers, payload = napcat.build_napcat_request(cfg, NapCatTarget("group", 456))

        self.assertEqual(url, "http://127.0.0.1:3001/send_group_msg?access_token=token%201")
        self.assertEqual(headers, {})
        self.assertEqual(payload, {"group_id": 456})

    def test_file_upload_uses_shared_auth_request_builder(self) -> None:
        calls: list[dict[str, Any]] = []
        cfg = make_config(napcat_token="token/file", napcat_token_mode="query")

        def fake_post_json(url, payload, headers, timeout, retries):
            calls.append({"url": url, "payload": payload, "headers": headers})
            return {"retcode": 0}

        napcat.post_json = fake_post_json

        report = napcat.send_file(cfg, "/tmp/result.txt", "result.txt", [NapCatTarget("group", 456)])

        self.assertFalse(report.all_failed)
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:3001/upload_group_file?access_token=token/file")
        self.assertEqual(calls[0]["headers"], {})
        self.assertEqual(calls[0]["payload"], {"group_id": 456, "file": "file:///tmp/result.txt", "name": "result.txt"})


if __name__ == "__main__":
    unittest.main()
