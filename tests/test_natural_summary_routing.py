"""总结自然请求仍须明确路由与有效引用，避免将相关字样都当作指令。"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from summary_bot.llm import SummaryResult
from summary_bot.messages import ChatMessage, extract_command
from summary_bot.routing import COMMANDS, Command, resolve_command
from summary_bot.service import RECEIPT_TEXT, SummaryBot
from summary_bot.store import JobStore
from test_bot import configuration, event, history_item


class NaturalSummaryRoutingTests(unittest.TestCase):
    """以用户表达及竞争功能为边界验证总结请求路由。"""

    def test_common_summary_requests_resolve_to_same_function(self):
        """常用请求前缀、总结目标和句尾标点均路由到总结。"""
        for text in (
            "总结", "帮我总结一下", "总结重点", "请帮我总结一下这段聊天记录",
            "总结要点", "总结。", "总结！", "  帮我总结一下  ",
        ):
            with self.subTest(text=text):
                command = resolve_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.keyword, "总结")

    def test_negation_cancellation_help_and_unrelated_text_do_not_summarize(self):
        """否定、取消、询问用法或无关请求不因出现总结字样触发功能。"""
        for text in (
            "不要总结", "取消总结", "总结功能怎么用", "帮我查天气",
            "这是总结", "总结功能", "总结\n额外要求", "总结 @_user_2",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_command(text))

    def test_other_registered_functions_keep_exact_keyword_matching(self):
        """总结别名不影响其他功能的精确路由，精确关键词优先匹配。"""
        weather = Command("天气", "查看天气", "发送天气")
        exact_alias = Command("总结重点", "专门的已注册功能", "发送总结重点")
        with patch("summary_bot.routing.COMMANDS", (*COMMANDS, weather, exact_alias)):
            self.assertIs(resolve_command("天气"), weather)
            self.assertIsNone(resolve_command("请天气"))
            self.assertIs(resolve_command("总结重点"), exact_alias)

    def test_other_person_mentions_and_rich_content_cannot_become_summary_requests(self):
        """实际事件解析保留他人@、附件和链接，从而阻止总结请求误匹配。"""
        data = event(text="帮我总结一下 @_user_2")
        data["message"]["mentions"].append({
            "key": "@_user_2", "id": {"open_id": "ou_other"}, "name": "其他成员",
        })
        self.assertIsNone(resolve_command(extract_command(data["message"], "ou_summary_bot")))
        for extra in (
            {"tag": "img", "image_key": "unused"},
            {"tag": "a", "text": "链接", "href": "https://example.test"},
        ):
            with self.subTest(extra=extra["tag"]):
                message = event()["message"]
                message["message_type"] = "post"
                message["content"] = json.dumps({"content": [[
                    {"tag": "at", "user_id": "ou_summary_bot"},
                    {"tag": "text", "text": "帮我总结一下"}, extra,
                ]]})
                self.assertIsNone(resolve_command(extract_command(message, "ou_summary_bot")))


class NaturalSummaryServiceTests(unittest.TestCase):
    """自然请求沿用有效引用、回执及所选材料总结的完整流程。"""

    def setUp(self):
        """全部依赖为临时队列和模拟接口，避免读取本地配置。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = configuration(state_db_path=Path(temporary.name) / "queue.db")
        self.feishu, self.llm = Mock(), Mock()
        self.selected = replace(ChatMessage.from_history(history_item()), sender_name="张三")
        self.feishu.fetch_forwarded.return_value = [self.selected]
        self.llm.summarize.return_value = SummaryResult("关键结论：周五发布。")
        self.bot = SummaryBot(self.config, self.feishu, self.llm, JobStore(self.config.state_db_path))

    def test_summary_aliases_validate_reference_then_receipt_and_summarize(self):
        """两个常见自然表达都执行所选消息总结，并只读取引用记录一次。"""
        for index, text in enumerate(("帮我总结一下", "总结重点")):
            with self.subTest(text=text):
                self.feishu.reset_mock()
                self.llm.reset_mock()
                self.assertTrue(self.bot.submit(event(f"om_alias_{index}", text=text)))
                self.assertTrue(self.bot.process_ack_next())
                self.feishu.reply.assert_called_once_with(f"om_alias_{index}", RECEIPT_TEXT, phase="receipt")
                self.llm.summarize.assert_not_called()
                self.assertTrue(self.bot.process_next())
                self.feishu.fetch_forwarded.assert_called_once()
                self.llm.summarize.assert_called_once()
                records = json.loads(self.llm.summarize.call_args.args[0])["messages"]
                self.assertEqual([item["message_id"] for item in records], ["om_old"])
                self.assertEqual(self.feishu.reply.call_count, 2)
                self.assertEqual(self.feishu.reply.call_args.kwargs, {"phase": "result"})

    def test_natural_summary_without_reference_gets_usage_without_receipt(self):
        """接受自然表达不放宽引用要求，缺引用只返回一次操作提示。"""
        self.assertTrue(self.bot.submit(event(text="请帮我总结一下这段聊天记录", parent_id="")))
        self.assertTrue(self.bot.process_ack_next())
        self.assertFalse(self.bot.process_next())
        self.feishu.reply.assert_called_once()
        self.assertIn("合并转发", self.feishu.reply.call_args.args[1])
        self.assertNotIn(RECEIPT_TEXT, self.feishu.reply.call_args.args[1])
        self.assertEqual(self.feishu.reply.call_args.kwargs, {"phase": "result"})
        self.feishu.fetch_forwarded.assert_not_called()
        self.llm.summarize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
