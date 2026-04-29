import unittest

from webhook_to_napcat.server import normalize_rules_doc


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


if __name__ == "__main__":
    unittest.main()
