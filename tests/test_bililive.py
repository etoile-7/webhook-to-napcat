from __future__ import annotations

import unittest

from webhook_to_napcat.bililive import (
    AggregateBucket,
    PENDING_END_LOCK,
    PENDING_END_NOTIFICATIONS,
    apply_live_session_segments_to_bucket,
    build_aggregate_context,
    build_end_bucket_metrics,
    build_start_bucket_score,
    cancel_pending_start_after_end,
    clear_live_session_segments,
    clear_recent_forwarded_start,
    get_bucket_field_value,
    get_recent_forwarded_start,
    handle_end_bucket,
    hold_start_after_recent_end,
    is_meaningful_streaming_end_candidate,
    is_recording_segment_end_bucket,
    is_recording_segment_start_bucket,
    is_true_bililive_end_bucket,
    is_true_bililive_start_bucket,
    remember_live_session_segment,
    remember_recent_forwarded_start,
    should_replace_aggregate_bucket_event,
    should_suppress_recent_forwarded_end_candidate,
    should_suppress_recent_forwarded_start_candidate,
)
from webhook_to_napcat.config import Config


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
        media_dir="media",
        public_media_dir="media",
        outbound_text_max_chars=5000,
        aggregate_window_ms=3000,
        notify_debounce_ms=15000,
        live_session_segment_ttl_ms=18 * 60 * 60 * 1000,
        post_end_start_confirm_ms=10000,
        internal_dedupe_ttl_seconds=86400,
        bililive_xml_base_dir="",
        bililive_xml_strip_prefixes=(),
        bililive_gift_price_table="",
    )


class BililiveTest(unittest.TestCase):
    def tearDown(self) -> None:
        with PENDING_END_LOCK:
            for pending in PENDING_END_NOTIFICATIONS.values():
                if pending.timer is not None:
                    pending.timer.cancel()
            PENDING_END_NOTIFICATIONS.clear()

    def make_end_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="bililive:end:30849777:心宜不是心仪",
            phase="end",
            group_name="bililive_end",
            group_config={"event_order": ["FileClosed", "SessionEnded", "StreamEnded"]},
            created_at=0.0,
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
            target={"private": 1},
        )

    def make_start_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="bililive:start:22625027:乃琳Queen",
            phase="start",
            group_name="bililive_start",
            group_config={"event_order": ["StreamStarted", "SessionStarted", "FileOpening"]},
            created_at=0.0,
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
            target={"private": 1},
        )

    def test_weaker_tail_fileclosed_does_not_replace_main_fileclosed(self) -> None:
        bucket = self.make_end_bucket()
        main_fileclosed = {
            "EventType": "FileClosed",
            "EventData": {
                "RoomId": 30849777,
                "Name": "心宜不是心仪",
                "Title": "【3D】糟糕，长脑子了！",
                "RelativePath": "rec/main.flv",
                "FileSize": 7717659925,
                "Duration": 8610.49,
                "Streaming": True,
            },
        }
        tiny_tail_fileclosed = {
            "EventType": "FileClosed",
            "EventData": {"RelativePath": "rec/tail.flv", "FileSize": 482339, "Duration": 0.062, "Streaming": True},
        }
        bucket.events["FileClosed"] = {"request_id": "main", "payload": main_fileclosed, "ts": "t1"}

        self.assertFalse(should_replace_aggregate_bucket_event(bucket, "FileClosed", main_fileclosed, tiny_tail_fileclosed))

        bucket.events["StreamEnded"] = {
            "request_id": "stream-ended",
            "payload": {"EventType": "StreamEnded", "EventData": {"Streaming": False}},
            "ts": "t2",
        }
        self.assertEqual(get_bucket_field_value(bucket, "EventData.RelativePath"), "rec/main.flv")
        metrics = build_end_bucket_metrics(bucket)
        self.assertEqual(metrics["file_size_bytes"], 7717659925)
        self.assertAlmostEqual(metrics["duration_seconds"], 8610.49)
        self.assertTrue(metrics["has_stream_ended"])
        self.assertIs(metrics["streaming"], False)

    def test_start_filters_recording_segment_and_accepts_streamstarted(self) -> None:
        bucket = self.make_start_bucket()
        bucket.events["SessionStarted"] = {
            "request_id": "session-started",
            "payload": {"EventType": "SessionStarted", "EventData": {"Streaming": True, "Recording": True}},
            "ts": "t1",
        }
        self.assertTrue(is_recording_segment_start_bucket(bucket))
        self.assertFalse(is_true_bililive_start_bucket(bucket))

        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {"EventType": "StreamStarted", "EventData": {"Streaming": True, "Recording": True}},
            "ts": "t2",
        }
        self.assertFalse(is_recording_segment_start_bucket(bucket))
        self.assertTrue(is_true_bililive_start_bucket(bucket))

    def test_recent_forwarded_start_suppresses_followup_start(self) -> None:
        bucket = self.make_start_bucket()
        bucket.request_ids.append("stream-1")
        bucket.events["StreamStarted"] = {
            "request_id": "stream-1",
            "payload": {"EventType": "StreamStarted", "EventData": {"RoomId": 1, "Name": "A", "Title": "T"}},
            "ts": "t1",
        }
        recent_score = (0, 1, 0, 1)
        self.assertEqual(build_start_bucket_score(bucket), (1, 0, 0, 1))
        self.assertTrue(should_suppress_recent_forwarded_start_candidate(recent_score, bucket))

    def test_post_end_reconnect_start_can_be_held_and_cancelled(self) -> None:
        cfg = make_config()
        bucket = self.make_start_bucket()
        notify_key = "bililive:22625027:乃琳Queen:post-end-jitter-test"
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "post-end-jitter-test"},
            },
            "ts": "t1",
        }
        self.assertTrue(hold_start_after_recent_end(cfg, notify_key, bucket, "preview"))
        self.assertTrue(cancel_pending_start_after_end(cfg, notify_key, reason="cancelled_by_followup_true_end", end_bucket=self.make_end_bucket()))

    def test_true_end_clears_start_dedupe(self) -> None:
        cfg = make_config()
        notify_key = "bililive:22625027:乃琳Queen:标题"
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {"EventType": "StreamStarted", "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "标题"}},
            "ts": "t1",
        }
        clear_recent_forwarded_start(notify_key)
        remember_recent_forwarded_start(cfg, notify_key, bucket)
        self.assertIsNotNone(get_recent_forwarded_start(notify_key))
        clear_recent_forwarded_start(notify_key)
        self.assertIsNone(get_recent_forwarded_start(notify_key))

    def test_recording_segment_end_is_not_true_end(self) -> None:
        bucket = self.make_end_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "segment-fc",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "标题",
                    "RelativePath": "rec/segment.flv",
                    "FileSize": 5417829380,
                    "Duration": 4847.777,
                    "Streaming": True,
                    "Recording": True,
                },
            },
            "ts": "t1",
        }
        self.assertTrue(is_recording_segment_end_bucket(bucket))
        self.assertTrue(is_meaningful_streaming_end_candidate(bucket))
        self.assertFalse(is_true_bililive_end_bucket(bucket))

    def test_streamended_merges_recording_segments_for_session_stats(self) -> None:
        cfg = make_config()
        notify_key = "bililive:22625027:乃琳Queen:标题"
        clear_live_session_segments(notify_key)

        segment = self.make_end_bucket()
        segment.request_ids.extend(["segment-fc"])
        segment.events["FileClosed"] = {
            "request_id": "segment-fc",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "标题", "RelativePath": "rec/part1.flv", "FileSize": 1000, "Duration": 7200, "Streaming": True},
            },
            "ts": "t1",
        }
        self.assertTrue(remember_live_session_segment(cfg, notify_key, segment))

        final = self.make_end_bucket()
        final.request_ids.extend(["final-fc", "stream-ended"])
        final.events["FileClosed"] = {
            "request_id": "final-fc",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "标题", "RelativePath": "rec/part2.flv", "FileSize": 2500, "Duration": 1800, "Streaming": True},
            },
            "ts": "t2",
        }
        final.events["StreamEnded"] = {
            "request_id": "stream-ended",
            "payload": {"EventType": "StreamEnded", "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "标题", "Streaming": False}},
            "ts": "t3",
        }
        self.assertTrue(is_true_bililive_end_bucket(final))
        self.assertIsNotNone(apply_live_session_segments_to_bucket(notify_key, final, cfg))
        context = build_aggregate_context(final, cfg)
        self.assertEqual(context["recording_segment_count"], 2)
        self.assertEqual(context["duration_seconds"], 9000)
        self.assertEqual(context["file_size_bytes"], 3500)
        self.assertIn("part1.flv", context["recording_segment_names"])
        self.assertIn("part2.flv", context["recording_segment_names"])
        clear_live_session_segments(notify_key)

    def test_empty_streamended_merges_into_pending_stats_candidate(self) -> None:
        cfg = make_config()
        notify_key = "bililive:22632424:贝拉kira:【突击】早上很坏！贝极星！"

        stats_bucket = self.make_end_bucket()
        stats_bucket.request_ids.extend(["session-ended", "file-closed"])
        stats_bucket.events["SessionEnded"] = {
            "request_id": "session-ended",
            "payload": {
                "EventType": "SessionEnded",
                "EventData": {
                    "RoomId": 22632424,
                    "Name": "贝拉kira",
                    "Title": "【突击】早上很坏！贝极星！",
                    "Streaming": True,
                    "Recording": False,
                },
            },
            "ts": "t1",
        }
        stats_bucket.events["FileClosed"] = {
            "request_id": "file-closed",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22632424,
                    "Name": "贝拉kira",
                    "Title": "【突击】早上很坏！贝极星！",
                    "RelativePath": "rec/main.flv",
                    "FileSize": 2504302033,
                    "Duration": 7266.716,
                    "Streaming": True,
                    "Recording": True,
                },
            },
            "ts": "t1",
        }
        handle_end_bucket(cfg, stats_bucket)

        stream_bucket = self.make_end_bucket()
        stream_bucket.request_ids.append("stream-ended")
        stream_bucket.events["StreamEnded"] = {
            "request_id": "stream-ended",
            "payload": {
                "EventType": "StreamEnded",
                "EventData": {
                    "RoomId": 22632424,
                    "Name": "贝拉kira",
                    "Title": "【突击】早上很坏！贝极星！",
                    "Streaming": False,
                    "Recording": False,
                },
            },
            "ts": "t2",
        }
        handle_end_bucket(cfg, stream_bucket)

        with PENDING_END_LOCK:
            pending = PENDING_END_NOTIFICATIONS.get(notify_key)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertIs(pending.bucket, stats_bucket)
            self.assertIn("StreamEnded", pending.bucket.events)
            self.assertIn("stream-ended", pending.bucket.request_ids)

        self.assertFalse(is_recording_segment_end_bucket(stats_bucket))
        self.assertTrue(is_true_bililive_end_bucket(stats_bucket))

    def test_recent_forwarded_end_suppresses_late_tiny_tail(self) -> None:
        bucket = self.make_end_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "tiny-fc",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {"RoomId": 1, "Name": "A", "Title": "T", "FileSize": 1081563, "Duration": 0.742, "Streaming": True},
            },
            "ts": "t1",
        }
        recent_score = (1, 1, 1728, 4366, 402630, 1325, 1476158361)
        self.assertTrue(should_suppress_recent_forwarded_end_candidate(recent_score, bucket))


if __name__ == "__main__":
    unittest.main()
