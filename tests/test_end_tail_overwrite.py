import unittest

from webhook_to_napcat.server import (
    AggregateBucket,
    Config,
    build_end_bucket_metrics,
    build_start_bucket_score,
    cancel_pending_start_after_end,
    get_bucket_field_value,
    get_start_after_end_confirm_window_ms,
    hold_start_after_recent_end,
    is_recording_segment_end_bucket,
    is_recording_segment_start_bucket,
    is_true_bililive_end_bucket,
    is_true_bililive_start_bucket,
    clear_recent_forwarded_start,
    get_recent_forwarded_start,
    remember_recent_forwarded_start,
    should_replace_aggregate_bucket_event,
    should_suppress_recent_forwarded_end_candidate,
    should_suppress_recent_forwarded_start_candidate,
)


class EndTailOverwriteTest(unittest.TestCase):
    def make_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="aggregate:bililive_end:end:30849777:心宜不是心仪",
            phase="end",
            group_name="bililive_end",
            group_config={"event_order": ["FileClosed", "SessionEnded", "StreamEnded"]},
            created_at=0.0,
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
            target={"private": 1, "group": None},
        )

    def make_config(self) -> Config:
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
            title_prefix="",
            include_headers=False,
            rules_path="rules.json",
            log_dir="",
            aggregate_window_ms=3000,
            notify_file_opening=False,
            notify_debounce_ms=15000,
        )

    def make_start_bucket(self) -> AggregateBucket:
        return AggregateBucket(
            key="aggregate:bililive_start:start:22625027:乃琳Queen",
            phase="start",
            group_name="bililive_start",
            group_config={
                "event_order": ["StreamStarted", "SessionStarted", "FileOpening"],
                "window_ms": 60000,
                "post_end_start_confirm_ms": 10000,
            },
            created_at=0.0,
            request_path="/webhook",
            remote_ip="127.0.0.1",
            auth={},
            target={"private": 1, "group": None},
        )

    def test_weaker_tail_fileclosed_does_not_replace_stronger_main_fileclosed(self) -> None:
        bucket = self.make_bucket()
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
            "EventData": {
                "RoomId": 30849777,
                "Name": "心宜不是心仪",
                "Title": "【3D】糟糕，长脑子了！",
                "RelativePath": "rec/tail.flv",
                "FileSize": 482339,
                "Duration": 0.062,
                "Streaming": True,
            },
        }
        tail_sessionended = {
            "EventType": "SessionEnded",
            "EventData": {
                "RoomId": 30849777,
                "Name": "心宜不是心仪",
                "Title": "【3D】糟糕，长脑子了！",
                "SessionId": "tail-session",
                "Streaming": False,
                "Recording": False,
            },
        }
        tail_streamended = {
            "EventType": "StreamEnded",
            "EventData": {
                "RoomId": 30849777,
                "Name": "心宜不是心仪",
                "Title": "【3D】糟糕，长脑子了！",
                "Streaming": False,
                "Recording": True,
            },
        }

        bucket.events["FileClosed"] = {"request_id": "main", "payload": main_fileclosed, "ts": "t1"}

        self.assertFalse(
            should_replace_aggregate_bucket_event(bucket, "FileClosed", main_fileclosed, tiny_tail_fileclosed)
        )

        bucket.events["SessionEnded"] = {"request_id": "tail-se", "payload": tail_sessionended, "ts": "t2"}
        bucket.events["StreamEnded"] = {"request_id": "tail-st", "payload": tail_streamended, "ts": "t3"}

        self.assertEqual(get_bucket_field_value(bucket, "EventData.RelativePath"), "rec/main.flv")
        self.assertEqual(get_bucket_field_value(bucket, "EventData.FileSize"), 7717659925)
        self.assertAlmostEqual(get_bucket_field_value(bucket, "EventData.Duration"), 8610.49)

        metrics = build_end_bucket_metrics(bucket)
        self.assertEqual(metrics["file_size_bytes"], 7717659925)
        self.assertAlmostEqual(metrics["duration_seconds"], 8610.49)
        self.assertIs(metrics["streaming"], False)
        self.assertTrue(metrics["has_stream_ended"])

    def test_stronger_fileclosed_can_replace_weaker_existing_one(self) -> None:
        bucket = self.make_bucket()
        weak_existing = {
            "EventType": "FileClosed",
            "EventData": {"FileSize": 482339, "Duration": 0.062},
        }
        strong_new = {
            "EventType": "FileClosed",
            "EventData": {"FileSize": 7717659925, "Duration": 8610.49},
        }
        self.assertTrue(
            should_replace_aggregate_bucket_event(bucket, "FileClosed", weak_existing, strong_new)
        )

    def test_recent_forwarded_end_suppresses_late_tiny_end_only_tail(self) -> None:
        bucket = self.make_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "tiny-fc",
            "ts": "t1",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【鸣潮/突击】来玩团子活动！",
                    "RelativePath": "rec/tiny-tail.flv",
                    "FileSize": 1081563,
                    "Duration": 0.742,
                    "Streaming": True,
                    "Recording": False,
                },
            },
        }
        bucket.events["SessionEnded"] = {
            "request_id": "tiny-se",
            "ts": "t2",
            "payload": {
                "EventType": "SessionEnded",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【鸣潮/突击】来玩团子活动！",
                    "SessionId": "tiny-session",
                    "Streaming": True,
                    "Recording": False,
                },
            },
        }

        recent_score = (1, 1, 1728, 4366, 402630, 1325, 1476158361)
        self.assertTrue(should_suppress_recent_forwarded_end_candidate(recent_score, bucket))

    def test_recent_forwarded_end_does_not_suppress_meaningful_followup_end(self) -> None:
        bucket = self.make_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "followup-fc",
            "ts": "t1",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 30858592,
                    "Name": "思诺snow",
                    "Title": "【3D】思诺的100问八",
                    "RelativePath": "rec/followup.flv",
                    "FileSize": 47897408,
                    "Duration": 182.643,
                    "Streaming": True,
                    "Recording": False,
                },
            },
        }
        bucket.events["SessionEnded"] = {
            "request_id": "followup-se",
            "ts": "t2",
            "payload": {
                "EventType": "SessionEnded",
                "EventData": {
                    "RoomId": 30858592,
                    "Name": "思诺snow",
                    "Title": "【3D】思诺的100问八",
                    "SessionId": "followup-session",
                    "Streaming": True,
                    "Recording": False,
                },
            },
        }

        recent_score = (1, 1, 1141, 5510, 226740, 2398, 2075784198)
        self.assertFalse(should_suppress_recent_forwarded_end_candidate(recent_score, bucket))

    def test_recent_forwarded_start_suppresses_weaker_fileopening_tail(self) -> None:
        bucket = self.make_start_bucket()
        bucket.request_ids.extend(["fileopening-1"])
        bucket.events["FileOpening"] = {
            "request_id": "fileopening-1",
            "ts": "t1",
            "payload": {
                "EventType": "FileOpening",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                },
            },
        }

        recent_score = (1, 1, 0, 2)
        self.assertEqual(build_start_bucket_score(bucket), (0, 0, 1, 1))
        self.assertTrue(should_suppress_recent_forwarded_start_candidate(recent_score, bucket))

    def test_sessionstarted_without_streamstarted_is_recording_segment_start(self) -> None:
        bucket = self.make_start_bucket()
        bucket.events["SessionStarted"] = {
            "request_id": "session-started",
            "ts": "t1",
            "payload": {
                "EventType": "SessionStarted",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "Streaming": True,
                    "Recording": True,
                },
            },
        }

        self.assertTrue(is_recording_segment_start_bucket(bucket))
        self.assertFalse(is_true_bililive_start_bucket(bucket))

    def test_streamstarted_is_true_start(self) -> None:
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "ts": "t1",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "Streaming": True,
                    "Recording": True,
                },
            },
        }

        self.assertFalse(is_recording_segment_start_bucket(bucket))
        self.assertTrue(is_true_bililive_start_bucket(bucket))

    def test_post_end_reconnect_start_can_be_held_and_cancelled_by_followup_end(self) -> None:
        cfg = self.make_config()
        bucket = self.make_start_bucket()
        notify_key = "bililive:22625027:乃琳Queen:post-end-jitter-test"
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "ts": "t1",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "post-end-jitter-test",
                    "Streaming": True,
                    "Recording": True,
                },
            },
        }

        self.assertEqual(get_start_after_end_confirm_window_ms(cfg, bucket), 10000)
        self.assertTrue(hold_start_after_recent_end(cfg, notify_key, bucket, "preview"))
        self.assertTrue(
            cancel_pending_start_after_end(
                cfg,
                notify_key,
                reason="cancelled_by_followup_true_end",
                end_bucket=self.make_bucket(),
            )
        )

    def test_true_end_clears_start_dedupe_so_reconnect_start_is_allowed(self) -> None:
        cfg = self.make_config()
        notify_key = "bililive:22625027:乃琳Queen:【归环/突击】我也要死吗？"
        bucket = self.make_start_bucket()
        bucket.events["StreamStarted"] = {
            "request_id": "stream-started",
            "ts": "t1",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "Streaming": True,
                    "Recording": True,
                },
            },
        }

        clear_recent_forwarded_start(notify_key)
        remember_recent_forwarded_start(cfg, notify_key, bucket)
        self.assertIsNotNone(get_recent_forwarded_start(notify_key))

        # This is what a forwarded true StreamEnded/下播 notification does; after it,
        # a reconnecting StreamStarted for the same room/name/title must not be hidden
        # behind the previous lifecycle's dedupe state.
        clear_recent_forwarded_start(notify_key)
        self.assertIsNone(get_recent_forwarded_start(notify_key))

    def test_recording_segment_end_while_streaming_is_not_true_stream_end(self) -> None:
        bucket = self.make_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "segment-fc",
            "ts": "t1",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "RelativePath": "rec/segment.flv",
                    "FileSize": 5417829380,
                    "Duration": 4847.777,
                    "Streaming": True,
                    "Recording": True,
                },
            },
        }
        bucket.events["SessionEnded"] = {
            "request_id": "segment-se",
            "ts": "t2",
            "payload": {
                "EventType": "SessionEnded",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "SessionId": "old-session",
                    "Streaming": True,
                    "Recording": False,
                },
            },
        }

        self.assertTrue(is_recording_segment_end_bucket(bucket))
        self.assertFalse(is_true_bililive_end_bucket(bucket))

    def test_streamended_is_true_end_even_if_stats_payload_streaming_true(self) -> None:
        bucket = self.make_bucket()
        bucket.events["FileClosed"] = {
            "request_id": "fc",
            "ts": "t1",
            "payload": {
                "EventType": "FileClosed",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "FileSize": 5417829380,
                    "Duration": 4847.777,
                    "Streaming": True,
                },
            },
        }
        bucket.events["StreamEnded"] = {
            "request_id": "stream-ended",
            "ts": "t2",
            "payload": {
                "EventType": "StreamEnded",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                    "Streaming": False,
                },
            },
        }

        self.assertFalse(is_recording_segment_end_bucket(bucket))
        self.assertTrue(is_true_bililive_end_bucket(bucket))

    def test_recent_forwarded_start_suppresses_stronger_streamstarted_followup(self) -> None:
        bucket = self.make_start_bucket()
        bucket.request_ids.extend(["stream-1"])
        bucket.events["StreamStarted"] = {
            "request_id": "stream-1",
            "ts": "t1",
            "payload": {
                "EventType": "StreamStarted",
                "EventData": {
                    "RoomId": 22625027,
                    "Name": "乃琳Queen",
                    "Title": "【归环/突击】我也要死吗？",
                },
            },
        }

        recent_score = (0, 1, 0, 1)
        self.assertEqual(build_start_bucket_score(bucket), (1, 0, 0, 1))
        self.assertTrue(should_suppress_recent_forwarded_start_candidate(recent_score, bucket))


if __name__ == "__main__":
    unittest.main()
