from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_config
from webhook_to_napcat.bililive_context import (
    build_aggregate_context,
    build_end_bucket_metrics,
    get_bucket_field_value,
    is_recording_segment_end_bucket,
    is_recording_segment_start_bucket,
    is_true_bililive_end_bucket,
    is_true_bililive_start_bucket,
    should_replace_aggregate_bucket_event,
    should_suppress_recent_forwarded_end_candidate,
)
from webhook_to_napcat.bililive_model import AggregateBucket
from webhook_to_napcat.bililive_runtime import (
    AGGREGATE_BUCKETS,
    LIVE_SESSION_SEGMENTS,
    PENDING_END_LOCK,
    PENDING_END_NOTIFICATIONS,
    PENDING_START_AFTER_END_LOCK,
    PENDING_START_AFTER_END_NOTIFICATIONS,
    RECENT_FORWARDED_STARTS,
    apply_live_session_segments_to_bucket,
    cancel_pending_start_after_end,
    clear_live_session_segments,
    clear_recent_forwarded_start,
    get_recent_forwarded_start,
    handle_end_bucket,
    hold_start_after_recent_end,
    remember_live_session_segment,
    remember_recent_forwarded_start,
    resolve_bililive_targets,
    reset_bililive_state,
)
from webhook_to_napcat.bililive_message import build_bililive_message
from webhook_to_napcat.bililive_xml import PRICE_TABLE_CACHE
from webhook_to_napcat.config import parse_bililive_targets_json


class BililiveTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_bililive_state()

    def tearDown(self) -> None:
        reset_bililive_state()

    def make_end_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="bililive:end:30849777:心宜不是心仪",
            phase="end",
            group_name="bililive_end",
            event_order=["FileClosed", "SessionEnded", "StreamEnded"],
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
        )

    def make_start_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="bililive:start:22625027:乃琳Queen",
            phase="start",
            group_name="bililive_start",
            event_order=["StreamStarted", "SessionStarted", "FileOpening"],
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
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
        self.assertTrue(cancel_pending_start_after_end(cfg, notify_key, reason="cancelled_by_followup_true_end"))

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

    def test_missing_xml_stats_are_not_rendered_as_zero(self) -> None:
        cfg = make_config()
        notify_key = "bililive:22632424:贝拉kira:标题"
        bucket = self.make_end_bucket()
        bucket.request_ids.extend(["file-closed", "stream-ended"])
        bucket.events["FileClosed"] = {
            "request_id": "file-closed",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22632424,
                    "Name": "贝拉kira",
                    "Title": "标题",
                    "RelativePath": "rec/session.flv",
                    "FileSize": 1024,
                    "Duration": 60,
                    "Streaming": True,
                },
            },
            "ts": "t1",
        }
        bucket.events["StreamEnded"] = {
            "request_id": "stream-ended",
            "payload": {"EventType": "StreamEnded", "EventData": {"RoomId": 22632424, "Name": "贝拉kira", "Title": "标题", "Streaming": False}},
            "ts": "t2",
        }

        self.assertIsNotNone(apply_live_session_segments_to_bucket(notify_key, bucket, cfg))
        text = build_bililive_message(bucket, cfg)

        self.assertIn("时长：1m0s", text)
        self.assertNotIn("弹幕：0", text)
        self.assertNotIn("互动：0", text)
        self.assertNotIn("SC数量：0", text)
        self.assertNotIn("总营收：¥0", text)
        self.assertNotIn("舰长", text)
        self.assertNotIn("提督", text)
        self.assertNotIn("总督", text)

    def test_existing_xml_stats_are_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "session.xml").write_text(
                '<i><d p="0,1,25,16777215,0,0,1001,0">hi</d>'
                '<sc uid="1002" user="A" price="30" />'
                '<gift uid="1003" user="B" giftname="小花花" giftcount="2" />'
                '<gift uid="1005" user="D" giftname="未知礼物" giftcount="3" />'
                '<guard uid="1004" user="C" level="3" count="1" /></i>',
                encoding="utf-8",
            )
            price_table = root / "prices.md"
            price_table.write_text("|礼物名|价格|\n|---|---|\n|小花花|1.5|\n|舰长|138|\n", encoding="utf-8")
            cfg = make_config(
                bililive_xml_base_dir=str(root),
                bililive_xml_strip_prefixes=("rec/",),
                bililive_gift_price_table=str(price_table),
            )
            notify_key = "bililive:22632424:贝拉kira:标题"
            bucket = self.make_end_bucket()
            bucket.request_ids.extend(["file-closed", "stream-ended"])
            bucket.events["FileClosed"] = {
                "request_id": "file-closed",
                "payload": {
                    "EventType": "FileClosed",
                    "EventData": {
                        "RoomId": 22632424,
                        "Name": "贝拉kira",
                        "Title": "标题",
                        "RelativePath": "rec/session.flv",
                        "FileSize": 1024,
                        "Duration": 60,
                        "Streaming": True,
                    },
                },
                "ts": "t1",
            }
            bucket.events["StreamEnded"] = {
                "request_id": "stream-ended",
                "payload": {"EventType": "StreamEnded", "EventData": {"RoomId": 22632424, "Name": "贝拉kira", "Title": "标题", "Streaming": False}},
                "ts": "t2",
            }

            self.assertIsNotNone(apply_live_session_segments_to_bucket(notify_key, bucket, cfg))
            text = build_bililive_message(bucket, cfg)

            self.assertIn("弹幕：1", text)
            self.assertIn("互动人数：5｜弹幕：1", text)
            self.assertIn("新增舰长：1", text)
            self.assertIn("SC数量 ： 1｜ 金额：¥30", text)
            self.assertIn("总营收（已知）：¥171", text)
            self.assertIn("总营收（已知）：¥171\n未知礼物：未知礼物×3", text)
            self.assertNotIn("提督：0", text)
            self.assertNotIn("总督：0", text)

    def test_room_targets_override_default_targets_and_dedupe(self) -> None:
        cfg = make_config(
            private=111,
            group=222,
            bililive_targets={
                "22632424": (
                    "default",
                    {"group": 162525281},
                    {"group": 1054553890},
                    {"group": 222},
                    {"private": 111},
                )
            },
        )
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {"EventType": "StreamStarted", "EventData": {"RoomId": 22632424, "Name": "贝拉kira", "Title": "标题"}},
            "ts": "t1",
        }

        self.assertEqual(
            [target.to_log() for target in resolve_bililive_targets(cfg, bucket)],
            [{"private": 111}, {"group": 222}, {"group": 162525281}, {"group": 1054553890}],
        )

    def test_bililive_targets_json_parses_room_targets(self) -> None:
        self.assertEqual(
            parse_bililive_targets_json('{"22632424":["default",{"group":162525281},{"private":123}]}'),
            {"22632424": ("default", {"group": 162525281}, {"private": 123})},
        )

    def test_unconfigured_room_uses_default_targets(self) -> None:
        cfg = make_config(private=111, group=222, bililive_targets={"22632424": ({"group": 162525281},)})
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {"EventType": "StreamStarted", "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "标题"}},
            "ts": "t1",
        }

        self.assertEqual([target.to_log() for target in resolve_bililive_targets(cfg, bucket)], [{"private": 111}, {"group": 222}])

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

    def test_reset_bililive_state_clears_memory_and_cancels_pending_timer(self) -> None:
        cfg = make_config()
        notify_key = "bililive:22625027:乃琳Queen:reset-test"
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {"RoomId": 22625027, "Name": "乃琳Queen", "Title": "reset-test"},
            },
            "ts": "t1",
        }

        self.assertTrue(hold_start_after_recent_end(cfg, notify_key, bucket, "preview"))
        remember_recent_forwarded_start(cfg, notify_key, bucket)
        AGGREGATE_BUCKETS["manual"] = bucket
        LIVE_SESSION_SEGMENTS["manual"] = object()  # type: ignore[assignment]
        PRICE_TABLE_CACHE["prices.md"] = {"礼物": 1.0}

        with PENDING_START_AFTER_END_LOCK:
            pending = PENDING_START_AFTER_END_NOTIFICATIONS.get(notify_key)
            self.assertIsNotNone(pending)
            assert pending is not None
            timer = pending.timer

        reset_bililive_state()

        self.assertEqual(AGGREGATE_BUCKETS, {})
        self.assertEqual(PENDING_END_NOTIFICATIONS, {})
        self.assertEqual(PENDING_START_AFTER_END_NOTIFICATIONS, {})
        self.assertEqual(RECENT_FORWARDED_STARTS, {})
        self.assertEqual(LIVE_SESSION_SEGMENTS, {})
        self.assertEqual(PRICE_TABLE_CACHE, {})
        self.assertIsNotNone(timer)
        assert timer is not None
        self.assertTrue(timer.finished.is_set())


if __name__ == "__main__":
    unittest.main()
