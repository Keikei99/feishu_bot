"""总结栏目样式的离线测试，不读取配置或调用外部服务。"""

import unittest

from summary_bot.formatting import format_summary_body
from summary_bot.prompt import SYSTEM_PROMPT


class SummaryFormattingTests(unittest.TestCase):
    """仅规范栏目标题，保留总结实际内容和列表结构。"""

    def test_user_example_headings_use_brackets(self):
        """旧版四个粗体标题均转换为用户要求的栏目名称。"""
        old = (
            "**讨论主题**\n关于机器人测试。\n\n"
            "**关键结论**\n1. 正在等待测试。\n\n"
            "**待办事项**\n* 完成验收。\n\n"
            "**待确认问题**\n* 测试时间？"
        )
        expected = (
            "【讨论主题】\n关于机器人测试。\n\n"
            "【关键结论】\n1. 正在等待测试。\n\n"
            "【待办事项】\n* 完成验收。\n\n"
            "【待确认问题】\n* 测试时间？"
        )
        self.assertEqual(format_summary_body(old), expected)

    def test_legacy_heading_styles_and_idempotence(self):
        """兼容井号和纯标题，正确的【】样式不会再次变化。"""
        old = "#讨论主题\n内容\n\n## 关键结论 ##\n结论\n\n待办事项：\n事项\n\n【待确认问题】\n问题\n"
        formatted = "【讨论主题】\n内容\n\n【关键结论】\n结论\n\n【待办事项】\n事项\n\n【待确认问题】\n问题\n"
        self.assertEqual(format_summary_body(old), formatted)
        self.assertEqual(format_summary_body(formatted), formatted)

    def test_inline_heading_body_moves_to_next_line(self):
        """栏目后的冒号正文分到下一行，正文标点保持原样。"""
        old = "**讨论主题**： 测试进展。\n关键结论: 1. 继续等待。\n【待办事项】：* 执行测试。"
        expected = "【讨论主题】\n测试进展。\n【关键结论】\n1. 继续等待。\n【待办事项】\n* 执行测试。"
        self.assertEqual(format_summary_body(old), expected)

    def test_body_lists_indentation_and_emphasis_are_unchanged(self):
        """列表中的星号、缩进、编号和正文粗体都原样保留。"""
        body = "1. 李俊淇提出两个问题：\n   * 限制消息数量。\n   * 研究**功能路由**。\n\n* 责任人及期限未明确。\n"
        self.assertEqual(format_summary_body("**关键结论**\n" + body), "【关键结论】\n" + body)

    def test_plain_mentions_and_list_items_are_not_headings(self):
        """正文里讨论标题、引用标题或将其作为列表项时不误改。"""
        text = (
            "讨论主题是机器人测试。\n"
            "关键结论尚未形成。\n"
            "正文提到**待办事项**但没有列出内容。\n"
            "* **关键结论**\n"
            "1. 待确认问题\n"
            "> **讨论主题**\n"
        )
        self.assertEqual(format_summary_body(text), text)

    def test_blank_lines_and_crlf_are_preserved(self):
        """不折叠空行，也不改变原有换行形式。"""
        self.assertEqual(
            format_summary_body("\r\n**关键结论**\r\n\r\n* 内容\r\n"),
            "\r\n【关键结论】\r\n\r\n* 内容\r\n",
        )

    def test_fenced_examples_remain_literal(self):
        """代码块中的旧标题作为引用材料保留，不冒充正文栏目。"""
        text = "```text\n**讨论主题**\n```\n\n~~~\n关键结论\n~~~\n"
        self.assertEqual(format_summary_body(text), text)

    def test_prompt_requests_literal_titles_without_outer_metadata(self):
        """模型被要求直接使用四个栏目标题，并由程序统一添加外层信息。"""
        for title in ("讨论主题", "关键结论", "待办事项", "待确认问题"):
            self.assertIn(f"【{title}】", SYSTEM_PROMPT)
        self.assertIn("不重复输出外层总结标题、消息条数或时间行", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
