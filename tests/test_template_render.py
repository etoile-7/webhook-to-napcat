import unittest

from webhook_to_napcat.server import render_template_text


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


if __name__ == "__main__":
    unittest.main()
