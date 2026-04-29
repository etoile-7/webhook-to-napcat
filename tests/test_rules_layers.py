import unittest

from webhook_to_napcat.server import normalize_rules_doc, render_rule_output


class RulesLayersTest(unittest.TestCase):
    def test_expands_named_outputs_and_targets_for_aggregate_outputs(self) -> None:
        doc = normalize_rules_doc(
            {
                "targets": {
                    "bella": ["default", {"group": 162525281}],
                },
                "outputs": {
                    "start_text": {
                        "type": "template",
                        "template": "🟢 {name}",
                    },
                    "bella_start": {
                        "$ref": "start_text",
                        "type": "segments",
                        "segments": [
                            {"type": "text", "text": "🟢 {name}"},
                            {"type": "image", "file": "/app/up/bella.jpg"},
                        ],
                        "targets_ref": "bella",
                    },
                },
                "aggregate": {
                    "groups": [
                        {
                            "name": "bililive_start",
                            "outputs": [
                                {
                                    "match": {"event_types_all": ["StreamStarted"]},
                                    "output_ref": "bella_start",
                                }
                            ],
                        }
                    ]
                },
            }
        )

        output = doc["aggregate"]["groups"][0]["outputs"][0]["output"]
        self.assertEqual(output["type"], "segments")
        self.assertEqual(output["template"], "🟢 {name}")
        self.assertEqual(output["targets"], ["default", {"group": 162525281}])

    def test_output_ref_can_be_overridden_inline(self) -> None:
        doc = normalize_rules_doc(
            {
                "outputs": {
                    "base": {
                        "type": "template",
                        "template": "base",
                        "targets": ["default"],
                    },
                },
                "rules": [
                    {
                        "match": {"has_keys": ["content"]},
                        "output": {"$ref": "base", "template": "override"},
                    }
                ],
                "default": {"$ref": "base", "template": "fallback"},
            }
        )

        output = doc["rules"][0]["output"]
        self.assertEqual(output["template"], "override")
        self.assertEqual(output["targets"], ["default"])
        self.assertEqual(doc["default"]["template"], "fallback")

    def test_template_output_can_add_cover_file_as_segments(self) -> None:
        rendered = render_rule_output(
            {
                "output": {
                    "type": "template",
                    "template": "🟢［{name}］开播啦！\n标题：{title}\n房间：{room_id}",
                    "cover_file": "/app/up/{room_id}.jpg",
                }
            },
            {"name": "嘉然今天吃什么", "title": "测试", "room_id": 22637261},
        )

        self.assertEqual(rendered["mode"], "segments")
        self.assertEqual(rendered["fallback_text"], "🟢［嘉然今天吃什么］开播啦！\n标题：测试\n房间：22637261")
        self.assertEqual(rendered["segments"][0]["data"]["text"], "🟢［嘉然今天吃什么］开播啦！")
        self.assertEqual(rendered["segments"][1]["type"], "image")
        self.assertEqual(rendered["segments"][1]["data"]["file"], "/app/up/22637261.jpg")
        self.assertEqual(rendered["segments"][2]["data"]["text"], "标题：测试\n房间：22637261")


if __name__ == "__main__":
    unittest.main()
