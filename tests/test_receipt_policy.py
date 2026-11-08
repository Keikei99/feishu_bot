"""验证只有通过材料校验的总结请求才发送处理回执，不接触真实服务。"""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from summary_bot.config import Config
from summary_bot.feishu import FeishuError, InvalidSelectionError
from summary_bot.llm import SummaryResult
from summary_bot.messages import ChatMessage
from summary_bot.prompt import build_input
from summary_bot.service import RECEIPT_TEXT, SummaryBot
from summary_bot.store import JobStore


NOW = 1_700_000_000_500


def request(message_id="om_request", keyword="总结", parent_id="om_forward"):
    """构造明确 @ 本机器人的用户群消息，引用对象可独立调整。"""
    return {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id, "chat_id": "oc_group", "chat_type": "group",
            "create_time": str(NOW), "message_type": "text", "parent_id": parent_id,
            "content": json.dumps({"text": "@_user_1 " + keyword}, ensure_ascii=False),
            "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "消息总结"}],
        },
    }


class ReceiptPolicyTests(unittest.TestCase):
    """将回执次数、材料校验顺序、异常恢复和并发作为可观察行为验证。"""

    def setUp(self):
        """每个场景使用虚构配置、临时数据库和模拟网关/模型。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Config(
            app_id="cli_receipt_test", app_secret="fake-secret",
            llm_base_url="https://model.example.test", llm_api_key="fake-key",
            llm_model="fake-model", bot_open_id="ou_bot",
            state_db_path=Path(temporary.name) / "queue.db",
        )
        self.store = JobStore(self.config.state_db_path)
        self.feishu, self.llm = Mock(), Mock()
        self.selected = ChatMessage(
            message_id="om_selected", chat_id="oc_group", sender_id="ou_user",
            create_time_ms=NOW - 1000, message_type="text", text="周五发布。", sender_name="张三",
        )
        self.feishu.fetch_forwarded.return_value = [self.selected]
        self.llm.summarize.return_value = SummaryResult("关键结论：周五发布。")
        self.bot = SummaryBot(self.config, self.feishu, self.llm, self.store)

    def assert_direct_result(self, expected_text):
        """直接提示在预处理阶段发完，不追加回执或模型任务。"""
        self.assertTrue(self.bot.process_ack_next())
        self.assertFalse(self.bot.process_next())
        self.feishu.reply.assert_called_once()
        reply = self.feishu.reply.call_args
        self.assertEqual(reply.kwargs, {"phase": "result"})
        self.assertIn(expected_text, reply.args[1])
        self.assertNotIn(RECEIPT_TEXT, reply.args[1])
        self.llm.summarize.assert_not_called()

    def test_unknown_and_empty_keywords_reply_with_menu_only(self):
        """未识别指令或仅 @ 机器人只收到一条文字菜单。"""
        for index, keyword in enumerate(("帮助", "", "不要总结", "总结功能怎么用")):
            with self.subTest(keyword=keyword):
                self.feishu.reply.reset_mock()
                self.assertTrue(self.bot.submit(request(f"om_menu_{index}", keyword)))
                self.assert_direct_result("功能菜单")
        self.feishu.fetch_forwarded.assert_not_called()

    def test_missing_reference_replies_with_usage_only(self):
        """总结指令缺少引用不发回执，也不查询其他消息。"""
        self.assertTrue(self.bot.submit(request(parent_id="")))
        self.assert_direct_result("多选")
        self.feishu.fetch_forwarded.assert_not_called()

    def test_invalid_or_empty_reference_replies_without_receipt(self):
        """普通、跨群引用及空记录均在回执之前被拒绝。"""
        cases = (
            ("ordinary", InvalidSelectionError("引用的不是合并转发")),
            ("other_group", InvalidSelectionError("引用记录不在本群")),
            ("empty", None),
        )
        for label, error in cases:
            with self.subTest(selection=label):
                self.feishu.reply.reset_mock()
                self.feishu.fetch_forwarded.side_effect = error
                self.feishu.fetch_forwarded.return_value = []
                self.assertTrue(self.bot.submit(request(f"om_{label}")))
                self.assert_direct_result("合并转发")

    def test_read_failure_replies_with_error_without_receipt(self):
        """没有成功读取材料时不声称已经开始处理总结。"""
        self.feishu.fetch_forwarded.side_effect = FeishuError("读取权限不足")
        self.assertTrue(self.bot.submit(request()))
        self.assert_direct_result("暂时失败")

    def test_message_or_total_budget_failure_has_no_receipt(self):
        """单条正文或完整材料超限时直接提示拆分，不发送处理回执。"""
        self.bot.config = replace(self.config, max_message_chars=2000, max_input_chars=4000)
        cases = (
            [replace(self.selected, text="中" * 2001)],
            [replace(self.selected, message_id=f"om_{index}", text="中" * 700) for index in range(8)],
        )
        for index, messages in enumerate(cases):
            with self.subTest(budget=index):
                self.feishu.reply.reset_mock()
                self.feishu.fetch_forwarded.return_value = messages
                self.assertTrue(self.bot.submit(request(f"om_budget_{index}")))
                self.assert_direct_result("过长")

    def test_valid_summary_validates_before_receipt_and_reuses_material(self):
        """获取和输入校验先于回执，模型消费同一份材料而不再次查询。"""
        order = []
        self.feishu.fetch_forwarded.side_effect = lambda trigger: order.append("fetch") or [self.selected]
        self.feishu.reply.side_effect = lambda *args, phase: order.append(phase)
        self.llm.summarize.side_effect = lambda content: order.append("model") or SummaryResult("结论")

        def checked_input(*args, **kwargs):
            order.append("build")
            return build_input(*args, **kwargs)

        self.assertTrue(self.bot.submit(request()))
        with patch("summary_bot.service.build_input", side_effect=checked_input):
            self.assertTrue(self.bot.process_ack_next())
            self.assertEqual(order, ["fetch", "build", "receipt"])
            self.llm.summarize.assert_not_called()
            self.assertTrue(self.bot.process_next())
        self.assertEqual(order, ["fetch", "build", "receipt", "model", "result"])
        self.feishu.fetch_forwarded.assert_called_once()
        self.assertEqual(self.feishu.reply.call_count, 2)

    def test_failed_menu_delivery_recovers_without_receipt_or_fetch(self):
        """直接提示发送失败后，重启只重发已保存的菜单。"""
        self.feishu.reply.side_effect = FeishuError("发送失败")
        self.assertTrue(self.bot.submit(request(keyword="帮助")))
        self.assertTrue(self.bot.process_ack_next())
        self.assertFalse(self.bot.process_next())
        self.feishu.reply.side_effect = None
        self.feishu.reply.reset_mock()
        self.feishu.fetch_forwarded.side_effect = AssertionError("恢复菜单不得查询消息")
        restarted = SummaryBot(self.config, self.feishu, self.llm, JobStore(self.config.state_db_path))
        self.assertFalse(restarted.process_ack_next())
        with closing(sqlite3.connect(self.config.state_db_path)) as db:
            retry_at = db.execute("SELECT next_retry_at FROM jobs").fetchone()[0]
        with patch("summary_bot.store.time.time", return_value=retry_at):
            self.assertTrue(restarted.process_ack_next())
        self.assertFalse(restarted.process_next())
        self.feishu.reply.assert_called_once()
        reply = self.feishu.reply.call_args
        self.assertEqual(reply.kwargs, {"phase": "result"})
        self.assertIn("功能菜单", reply.args[1])
        self.assertNotIn(RECEIPT_TEXT, reply.args[1])
        self.feishu.fetch_forwarded.assert_not_called()
        self.llm.summarize.assert_not_called()

    def test_blocked_model_does_not_delay_menu_or_second_valid_receipt(self):
        """总结耗时不阻塞后续菜单，且只给后续有效总结发送回执。"""
        model_started, release_model = Event(), Event()
        menu_sent, second_receipt = Event(), Event()
        replies = []

        def summarize(content):
            model_started.set()
            if not release_model.wait(timeout=5):
                raise AssertionError("测试未释放模型")
            return SummaryResult("结论")

        def reply(message_id, text, *, phase):
            replies.append((message_id, text, phase))
            if message_id == "om_menu" and phase == "result":
                menu_sent.set()
            if message_id == "om_second" and phase == "receipt":
                second_receipt.set()

        self.feishu.reply.side_effect = reply
        self.llm.summarize.side_effect = summarize
        self.assertTrue(self.bot.submit(request("om_first")))
        self.bot.start()
        try:
            self.assertTrue(model_started.wait(timeout=3))
            self.assertTrue(self.bot.submit(request("om_menu", keyword="帮助")))
            self.assertTrue(self.bot.submit(request("om_second")))
            self.assertTrue(menu_sent.wait(timeout=2))
            self.assertTrue(second_receipt.wait(timeout=2))
            self.assertFalse(release_model.is_set())
            self.assertEqual([phase for mid, text, phase in replies if mid == "om_menu"], ["result"])
            self.assertEqual(self.llm.summarize.call_count, 1)
        finally:
            release_model.set()
            self.bot.close()


if __name__ == "__main__":
    unittest.main()
