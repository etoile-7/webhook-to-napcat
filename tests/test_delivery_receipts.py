from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import make_config
from webhook_to_napcat import internal
from webhook_to_napcat.delivery_store import DeliveryStore
from webhook_to_napcat.napcat import DeliveryReport


class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = make_config(media_dir=self.directory.name, chunk_size=50)
        self.payload = {'notification_id': 'ito:receipt:test', 'program_id': 'ito',
            'program_name': 'test', 'targets': [{'type': 'user', 'id': '123'}, {'type': 'group', 'id': '456'}],
            'summary': 'hello', 'sent_at': '2026-09-26T00:00:00Z', 'attachments': []}

    def call(self):
        return internal.handle_internal_notification(self.cfg, self.payload,
            request_id='offline', request_meta={}, auth={})

    def test_failed_recipient_retries_without_replaying_success_and_survives_reopen(self):
        calls = []
        def send(cfg, text, targets):
            target = targets[0]
            calls.append(target.id)
            return DeliveryReport([{'ok': target.id == 123 or calls.count(456) > 1,
                                    'response': {'retcode': 1}}], [text])
        with patch.object(internal, 'send_text', side_effect=send):
            self.assertEqual(self.call().status_code, 502)
            result = self.call()  # Handler opens a new SQLite connection each time.
            self.assertEqual(result.body['state'], 'forwarded')
            self.assertEqual(result.body['text_confirmed'], 2)
            self.assertTrue(self.call().body['duplicate'])
        self.assertEqual(calls, [123, 456, 456])

    def test_all_failed_then_successful_retry_is_not_deduplicated(self):
        with patch.object(internal, 'send_text', return_value=DeliveryReport([{'ok': False, 'response': {'retcode': 1}}], [])):
            self.assertEqual(self.call().status_code, 502)
        with patch.object(internal, 'send_text', return_value=DeliveryReport([{'ok': True}], [])) as sender:
            self.assertEqual(self.call().body['state'], 'forwarded')
            self.assertEqual(sender.call_count, 2)

    def test_split_message_retries_only_failed_chunk_and_remainder(self):
        self.payload['targets'] = self.payload['targets'][:1]
        self.payload['summary'] = 'a'*130
        calls = []
        def send(cfg, text, targets):
            calls.append(text)
            return DeliveryReport([{'ok': len(calls) != 2, 'response': {'retcode': 1}}], [text])
        with patch.object(internal, 'send_text', side_effect=send):
            self.assertEqual(self.call().status_code, 502)
            self.cfg = replace(self.cfg, chunk_size=100)
            self.assertEqual(self.call().status_code, 200)
        self.assertEqual([len(c) for c in calls], [50, 50, 50, 30])

    def test_crash_after_sending_mark_is_uncertain(self):
        store = DeliveryStore(self.cfg.media_dir)
        with store.claim(self.payload):
            store.set_state(self.payload['notification_id'], 'text:private:123:0', 'sending')
        store.close()
        with patch.object(internal, 'send_text', return_value=DeliveryReport([{'ok': True}], [])) as sender:
            result = self.call()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(sender.call_count, 1)  # The independent group still receives it.

    def test_same_id_different_content_rejected_and_empty_targets_not_forwarded(self):
        self.payload['targets'] = []
        self.assertEqual(self.call().body['state'], 'accepted_no_targets')
        self.payload['summary'] = 'changed'
        self.assertEqual(self.call().status_code, 409)

    def test_invalid_attachment_does_not_prevent_text(self):
        self.payload['attachments'] = [{'type': 'image'}]
        with patch.object(internal, 'send_text', return_value=DeliveryReport([{'ok': True}], [])) as sender:
            result = self.call()
        self.assertEqual(result.body['state'], 'forwarded')
        self.assertEqual(result.body['attachment_failures'], 1)
        self.assertEqual(sender.call_count, 2)

    def test_verified_uncertain_outcomes_can_resume_without_replaying_delivered_target(self):
        with patch.object(internal, 'send_text', return_value=DeliveryReport(
                [{'ok': False, 'error': 'timeout'}], [])) as sender:
            self.assertEqual(self.call().body['state'], 'uncertain')
            self.assertEqual(self.call().status_code, 409)
            self.assertEqual(sender.call_count, 2)
        store = DeliveryStore(self.cfg.media_dir)
        store.resolve(self.payload['notification_id'], 'text:private:123:0',
                      outcome='delivered', evidence='QQ message found by operator')
        store.resolve(self.payload['notification_id'], 'text:group:456:0',
                      outcome='not-delivered', evidence='NapCat request rejected before send')
        with self.assertRaises(ValueError):
            store.resolve(self.payload['notification_id'], 'text:private:123:0',
                          outcome='not-delivered', evidence='attempt to reset a confirmed step')
        store.close()
        with patch.object(internal, 'send_text', return_value=DeliveryReport([{'ok': True}], [])) as sender:
            self.assertEqual(self.call().body['state'], 'forwarded')
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(sender.call_args.args[2][0].id, 456)



if __name__ == '__main__':
    unittest.main()
