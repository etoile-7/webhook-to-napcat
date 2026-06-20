from __future__ import annotations

import tempfile
import unittest

from webhook_to_napcat import server
from webhook_to_napcat.config import Config
from webhook_to_napcat.internal import HandlerResult


def make_config() -> Config:
    return Config(
        listen_host="127.0.0.1",
        listen_port=8787,
        path="/webhook",
        secret="",
        napcat_base_url="http://127.0.0.1:3001",
        napcat_token="",
        napcat_token_mode="header",
        private=1,
        group=None,
        timeout=1.0,
        retries=0,
        chunk_size=280,
        log_dir="",
        media_dir=tempfile.gettempdir(),
        public_media_dir=tempfile.gettempdir(),
        outbound_text_max_chars=5000,
        aggregate_window_ms=3000,
        notify_debounce_ms=15000,
        live_session_segment_ttl_ms=1000,
        post_end_start_confirm_ms=1000,
        internal_dedupe_ttl_seconds=86400,
        bililive_xml_base_dir="",
        bililive_xml_strip_prefixes=(),
        bililive_gift_price_table="",
    )


class RoutingTest(unittest.TestCase):
    def test_ito_route_has_priority_over_bililive_shape(self) -> None:
        calls = []
        original_bililive = server.handle_bililive_notification
        original_unknown = server.handle_unknown_notification

        def fake_bililive(*args, **kwargs):
            calls.append("bililive")
            return HandlerResult(200, {"ok": True, "route": "bililive"})

        def fake_unknown(*args, **kwargs):
            calls.append("unknown")
            return HandlerResult(200, {"ok": True, "route": "unknown"})

        server.handle_bililive_notification = fake_bililive
        server.handle_unknown_notification = fake_unknown
        try:
            payload = {
                "notification_id": "ito:test",
                "program_id": "ito",
                "program_name": "ITO",
                "targets": [],
                "summary": "summary",
                "sent_at": "2026-06-10T13:30:00Z",
                "attachments": [],
                "EventType": "StreamStarted",
                "EventData": {"RoomId": 1},
            }
            result = server.dispatch_notification(make_config(), payload, request_id="req", request_meta={}, auth={})
        finally:
            server.handle_bililive_notification = original_bililive
            server.handle_unknown_notification = original_unknown

        self.assertEqual(result.body["route"], "ito")
        self.assertEqual(result.status_code, 400)
        self.assertIn("unexpected_fields:EventData,EventType", result.body["errors"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
