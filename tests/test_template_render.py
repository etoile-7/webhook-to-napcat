import json
import tempfile
import unittest
from pathlib import Path

from webhook_to_napcat.server import (
    Config,
    append_message_log,
    persist_incoming_base64_assets,
    render_rule_output,
    render_template_text,
    sanitize_payload_for_log,
    summarize_message_for_log,
)


class TemplateRenderTest(unittest.TestCase):
    def test_missing_field_does_not_break_other_fields(self) -> None:
        text = render_template_text(
            "🔴［{name}］下播了\n标题：{title}\nSC数量 ： {sc_count}｜ 金额：¥{sc_total}",
            {
                "name": "贝拉kira",
                "title": "【3D】贝拉的二三事",
                "sc_total": "3360",
            },
        )
        self.assertEqual(
            text,
            "🔴［贝拉kira］下播了\n标题：【3D】贝拉的二三事\nSC数量 ： {sc_count}｜ 金额：¥3360",
        )

    def test_nested_fields_render_in_template(self) -> None:
        text = render_template_text(
            "📼 录播上传完成\n标题：{live.title}\n提交信息：{submission.path}",
            {
                "live": {"title": "【贝拉/突击直播】2026.05.10 过战双主线碑火铸脊！ 直播录像"},
                "submission": {"path": "/data/app/submissions/example.json"},
            },
        )
        self.assertEqual(
            text,
            "📼 录播上传完成\n标题：【贝拉/突击直播】2026.05.10 过战双主线碑火铸脊！ 直播录像\n提交信息：/data/app/submissions/example.json",
        )

    def test_base64_cover_is_persisted_and_removed_from_log_payload(self) -> None:
        png_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5X2x8AAAAASUVORK5CYII="
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            cfg = Config(
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
                media_dir=media_dir,
                public_media_dir=public_dir,
            )
            payload = {
                "schema": "bili_upload_auto.result.v1",
                "event_id": "evt-1",
                "recording_key": "rec-1",
                "cover": {
                    "file_name": "cover.png",
                    "mime_type": "image/png",
                    "base64": png_base64,
                },
            }

            saved = persist_incoming_base64_assets(cfg, payload, "request-1")
            self.assertEqual(saved["cover_file"], saved["cover_path"])
            self.assertTrue(saved["cover_file"].startswith(public_dir))
            self.assertTrue(Path(saved["cover_file"].replace(public_dir, media_dir, 1)).exists())
            self.assertNotIn("base64", saved["cover"])
            self.assertTrue(saved["cover"]["base64_saved"])
            self.assertTrue(saved["cover"]["base64_uri"].startswith("base64://"))
            self.assertEqual(saved["cover_base64"], saved["cover"]["base64_uri"])

            logged = sanitize_payload_for_log(saved)
            self.assertEqual(logged["cover"]["file_path"], saved["cover_file"])
            self.assertNotIn("base64", logged["cover"])
            self.assertTrue(logged["cover_base64"]["base64_omitted"])
            self.assertEqual(logged["cover_base64"]["decoded_bytes"], len(Path(saved["cover"]["internal_path"]).read_bytes()))
            self.assertIn("sha256", logged["cover_base64"])

    def test_nested_non_image_base64_assets_are_persisted_recursively(self) -> None:
        pdf_base64 = "JVBERi0xLjQKSGVsbG8gUERG"
        with tempfile.TemporaryDirectory() as media_dir, tempfile.TemporaryDirectory() as public_dir:
            cfg = Config(
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
                media_dir=media_dir,
                public_media_dir=public_dir,
            )
            payload = {
                "schema": "example.file.upload.v1",
                "attachments": [
                    {
                        "filename": "manual.pdf",
                        "mime_type": "application/pdf",
                        "base64": pdf_base64,
                    }
                ],
            }

            saved = persist_incoming_base64_assets(cfg, payload, "request-2")
            attachment = saved["attachments"][0]
            self.assertTrue(attachment["file_path"].startswith(public_dir))
            self.assertTrue(attachment["file_path"].endswith(".pdf"))
            self.assertTrue(Path(attachment["file_path"].replace(public_dir, media_dir, 1)).exists())
            self.assertNotIn("base64", attachment)
            self.assertTrue(attachment["base64_saved"])

            logged = sanitize_payload_for_log(saved)
            self.assertTrue(logged["attachments"][0]["base64_uri"]["base64_omitted"])
            self.assertNotIn(pdf_base64, json.dumps(logged, ensure_ascii=False))

    def test_upload_success_cover_base64_can_be_rendered_and_summarized_without_leaking(self) -> None:
        png_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5X2x8AAAAASUVORK5CYII="
        message = render_rule_output(
            {
                "output": {
                    "type": "segments",
                    "segments": [
                        {"type": "text", "text": "uploaded"},
                        {"type": "image", "file": "{cover_base64}"},
                    ],
                    "fallback_template": "uploaded",
                }
            },
            {"cover_base64": f"base64://{png_base64}"},
        )

        self.assertEqual(message["mode"], "segments")
        self.assertEqual(message["segments"][1]["data"]["file"], f"base64://{png_base64}")
        summary = summarize_message_for_log(message)
        self.assertEqual(summary["segments"][1]["file_kind"], "base64")
        self.assertTrue(summary["segments"][1]["file"]["base64_omitted"])
        self.assertNotIn(png_base64, json.dumps(summary, ensure_ascii=False))

    def test_append_message_log_redacts_base64_as_last_line_of_defense(self) -> None:
        png_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5X2x8AAAAASUVORK5CYII="
        with tempfile.TemporaryDirectory() as log_dir:
            cfg = Config(
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
                log_dir=log_dir,
                aggregate_window_ms=3000,
                notify_file_opening=False,
                notify_debounce_ms=15000,
            )
            append_message_log(cfg, {"message": {"file": f"base64://{png_base64}"}})
            log_path = next(Path(log_dir).glob("messages-*.jsonl"))
            line = log_path.read_text(encoding="utf-8")
            self.assertNotIn(png_base64, line)
            logged = json.loads(line)
            self.assertTrue(logged["message"]["file"]["base64_omitted"])


if __name__ == "__main__":
    unittest.main()
