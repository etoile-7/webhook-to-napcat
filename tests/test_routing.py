from __future__ import annotations

import unittest

from tests.helpers import make_config
from webhook_to_napcat import server
from webhook_to_napcat.internal import HandlerResult


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
