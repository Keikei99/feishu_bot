"""离线验证重试耗尽后的持久失败通知，以及独立请求的模型调用语义。"""

from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from summary_bot.config import Config
from summary_bot.feishu import FeishuError
from summary_bot.llm import SummaryResult
from summary_bot.messages import ChatMessage
from summary_bot.service import FAILURE_NOTICE_TEXT, SummaryBot
from summary_bot.store import JobStore


class FailureNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.db"
        clock_patch = patch("summary_bot.store.time.time", return_value=1_700_000_000.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.store = self.new_store()
        self.message = ChatMessage(
            message_id="om_request", chat_id="oc_test", sender_id="ou_user",
            create_time_ms=1_700_000_000_000, message_type="text", text="总结",
            parent_id="om_forward", command="总结",
        )

    def new_store(self, **kwargs):
        return JobStore(self.path, max_retries=kwargs.pop("max_retries", 1), **kwargs)

    def row(self, message_id="om_request"):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM jobs WHERE message_id=?", (message_id,)).fetchone())

    def bot(self):
        config = Config(
            app_id="cli_notice_test", app_secret="fake-secret",
            llm_base_url="https://model.example.test", llm_api_key="fake-key",
            llm_model="fake-model", bot_open_id="ou_bot", state_db_path=self.path,
        )
        feishu, llm = Mock(), Mock()
        feishu.fetch_forwarded.return_value = [replace(
            self.message, message_id="om_selected", text="周五发布。", sender_name="张三",
        )]
        llm.summarize.return_value = SummaryResult("【关键结论】\n周五发布。")
        return SummaryBot(config, feishu, llm, self.store), feishu, llm

    def exhaust_result(self):
        """通过真实队列状态转换耗尽结果阶段，保留已有总结供断言。"""
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.mark_acknowledged(self.message.message_id)
        while True:
            self.assertIsNotNone(self.store.next_job())
            self.store.save_reply(self.message.message_id, "已经生成的总结")
            retry = self.store.finish(self.message.message_id, failed=True)
            if retry.next_retry_at is None:
                break
            self.clock.return_value = retry.next_retry_at

    def test_only_exhausted_business_retry_notifies_user_without_regenerating(self):
        bot, feishu, llm = self.bot()

        def reply(message_id, text, *, phase):
            if phase == "result":
                raise FeishuError("虚构发送故障")

        feishu.reply.side_effect = reply
        self.store.enqueue(self.message)
        self.assertTrue(bot.process_ack_next())
        self.assertTrue(bot.process_next())
        saved_text = self.row()["reply_text"]
        self.assertFalse(bot.process_failure_notice_next())
        self.clock.return_value = self.row()["next_retry_at"]
        self.assertTrue(bot.process_next())
        self.assertTrue(bot.process_failure_notice_next())
        self.assertEqual(feishu.reply.call_args.args, (self.message.message_id, FAILURE_NOTICE_TEXT))
        self.assertEqual(feishu.reply.call_args.kwargs, {"phase": "failure_notice"})
        self.assertEqual([call.kwargs["phase"] for call in feishu.reply.call_args_list],
                         ["receipt", "result", "result", "failure_notice"])
        self.assertEqual(llm.summarize.call_count, 1)
        row = self.row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["reply_text"], saved_text)
        self.assertTrue(row["payload"])
        self.assertEqual(row["failure_notice_status"], "sent")
        self.assertFalse(bot.process_failure_notice_next())

    def test_exhausted_receipt_notifies_without_starting_model(self):
        self.store = self.new_store(max_retries=0)
        bot, feishu, llm = self.bot()

        def reply(message_id, text, *, phase):
            if phase == "receipt":
                raise FeishuError("虚构回执故障")

        feishu.reply.side_effect = reply
        self.store.enqueue(self.message)
        self.assertTrue(bot.process_ack_next())
        self.assertTrue(bot.process_failure_notice_next())
        self.assertEqual(feishu.reply.call_args.kwargs, {"phase": "failure_notice"})
        self.assertFalse(bot.process_next())
        llm.summarize.assert_not_called()
        self.assertEqual(self.row()["failure_notice_status"], "sent")

    def test_notice_failure_has_independent_budget_and_stops_after_restart(self):
        self.exhaust_result()
        original = self.row()
        bot, feishu, llm = self.bot()
        feishu.reply.side_effect = FeishuError("虚构通知故障")
        self.assertTrue(bot.process_failure_notice_next())
        self.assertEqual(self.row()["failure_notice_status"], "pending")
        self.assertEqual(self.row()["failure_notice_attempts"], 1)
        retry_at = self.row()["failure_notice_next_retry_at"]
        self.assertEqual(retry_at, self.clock.return_value + 5)
        self.assertFalse(bot.process_failure_notice_next())
        self.clock.return_value = retry_at
        self.assertTrue(bot.process_failure_notice_next())
        row = self.row()
        self.assertEqual(row["failure_notice_status"], "failed")
        self.assertEqual(row["failure_notice_attempts"], 2)
        self.assertEqual(row["status"], "failed")
        for field in ("payload", "reply_text", "attempts"):
            self.assertEqual(row[field], original[field])
        self.clock.return_value += 100
        self.store = self.new_store()
        self.assertIsNone(self.store.next_failure_notice())
        self.assertIsNone(self.store.next_job())
        llm.summarize.assert_not_called()

    def test_notice_can_be_retried_after_delivery_recovers(self):
        self.exhaust_result()
        bot, feishu, llm = self.bot()
        feishu.reply.side_effect = FeishuError("虚构通知故障")
        self.assertTrue(bot.process_failure_notice_next())
        deadline = self.row()["failure_notice_next_retry_at"]
        self.clock.return_value += 1
        self.store = self.new_store()
        bot.store = self.store
        self.assertEqual(self.row()["failure_notice_next_retry_at"], deadline)
        self.assertFalse(bot.process_failure_notice_next())
        self.clock.return_value = deadline
        feishu.reply.side_effect = None
        self.assertTrue(bot.process_failure_notice_next())
        self.assertEqual([call.kwargs["phase"] for call in feishu.reply.call_args_list],
                         ["failure_notice", "failure_notice"])
        self.assertEqual(feishu.reply.call_args.args[1], FAILURE_NOTICE_TEXT)
        self.assertEqual(self.row()["failure_notice_status"], "sent")
        llm.summarize.assert_not_called()

    def test_sent_notice_is_not_sent_again_after_restart(self):
        self.exhaust_result()
        job = self.store.next_failure_notice()
        self.assertEqual(job.message.message_id, self.message.message_id)
        self.store.finish_failure_notice(self.message.message_id)
        self.clock.return_value += 100
        self.store = self.new_store()
        self.assertIsNone(self.store.next_failure_notice())
        self.assertEqual(self.row()["failure_notice_status"], "sent")
        self.assertEqual(self.row()["reply_text"], "已经生成的总结")

    def test_interrupted_notice_consumes_attempt_and_respects_backoff(self):
        self.exhaust_result()
        self.assertIsNotNone(self.store.next_failure_notice())
        self.store = self.new_store()
        self.assertEqual(self.row()["failure_notice_status"], "pending")
        self.assertEqual(self.row()["failure_notice_attempts"], 1)
        self.assertEqual(self.row()["failure_notice_next_retry_at"], self.clock.return_value + 5)
        self.assertIsNone(self.store.next_failure_notice())
        self.clock.return_value += 5
        self.assertIsNotNone(self.store.next_failure_notice())
        self.store = self.new_store()
        self.assertEqual(self.row()["failure_notice_status"], "failed")
        self.assertEqual(self.row()["failure_notice_attempts"], 2)
        self.assertIsNone(self.store.next_failure_notice())

    def test_lowering_budget_cancels_pending_notice(self):
        self.exhaust_result()
        self.store.next_failure_notice()
        self.store.finish_failure_notice(self.message.message_id, failed=True)
        self.store = self.new_store(max_retries=0)
        self.assertEqual(self.row()["failure_notice_status"], "failed")
        self.assertIsNone(self.store.next_failure_notice())

    def test_notice_backoff_is_capped(self):
        self.store = self.new_store(max_retries=3, retry_max_seconds=12)
        self.exhaust_result()
        for expected_delay in (5, 10, 12):
            self.assertIsNotNone(self.store.next_failure_notice())
            retry = self.store.finish_failure_notice(self.message.message_id, failed=True)
            self.assertEqual(retry.next_retry_at, self.clock.return_value + expected_delay)
            self.assertEqual(self.row()["status"], "failed")
            self.clock.return_value = retry.next_retry_at
        self.assertIsNotNone(self.store.next_failure_notice())
        retry = self.store.finish_failure_notice(self.message.message_id, failed=True)
        self.assertIsNone(retry.next_retry_at)
        self.assertEqual(self.row()["failure_notice_status"], "failed")

    def test_existing_failed_task_gets_notice_fields_without_restarting_business(self):
        old_path = self.path.with_name("old.db")
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute("""
                CREATE TABLE jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, reply_text TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL
                )
            """)
            db.execute(
                "INSERT INTO jobs(message_id,payload,reply_text,acknowledged,attempts,status,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.message.message_id, json.dumps(asdict(self.message)), "原有总结", 1, 2,
                 "failed", self.clock.return_value),
            )
        self.path = old_path
        self.store = self.new_store()
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.row()["reply_text"], "原有总结")
        self.assertIsNotNone(self.store.next_failure_notice())
        self.assertEqual(self.row()["failure_notice_attempts"], 1)
        self.assertEqual(self.row()["status"], "failed")

    def test_legacy_task_never_gets_failure_notice(self):
        self.store.enqueue(self.message)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE jobs SET status='legacy'")
        self.store = self.new_store()
        self.assertIsNone(self.store.next_failure_notice())
        self.assertEqual(self.row()["failure_notice_status"], "none")

    def test_background_worker_delivers_failure_notice_without_restart(self):
        self.store = self.new_store(max_retries=0)
        bot, feishu, llm = self.bot()
        delivered = Event()

        def reply(message_id, text, *, phase):
            if phase == "result":
                raise FeishuError("虚构发送故障")
            if phase == "failure_notice":
                delivered.set()

        feishu.reply.side_effect = reply
        self.store.enqueue(self.message)
        bot.start()
        try:
            self.assertTrue(delivered.wait(timeout=3), "后台线程没有发送重试耗尽通知")
        finally:
            bot.close()
        self.assertEqual(llm.summarize.call_count, 1)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["failure_notice_status"], "sent")

    def test_two_new_requests_for_same_forward_are_independent_model_calls(self):
        bot, feishu, llm = self.bot()
        second = replace(self.message, message_id="om_second_request")
        self.assertEqual(second.parent_id, self.message.parent_id)
        self.store.enqueue(self.message)
        self.store.enqueue(second)
        self.assertTrue(bot.process_ack_next())
        self.assertTrue(bot.process_ack_next())
        self.assertTrue(bot.process_next())
        self.assertTrue(bot.process_next())
        self.assertEqual(llm.summarize.call_count, 2)
        self.assertEqual(llm.summarize.call_args_list[0].args, llm.summarize.call_args_list[1].args)
        self.assertEqual([call.args[0] for call in feishu.reply.call_args_list],
                         [self.message.message_id, second.message_id, self.message.message_id, second.message_id])
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.row(second.message_id)["status"], "done")


if __name__ == "__main__":
    unittest.main()
