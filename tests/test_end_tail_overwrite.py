import unittest

from webhook_to_napcat.server import (
    AggregateBucket,
    build_end_bucket_metrics,
    get_bucket_field_value,
    should_replace_aggregate_bucket_event,
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


if __name__ == "__main__":
    unittest.main()
