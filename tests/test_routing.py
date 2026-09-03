from __future__ import annotations

import unittest

from tests.helpers import make_config
from webhook_to_napcat import server
from webhook_to_napcat.internal import HandlerResult


class RoutingTest(unittest.TestCase):
    def test_ito_route_has_priority_over_generic_fallback(self) -> None:
        original_unknown = server.handle_unknown_notification

        def fail_if_called(*args, **kwargs):
            raise AssertionError("ito payload must not use generic fallback")

        server.handle_unknown_notification = fail_if_called
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
            server.handle_unknown_notification = original_unknown

        self.assertEqual(result.body["route"], "ito")
        self.assertEqual(result.status_code, 400)
        self.assertIn("unexpected_fields:EventData,EventType", result.body["errors"])

    def test_recorder_shape_falls_back_to_generic_forwarding(self) -> None:
        calls = []
        original_unknown = server.handle_unknown_notification

        def fake_unknown(*args, **kwargs):
            calls.append(kwargs)
            return HandlerResult(200, {"ok": True, "route": "unknown"})

        server.handle_unknown_notification = fake_unknown
        try:
            result = server.dispatch_notification(
                make_config(),
                {"EventType": "StreamStarted", "EventData": {"RoomId": 1, "Name": "主播"}},
                request_id="req-recorder",
                request_meta={},
                auth={},
            )
        finally:
            server.handle_unknown_notification = original_unknown

        self.assertEqual(result.body["route"], "unknown")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
