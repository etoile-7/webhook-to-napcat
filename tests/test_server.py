from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from tests.helpers import make_config
from webhook_to_napcat import server
from webhook_to_napcat.internal import HandlerResult


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_dispatch = server.dispatch_notification
        self.calls: list[dict[str, Any]] = []

    def tearDown(self) -> None:
        server.dispatch_notification = self.original_dispatch

    def run_server(self, cfg):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.WebhookHandler)
        httpd.cfg = cfg  # type: ignore[attr-defined]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def request(self, url: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw)

    def install_fake_dispatch(self) -> None:
        def fake_dispatch(cfg, payload, *, request_id, request_meta, auth):
            self.calls.append({"payload": payload, "request_meta": request_meta, "auth": auth})
            return HandlerResult(200, {"ok": True, "route": "fake", "request_id": request_id})

        server.dispatch_notification = fake_dispatch

    def test_health_check(self) -> None:
        base_url = self.run_server(make_config(listen_port=0))
        code, body = self.request(base_url + "/health")

        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_post_wrong_path_returns_404(self) -> None:
        self.install_fake_dispatch()
        base_url = self.run_server(make_config(listen_port=0))
        code, body = self.request(
            base_url + "/wrong",
            method="POST",
            data=b'{"event":"test"}',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(self.calls, [])

    def test_secret_rejects_missing_and_wrong_values(self) -> None:
        self.install_fake_dispatch()
        base_url = self.run_server(make_config(listen_port=0, secret="secret-1"))

        missing_code, missing_body = self.request(
            base_url + "/webhook",
            method="POST",
            data=b'{"event":"test"}',
            headers={"Content-Type": "application/json"},
        )
        wrong_code, wrong_body = self.request(
            base_url + "/webhook",
            method="POST",
            data=b'{"event":"test"}',
            headers={"Content-Type": "application/json", "X-Webhook-Secret": "wrong"},
        )

        self.assertEqual(missing_code, 401)
        self.assertEqual(wrong_code, 401)
        self.assertFalse(missing_body["ok"])
        self.assertFalse(wrong_body["ok"])
        self.assertEqual(self.calls, [])

    def test_secret_accepts_header_and_query_values(self) -> None:
        self.install_fake_dispatch()
        base_url = self.run_server(make_config(listen_port=0, secret="secret-1"))

        header_code, _ = self.request(
            base_url + "/webhook",
            method="POST",
            data=b'{"event":"header"}',
            headers={"Content-Type": "application/json", "X-Webhook-Secret": "secret-1"},
        )
        query_code, _ = self.request(
            base_url + "/webhook?secret=secret-1",
            method="POST",
            data=b'{"event":"query"}',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(header_code, 200)
        self.assertEqual(query_code, 200)
        self.assertEqual([call["payload"]["event"] for call in self.calls], ["header", "query"])
        self.assertEqual(self.calls[0]["auth"]["status"], "passed")
        self.assertEqual(self.calls[1]["auth"]["status"], "passed")

    def test_post_parses_supported_content_types(self) -> None:
        self.install_fake_dispatch()
        base_url = self.run_server(make_config(listen_port=0))

        json_code, _ = self.request(
            base_url + "/webhook",
            method="POST",
            data=json.dumps({"event": "json", "ok": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        text_code, _ = self.request(
            base_url + "/webhook",
            method="POST",
            data="plain text".encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        form_code, _ = self.request(
            base_url + "/webhook",
            method="POST",
            data=urllib.parse.urlencode({"event": "form", "status": "ok"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual((json_code, text_code, form_code), (200, 200, 200))
        self.assertEqual(self.calls[0]["payload"], {"event": "json", "ok": True})
        self.assertEqual(self.calls[1]["payload"], "plain text")
        self.assertEqual(self.calls[2]["payload"], {"event": "form", "status": "ok"})


if __name__ == "__main__":
    unittest.main()
