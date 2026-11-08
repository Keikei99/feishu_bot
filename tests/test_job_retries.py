"""验证持久任务在同一进程内按预算退避重试；所有时钟、接口和凭据均为虚构。"""

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
from summary_bot.service import SummaryBot
from summary_bot.store import JobStore


class JobRetryTests(unittest.TestCase):
    """使用虚拟墙钟验证重试期限、预算、迁移和用户可观察的发送行为。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "queue.db"
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
        return JobStore(self.path, **kwargs)

    def row(self, message_id="om_request"):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM jobs WHERE message_id=?", (message_id,)).fetchone())

    def advance(self, seconds):
        self.clock.return_value += seconds

    def acknowledge(self, message=None):
        message = message or self.message
        self.assertTrue(self.store.enqueue(message))
        self.assertEqual(self.store.next_ack().message.message_id, message.message_id)
        self.store.mark_acknowledged(message.message_id)

    def bot(self):
        config = Config(
            app_id="cli_retry_test", app_secret="fake-secret",
            llm_base_url="https://model.example.test", llm_api_key="fake-key",
            llm_model="fake-model", bot_open_id="ou_bot", state_db_path=self.path,
        )
        feishu, llm = Mock(), Mock()
        feishu.fetch_forwarded.return_value = [replace(
            self.message, message_id="om_selected", text="周五发布。", sender_name="张三",
        )]
        llm.summarize.return_value = SummaryResult("【关键结论】\n周五发布。")
        return SummaryBot(config, feishu, llm, self.store), feishu, llm

    def write_old_failed_database(self, acknowledged=0):
        self.path = Path(self.temporary.name) / f"old-{acknowledged}.db"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                    reply_text TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL
                )
            """)
            db.execute(
                "INSERT INTO jobs(message_id,payload,reply_text,acknowledged,status,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.message.message_id, json.dumps(asdict(self.message)), "保存的回复", acknowledged,
                 "failed", self.clock.return_value),
            )

    def test_exponential_backoff_is_capped_and_does_not_run_early(self):
        self.store = self.new_store(max_retries=4, retry_base_seconds=5, retry_max_seconds=12)
        self.store.enqueue(self.message)
        for attempt, delay in enumerate((5, 10, 12, 12), start=1):
            with self.subTest(attempt=attempt):
                self.assertIsNotNone(self.store.next_ack())
                self.assertEqual(self.row()["attempts"], attempt)
                self.store.finish(self.message.message_id, failed=True)
                row = self.row()
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["next_retry_at"], self.clock.return_value + delay)
                self.advance(delay - 0.01)
                self.assertIsNone(self.store.next_ack())
                self.advance(0.01)
        self.assertIsNotNone(self.store.next_ack())
        self.store.finish(self.message.message_id, failed=True)
        self.assertEqual(self.row()["attempts"], 5)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["next_retry_at"], 0)

    def test_zero_retries_stops_after_the_initial_attempt(self):
        self.store = self.new_store(max_retries=0)
        self.store.enqueue(self.message)
        self.assertIsNotNone(self.store.next_ack())
        self.store.finish(self.message.message_id, failed=True)
        self.advance(1000)
        self.assertIsNone(self.store.next_ack())
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["attempts"], 1)

    def test_backing_off_request_does_not_block_new_acknowledgements(self):
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.finish(self.message.message_id, failed=True)
        second = replace(self.message, message_id="om_second")
        self.store.enqueue(second)
        self.assertEqual(self.store.next_ack().message.message_id, second.message_id)
        self.assertIsNone(self.store.next_ack())

    def test_backing_off_result_does_not_block_new_ready_results(self):
        self.acknowledge()
        self.store.next_job()
        self.store.finish(self.message.message_id, failed=True)
        second = replace(self.message, message_id="om_second")
        self.acknowledge(second)
        self.assertEqual(self.store.next_job().message.message_id, second.message_id)
        self.assertIsNone(self.store.next_job())

    def test_receipt_success_starts_an_independent_result_budget(self):
        self.store = self.new_store(max_retries=1)
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.finish(self.message.message_id, failed=True)
        self.advance(5)
        self.assertIsNotNone(self.store.next_ack())
        self.assertEqual(self.row()["attempts"], 2)
        self.store.mark_acknowledged(self.message.message_id)
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.row()["next_retry_at"], 0)
        self.assertIsNotNone(self.store.next_job())
        self.store.finish(self.message.message_id, failed=True)
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.row()["next_retry_at"], self.clock.return_value + 5)

    def test_restart_keeps_attempts_and_existing_retry_deadline(self):
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.finish(self.message.message_id, failed=True)
        original = self.row()
        self.advance(2)
        self.store = self.new_store()
        self.assertEqual(self.row()["attempts"], original["attempts"])
        self.assertEqual(self.row()["next_retry_at"], original["next_retry_at"])
        self.assertIsNone(self.store.next_ack())
        self.advance(3)
        self.assertIsNotNone(self.store.next_ack())
        self.assertEqual(self.row()["attempts"], 2)

    def test_duplicate_failure_notification_does_not_reschedule_or_use_budget(self):
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.finish(self.message.message_id, failed=True)
        original = self.row()
        self.advance(1)
        self.store.finish(self.message.message_id, failed=True)
        self.assertEqual(self.row(), original)

    def test_lowered_retry_budget_cancels_old_retry_but_keeps_new_requests(self):
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store.finish(self.message.message_id, failed=True)
        second = replace(self.message, message_id="om_new_request")
        self.store.enqueue(second)
        self.store = self.new_store(max_retries=0)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.row()["next_retry_at"], 0)
        self.assertEqual(self.store.next_ack().message.message_id, second.message_id)
        self.assertEqual(self.row(second.message_id)["attempts"], 1)
        self.assertIsNone(self.store.next_ack())

    def test_exhausted_result_cannot_be_revived_by_restart(self):
        self.store = self.new_store(max_retries=1)
        self.acknowledge()
        self.store.next_job()
        self.store.save_reply(self.message.message_id, "已生成的总结")
        self.store.finish(self.message.message_id, failed=True)
        self.advance(5)
        self.store.next_job()
        self.store.finish(self.message.message_id, failed=True)
        for _ in range(2):
            self.store = self.new_store(max_retries=1)
            self.assertIsNone(self.store.next_ack())
            self.assertIsNone(self.store.next_job())
        row = self.row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["reply_text"], "已生成的总结")
        self.assertFalse(self.store.enqueue(self.message))

    def test_interrupted_receipt_uses_remaining_budget_and_backoff(self):
        self.store = self.new_store(max_retries=1)
        self.store.enqueue(self.message)
        self.store.next_ack()
        self.store = self.new_store(max_retries=1)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.row()["next_retry_at"], self.clock.return_value + 5)
        self.assertIsNone(self.store.next_ack())
        self.advance(5)
        self.assertIsNotNone(self.store.next_ack())
        self.store = self.new_store(max_retries=1)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["attempts"], 2)
        self.assertIsNone(self.store.next_ack())

    def test_interrupted_result_preserves_saved_text_and_retry_deadline(self):
        self.acknowledge()
        self.store.next_job()
        self.store.save_reply(self.message.message_id, "已保存的总结")
        self.store = self.new_store()
        self.assertIsNone(self.store.next_job())
        self.advance(5)
        job = self.store.next_job()
        self.assertEqual(job.reply_text, "已保存的总结")
        self.assertEqual(self.row()["attempts"], 2)
        self.assertIsNone(self.store.next_ack())

    def test_old_failed_tasks_migrate_once_with_retry_budget(self):
        for acknowledged in (0, 1):
            with self.subTest(acknowledged=acknowledged):
                self.write_old_failed_database(acknowledged)
                self.store = self.new_store()
                self.assertEqual(self.row()["status"], "pending")
                self.assertEqual(self.row()["attempts"], 1)
                self.assertEqual(self.row()["next_retry_at"], self.clock.return_value + 5)
                self.advance(1)
                self.store = self.new_store()
                self.assertEqual(self.row()["next_retry_at"], self.clock.return_value + 4)
                self.assertIsNone(self.store.next_ack())
                self.assertIsNone(self.store.next_job())
                self.advance(4)
                job = self.store.next_job() if acknowledged else self.store.next_ack()
                self.assertEqual(job.reply_text, "保存的回复")
                self.assertEqual(self.row()["attempts"], 2)

    def test_old_failed_task_honors_disabled_retries(self):
        self.write_old_failed_database()
        self.store = self.new_store(max_retries=0)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertIsNone(self.store.next_ack())
        self.store = self.new_store()
        self.assertIsNone(self.store.next_ack())

    def test_failed_migration_rolls_back_schema_and_can_be_retried(self):
        self.write_old_failed_database(acknowledged=1)
        original = self.row()
        with patch.object(JobStore, "_prune", side_effect=sqlite3.OperationalError("模拟升级中断")):
            with self.assertRaises(sqlite3.OperationalError):
                self.new_store()
        with closing(sqlite3.connect(self.path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("attempts", columns)
        self.assertNotIn("next_retry_at", columns)
        self.assertEqual(self.row(), original)
        self.store = self.new_store()
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.row()["next_retry_at"], self.clock.return_value + 5)

    def test_receipt_retries_in_same_process_then_summarizes(self):
        bot, feishu, llm = self.bot()
        self.store.enqueue(self.message)
        feishu.reply.side_effect = FeishuError("临时发送失败")
        self.assertTrue(bot.process_ack_next())
        self.assertFalse(bot.process_ack_next())
        self.assertFalse(bot.process_next())
        llm.summarize.assert_not_called()
        self.assertNotIn(self.message.message_id, bot._prepared_inputs)
        feishu.reply.side_effect = None
        self.advance(5)
        self.assertTrue(bot.process_ack_next())
        self.assertTrue(bot.process_next())
        self.assertEqual(llm.summarize.call_count, 1)
        self.assertEqual([call.kwargs["phase"] for call in feishu.reply.call_args_list],
                         ["receipt", "receipt", "result"])
        self.assertEqual(self.row()["status"], "done")

    def test_result_retry_reuses_text_without_model_or_receipt(self):
        bot, feishu, llm = self.bot()
        self.store.enqueue(self.message)
        self.assertTrue(bot.process_ack_next())
        feishu.reply.side_effect = FeishuError("临时发送失败")
        self.assertTrue(bot.process_next())
        saved_text = self.row()["reply_text"]
        self.assertFalse(bot.process_next())
        feishu.reply.side_effect = None
        self.advance(5)
        self.assertTrue(bot.process_next())
        self.assertEqual(llm.summarize.call_count, 1)
        self.assertEqual(feishu.fetch_forwarded.call_count, 1)
        self.assertEqual([call.kwargs["phase"] for call in feishu.reply.call_args_list],
                         ["receipt", "result", "result"])
        self.assertEqual(feishu.reply.call_args.args[1], saved_text)
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.row()["payload"], "")
        self.assertIsNone(self.row()["reply_text"])

    def test_menu_retry_reuses_saved_prompt_without_receipt(self):
        bot, feishu, llm = self.bot()
        self.store.enqueue(replace(self.message, command="帮助"))
        feishu.reply.side_effect = FeishuError("临时发送失败")
        self.assertTrue(bot.process_ack_next())
        saved_text = self.row()["reply_text"]
        self.assertIn("功能菜单", saved_text)
        self.assertFalse(bot.process_ack_next())
        self.advance(5)
        feishu.reply.side_effect = None
        self.assertTrue(bot.process_ack_next())
        self.assertEqual(feishu.reply.call_args.args[1], saved_text)
        self.assertEqual([call.kwargs["phase"] for call in feishu.reply.call_args_list],
                         ["result", "result"])
        self.assertFalse(bot.process_next())
        llm.summarize.assert_not_called()
        feishu.fetch_forwarded.assert_not_called()

    def test_background_worker_retries_after_deadline_without_restart(self):
        bot, feishu, llm = self.bot()
        retry_scheduled, delivered = Event(), Event()
        original_finish = self.store.finish
        result_attempts = 0

        def reply(message_id, text, *, phase):
            nonlocal result_attempts
            if phase == "result":
                result_attempts += 1
                if result_attempts == 1:
                    raise FeishuError("临时发送失败")
                delivered.set()

        def finish(message_id, failed=False):
            result = original_finish(message_id, failed=failed)
            if failed:
                retry_scheduled.set()
            return result

        feishu.reply.side_effect = reply
        self.store.enqueue(self.message)
        with patch.object(self.store, "finish", side_effect=finish):
            bot.start()
            try:
                self.assertTrue(retry_scheduled.wait(timeout=3), "后台线程没有记录发送失败")
                self.assertFalse(delivered.is_set())
                self.clock.return_value = self.row()["next_retry_at"]
                self.assertTrue(delivered.wait(timeout=3), "后台线程没有自动领取到期重试")
            finally:
                bot.close()
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(result_attempts, 2)
        self.assertEqual(llm.summarize.call_count, 1)


if __name__ == "__main__":
    unittest.main()
