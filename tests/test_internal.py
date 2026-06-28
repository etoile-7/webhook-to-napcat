from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_config
from webhook_to_napcat import internal
from webhook_to_napcat.napcat import DeliveryReport


PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5X2x8AAAAASUVORK5CYII="


class InternalNotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        internal.SEEN_NOTIFICATIONS.clear()
        self.original_send_text = internal.send_text
        self.original_send_file = internal.send_file
        self.original_send_segments = internal.send_segments

    def tearDown(self) -> None:
        internal.send_text = self.original_send_text
        internal.send_file = self.original_send_file
        internal.send_segments = self.original_send_segments
        internal.SEEN_NOTIFICATIONS.clear()

    def payload(self) -> dict:
        return {
            "notification_id": "ito:test:done:1",
            "program_id": "ito",
            "program_name": "ITO",
            "targets": [{"type": "user", "id": "123"}, {"type": "group", "id": "456"}],
            "summary": "ITO\n状态：完成",
            "sent_at": "2026-06-10T13:30:00Z",
            "attachments": [
                {"type": "image", "file_name": "result.png", "mime_type": "image/png", "base64": PNG_BASE64},
            ],
        }

    def test_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                {"program_id": "ito"},
                request_id="req-1",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.body["ok"])
        self.assertEqual(result.body["route"], "ito")

    def test_rejects_unexpected_top_level_fields(self) -> None:
        payload = self.payload()
        payload["EventType"] = "StreamStarted"
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                payload,
                request_id="req-extra",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.body["ok"])
        self.assertIn("unexpected_fields:EventType", result.body["errors"])

    def test_rejects_unknown_target_type(self) -> None:
        payload = self.payload()
        payload["targets"] = [{"type": "channel", "id": "789"}]
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                payload,
                request_id="req-target-type",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.body["ok"])
        self.assertIn("target_0_type_invalid", result.body["errors"])

    def test_rejects_non_numeric_target_id(self) -> None:
        payload = self.payload()
        payload["targets"] = [{"type": "user", "id": "user-001"}]
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                payload,
                request_id="req-target-id",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.body["ok"])
        self.assertIn("target_0_id_not_numeric", result.body["errors"])

    def test_sends_summary_and_image_attachment(self) -> None:
        calls = {"text": [], "images": [], "files": [], "order": []}

        def fake_send_text(cfg, text, targets):
            calls["order"].append("summary")
            calls["text"].append((text, [target.to_log() for target in targets]))
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        def fake_send_segments(cfg, segments, targets):
            calls["order"].append("image")
            calls["images"].append((segments, [target.to_log() for target in targets]))
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[])

        def fake_send_file(cfg, file_path, file_name, targets):
            calls["order"].append("file")
            calls["files"].append((file_path, file_name, [target.to_log() for target in targets]))
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[])

        internal.send_text = fake_send_text
        internal.send_segments = fake_send_segments
        internal.send_file = fake_send_file

        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                self.payload(),
                request_id="req-2",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
            saved_files = list(Path(media_dir).rglob("*.png"))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls["text"][0][0], "ITO\n状态：完成")
        self.assertEqual(calls["text"][0][1], [{"private": 123}, {"group": 456}])
        self.assertEqual(calls["images"][0][0][0]["type"], "image")
        self.assertTrue(calls["images"][0][0][0]["data"]["file"].startswith("base64://"))
        self.assertEqual(calls["images"][0][1], [{"private": 123}, {"group": 456}])
        self.assertEqual(calls["files"], [])
        self.assertEqual(calls["order"], ["summary", "image"])
        self.assertTrue(saved_files)

    def test_sends_non_image_attachment_as_file(self) -> None:
        calls = {"text": [], "files": [], "images": [], "order": []}

        def fake_send_text(cfg, text, targets):
            calls["order"].append("summary")
            calls["text"].append((text, [target.to_log() for target in targets]))
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        def fake_send_file(cfg, file_path, file_name, targets):
            calls["order"].append("file")
            calls["files"].append((file_path, file_name, [target.to_log() for target in targets]))
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[])

        internal.send_text = fake_send_text
        internal.send_file = fake_send_file
        internal.send_segments = lambda cfg, segments, targets: calls["images"].append(segments)  # type: ignore[assignment]

        payload = self.payload()
        payload["attachments"] = [{"type": "log", "file_name": "result.txt", "mime_type": "text/plain", "base64": "bG9n"}]

        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                payload,
                request_id="req-file",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls["files"][0][1], "result.txt")
        self.assertEqual(calls["files"][0][2], [{"private": 123}, {"group": 456}])
        self.assertEqual(calls["images"], [])
        self.assertEqual(calls["order"], ["summary", "file"])

    def test_attachment_failure_is_logged_as_partial_forwarded(self) -> None:
        def fake_send_text(cfg, text, targets):
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        def fake_send_segments(cfg, segments, targets):
            return DeliveryReport(results=[{"target": target.to_log(), "ok": False, "response": {"retcode": 200, "status": "failed"}} for target in targets], chunks=[])

        internal.send_text = fake_send_text
        internal.send_segments = fake_send_segments

        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir, tempfile.TemporaryDirectory() as log_dir:
            result = internal.handle_internal_notification(
                make_config(media_dir=media_dir, public_media_dir=public_dir, log_dir=log_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000),
                self.payload(),
                request_id="req-partial",
                request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"},
                auth={},
            )
            message_rows = []
            error_rows = []
            for path in Path(log_dir).glob("messages-*.jsonl"):
                message_rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            for path in Path(log_dir).glob("errors-*.jsonl"):
                error_rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body["attachment_failures"], 1)
        self.assertEqual(message_rows[-1]["outcome"], "partial_forwarded")
        self.assertEqual(message_rows[-1]["attachment_failure_count"], 1)
        self.assertEqual(error_rows[-1]["error_type"], "attachment_forward_failed")

    def test_duplicate_notification_is_accepted_without_resending(self) -> None:
        send_count = 0

        def fake_send_text(cfg, text, targets):
            nonlocal send_count
            send_count += 1
            return DeliveryReport(results=[{"target": target.to_log(), "ok": True, "response": {"retcode": 0}} for target in targets], chunks=[text])

        internal.send_text = fake_send_text
        internal.send_file = lambda cfg, file_path, file_name, targets: DeliveryReport(results=[], chunks=[])

        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            cfg = make_config(media_dir=media_dir, public_media_dir=public_dir, live_session_segment_ttl_ms=1000, post_end_start_confirm_ms=1000)
            first = internal.handle_internal_notification(cfg, self.payload(), request_id="req-3", request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"}, auth={})
            second = internal.handle_internal_notification(cfg, self.payload(), request_id="req-4", request_meta={"path": "/webhook", "remote_ip": "127.0.0.1"}, auth={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.body["duplicate"])
        self.assertEqual(send_count, 1)


if __name__ == "__main__":
    unittest.main()
