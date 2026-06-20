from __future__ import annotations

import json
import tempfile
import unittest

from tests.helpers import make_config
from webhook_to_napcat import unknown
from webhook_to_napcat.napcat import DeliveryReport


PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5X2x8AAAAASUVORK5CYII="


class UnknownNotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_send_text = unknown.send_text
        self.original_send_segments = unknown.send_segments

    def tearDown(self) -> None:
        unknown.send_text = self.original_send_text
        unknown.send_segments = self.original_send_segments

    def test_unknown_json_is_forwarded_as_json_text(self) -> None:
        calls = []

        def fake_send_text(cfg, text, targets):
            calls.append(text)
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        unknown.send_text = fake_send_text
        unknown.send_segments = lambda cfg, segments, targets: DeliveryReport(results=[], chunks=[])

        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = unknown.handle_unknown_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                {"event": "test", "status": "ok"},
                request_id="req-1",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )

        self.assertEqual(result.status_code, 200)
        self.assertIn('"event": "test"', calls[0])
        self.assertIn('"status": "ok"', calls[0])

    def test_unknown_base64_is_saved_and_not_leaked_to_text(self) -> None:
        calls = {"text": [], "segments": []}

        def fake_send_text(cfg, text, targets):
            calls["text"].append(text)
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        def fake_send_segments(cfg, segments, targets):
            calls["segments"].append(segments)
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[])

        unknown.send_text = fake_send_text
        unknown.send_segments = fake_send_segments

        payload = {"event": "upload", "image_base64": PNG_BASE64}
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = unknown.handle_unknown_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                payload,
                request_id="req-2",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )

        self.assertEqual(result.status_code, 200)
        self.assertNotIn(PNG_BASE64, json.dumps(calls["text"], ensure_ascii=False))
        self.assertIn("base64_omitted", calls["text"][0])
        self.assertEqual(calls["segments"][0][0]["type"], "image")

    def test_unknown_without_default_target_fails(self) -> None:
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = unknown.handle_unknown_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, private=None, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                {"event": "test"},
                request_id="req-3",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
        self.assertEqual(result.status_code, 502)
        self.assertFalse(result.body["ok"])


if __name__ == "__main__":
    unittest.main()
