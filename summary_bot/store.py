"""使用 SQLite 保存待处理任务、已生成回复和消息去重状态，支持单进程恢复。"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import logging
import math
from pathlib import Path
import sqlite3
import time

from .messages import ChatMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    """表示从持久化队列中领取的一条不可变任务快照。

    字段：
        message：触发功能的统一消息，包含请求表达和被引用消息 ID。
        reply_text：发送前已保存的回复文本；为 None 时需要生成总结，
            有值时可直接恢复发送，无需再次调用模型。
    """
    message: ChatMessage
    reply_text: str | None


@dataclass(frozen=True)
class RetryState:
    """一次失败后的持久化调度结果；next_retry_at 为 None 表示次数耗尽。"""
    attempts: int
    next_retry_at: float | None


class JobStore:
    """提供单进程使用的持久化先进先出队列，并以 message_id 去重。

    任务状态：
        pending 等待校验或处理；acknowledging 正在校验请求或发送回执；next_job 将已回执
        任务领取为 running；回复成功后成为 done；legacy 保留升级前未完成任务；
        处理或发送异常后按退避重新进入 pending，次数耗尽后成为 failed。
        attempts 记录当前阶段已领取的次数，next_retry_at 是下次可领取的时间。
        重启保留次数和退避期限，并在相同预算内恢复中断任务；failed 不再复活。
        failed 的用户通知独立保存状态、次数和退避期限，不修改原回复。
        done 和 failed 的过期清理期限为七天，尚待发送的失败通知暂不清理。

    连接模型：
        每个操作独立打开连接，允许事件接收、回执和总结线程分别使用。
        不支持多个服务进程共同使用同一数据库，因为初始化包含任务恢复操作。
    """

    def __init__(self, path: Path, *, max_retries: int = 5,
                 retry_base_seconds: float = 5, retry_max_seconds: float = 300):
        """初始化数据库、清理过期记录，并恢复上次未完成的任务。

        参数：
            path：数据库文件路径；父目录不存在时自动创建。
            max_retries：每个阶段首次尝试之外的重试次数，可为零。
            retry_base_seconds / retry_max_seconds：指数退避的起始间隔和上限。

        副作用：
            创建 jobs 表及状态索引，启用 WAL 日志模式，先清理超过七天的
            done/failed 记录，再按预算调度中断任务。升级旧库时自动补充调度字段，
            旧 failed 按已经失败一次处理。保存过的回复保持不变。

        异常：
            目录权限、磁盘写入或 SQLite 初始化错误向调用方传播。
        """
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries 必须是非负整数")
        if (not math.isfinite(retry_base_seconds) or not math.isfinite(retry_max_seconds)
                or retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds):
            raise ValueError("重试间隔必须为有限正数，且上限不小于起始间隔")
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # 表结构升级与状态恢复一起提交，防止中途失败留下部分迁移的库。
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    reply_text TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    failure_notice_status TEXT NOT NULL DEFAULT 'none',
                    failure_notice_attempts INTEGER NOT NULL DEFAULT 0,
                    failure_notice_next_retry_at REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at REAL NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, seq)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "acknowledged" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0")
                # 旧任务没有保存 @身份和指令，无法证明满足新触发条件。保留记录，
                # 不在升级后自动发送旧总结或回执。
                db.execute("UPDATE jobs SET status='legacy' WHERE status IN ('pending', 'running', 'failed')")
            upgrading_retries = "attempts" not in columns
            if upgrading_retries:
                db.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE jobs SET attempts=1 WHERE status IN ('running', 'failed', 'acknowledging')")
            if "next_retry_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN next_retry_at REAL NOT NULL DEFAULT 0")
            for name, definition in (
                ("failure_notice_status", "TEXT NOT NULL DEFAULT 'none'"),
                ("failure_notice_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("failure_notice_next_retry_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, acknowledged, next_retry_at, seq)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_failure_notice_due ON jobs"
                       "(status, failure_notice_status, failure_notice_next_retry_at, seq)")
            self._prune(db)
            # 只有旧库的 failed 尚未应用重试上限；新库的 failed 必须保持终态。
            interrupted = db.execute(
                "SELECT message_id, attempts FROM jobs WHERE status IN ('running', 'acknowledging') "
                "OR (? AND status='failed') OR (status='pending' AND attempts>?)",
                (upgrading_retries, self.max_retries),
            ).fetchall()
            now = time.time()
            for row in interrupted:
                retry = self._schedule_retry(db, row["message_id"], max(1, row["attempts"]), now)
                if retry.next_retry_at is None:
                    logger.error("任务恢复时重试次数已耗尽，message_id=%s, attempts=%s",
                                 row["message_id"], retry.attempts)
                else:
                    logger.warning("恢复任务已安排重试，message_id=%s, attempts=%s, next_retry_at=%.3f",
                                   row["message_id"], retry.attempts, retry.next_retry_at)
            # 兼容升级前已耗尽的任务；已发送或已耗尽的通知始终保持终态。
            db.execute(
                "UPDATE jobs SET failure_notice_status='pending', failure_notice_next_retry_at=? "
                "WHERE status='failed' AND failure_notice_status='none'", (now,),
            )
            interrupted_notices = db.execute(
                "SELECT message_id, failure_notice_attempts FROM jobs WHERE status='failed' AND "
                "(failure_notice_status='sending' OR "
                "(failure_notice_status='pending' AND failure_notice_attempts>?))",
                (self.max_retries,),
            ).fetchall()
            for row in interrupted_notices:
                retry = self._schedule_failure_notice_retry(
                    db, row["message_id"], max(1, row["failure_notice_attempts"]), now,
                )
                if retry.next_retry_at is None:
                    logger.error("恢复失败通知时次数已耗尽，message_id=%s, attempts=%s",
                                 row["message_id"], retry.attempts)
                else:
                    logger.warning("失败通知已安排重试，message_id=%s, attempts=%s, next_retry_at=%.3f",
                                   row["message_id"], retry.attempts, retry.next_retry_at)

    @contextmanager
    def _connection(self):
        """在 with 块内提供独立 SQLite 连接，并管理提交、回滚和释放。

        产出：
            row_factory 为 sqlite3.Row 的连接，查询结果可按列名读取，
            锁等待超时为 10 秒。块内正常结束时提交，异常时回滚。

        无论块内是否抛出异常，finally 都关闭连接；SQLite 异常继续向外传播。
        """
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _prune(db):
        """删除七天前更新的 done/failed 记录，返回 None。

        db 是由调用方管理事务的 SQLite 连接；本方法不自行提交或关闭它。
        pending/running 任务及尚待发送的失败通知不受影响，过期去重记录被删除后
        同 ID 可再次入队。升级前尚未安排通知的 failed 仍适用七天保留期。
        """
        db.execute("DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ? "
                   "AND failure_notice_status NOT IN ('pending', 'sending')",
                   (time.time() - 7 * 86400,))

    def enqueue(self, message: ChatMessage) -> bool:
        """将统一消息序列化为待处理任务，按 message_id 原子去重。

        参数：
            message：已通过业务过滤和必要字段校验的触发消息。

        返回：
            新任务成功写入返回 True；数据库已有同一消息 ID 时返回 False，
            不覆盖旧任务的状态或已生成回复。方法返回前事务已经提交。

        异常：
            SQLite 连接或写入异常向外传播，让事件回调知道任务未成功落盘。
        """
        with self._connection() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO jobs(message_id, payload, updated_at) VALUES (?, ?, ?)",
                (message.message_id, json.dumps(asdict(message), ensure_ascii=False), time.time()),
            )
            return result.rowcount == 1

    def next_ack(self) -> Job | None:
        """原子领取待校验请求，连同已保存提示供发送失败后直接恢复。

        校验后只有有效总结才发送回执；菜单或无效请求由预处理线程直接回复。
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND acknowledged=0 "
                "AND next_retry_at <= ? ORDER BY seq LIMIT 1", (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='acknowledging', attempts=attempts+1, "
                       "next_retry_at=0, updated_at=? WHERE seq=?",
                       (time.time(), row["seq"]))
            return Job(ChatMessage(**json.loads(row["payload"])), row["reply_text"])

    def mark_acknowledged(self, message_id: str) -> None:
        """回执成功后才允许执行功能；重启保留该状态，避免再次发送回执。"""
        with self._connection() as db:
            db.execute(
                "UPDATE jobs SET acknowledged=1, status='pending', attempts=0, next_retry_at=0, updated_at=? "
                "WHERE message_id=? AND status='acknowledging'",
                (time.time(), message_id),
            )

    def next_job(self) -> Job | None:
        """领取序号最小、已发回执的 pending 任务，并原子标记 running。

        返回：
            有任务时返回包含消息和可恢复回复的 Job；队列无待处理任务时返回 None。

        事务行为：
            BEGIN IMMEDIATE 在读取前申请写事务，使选择与状态修改作为一个
            领取操作提交。记录不会被删除，异常退出后仍可由下次初始化恢复。

        异常：
            数据库或存储 JSON 损坏等异常继续传播给后台队列循环。
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND acknowledged=1 "
                "AND next_retry_at <= ? ORDER BY seq LIMIT 1", (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', attempts=attempts+1, next_retry_at=0, "
                       "updated_at=? WHERE seq=?", (time.time(), row["seq"]))
            return Job(ChatMessage(**json.loads(row["payload"])), row["reply_text"])

    def save_reply(self, message_id: str, text: str) -> None:
        """在实际发送前持久化回复文本，更新任务时间，返回 None。

        message_id 定位已有任务，text 可以是总结正文或通用失败提示。
        该操作不改变 running 状态；进程若在发送前后退出，恢复任务可复用此文本。
        调用方应只对已经领取的任务使用本方法，方法本身不检查更新行数。
        """
        with self._connection() as db:
            db.execute("UPDATE jobs SET reply_text=?, updated_at=? WHERE message_id=?",
                       (text, time.time(), message_id))

    def _retry_at(self, attempts: int, now: float) -> float | None:
        """根据已执行次数计算有上限的指数退避；None 表示预算耗尽。"""
        if attempts > self.max_retries:
            return None
        # 逐步翻倍并提前封顶，避免极大重试次数造成指数溢出。
        delay = self.retry_base_seconds
        for _ in range(attempts - 1):
            delay = min(delay * 2, self.retry_max_seconds)
            if delay >= self.retry_max_seconds:
                break
        return now + delay

    def _schedule_retry(self, db, message_id: str, attempts: int, now: float) -> RetryState:
        """在现有事务中安排退避或写入终态，并原子安排耗尽通知。"""
        next_retry_at = self._retry_at(attempts, now)
        db.execute(
            "UPDATE jobs SET status=?, attempts=?, next_retry_at=?, updated_at=? WHERE message_id=?",
            ("pending" if next_retry_at is not None else "failed", attempts,
             next_retry_at if next_retry_at is not None else 0, now, message_id),
        )
        if next_retry_at is None:
            db.execute(
                "UPDATE jobs SET failure_notice_status='pending', failure_notice_next_retry_at=? "
                "WHERE message_id=? AND failure_notice_status='none'", (now, message_id),
            )
        return RetryState(attempts, next_retry_at)

    def next_failure_notice(self) -> Job | None:
        """原子领取一条到期失败通知，不重新执行原任务，也不改动原回复。"""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='failed' AND failure_notice_status='pending' "
                "AND failure_notice_next_retry_at<=? ORDER BY seq LIMIT 1", (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET failure_notice_status='sending', "
                "failure_notice_attempts=failure_notice_attempts+1, "
                "failure_notice_next_retry_at=0, updated_at=? WHERE seq=?",
                (time.time(), row["seq"]),
            )
            return Job(ChatMessage(**json.loads(row["payload"])), row["reply_text"])

    def _schedule_failure_notice_retry(self, db, message_id: str, attempts: int,
                                       now: float) -> RetryState:
        """独立调度通知重试；业务任务仍保持 failed，原正文和结果保持不变。"""
        next_retry_at = self._retry_at(attempts, now)
        db.execute(
            "UPDATE jobs SET failure_notice_status=?, failure_notice_attempts=?, "
            "failure_notice_next_retry_at=?, updated_at=? WHERE message_id=? AND status='failed'",
            ("pending" if next_retry_at is not None else "failed", attempts,
             next_retry_at if next_retry_at is not None else 0, now, message_id),
        )
        return RetryState(attempts, next_retry_at)

    def finish_failure_notice(self, message_id: str, failed: bool = False) -> RetryState | None:
        """完成通知或持久化其独立退避；成功、重复完成通知时返回 None。"""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT failure_notice_attempts FROM jobs WHERE message_id=? AND status='failed' "
                "AND failure_notice_status='sending'", (message_id,),
            ).fetchone()
            if row is None:
                return None
            if failed:
                return self._schedule_failure_notice_retry(
                    db, message_id, row["failure_notice_attempts"], time.time(),
                )
            db.execute(
                "UPDATE jobs SET failure_notice_status='sent', failure_notice_next_retry_at=0, "
                "updated_at=? WHERE message_id=?", (time.time(), message_id),
            )

    def finish(self, message_id: str, failed: bool = False) -> RetryState | None:
        """写入任务处理结果，并按是否成功决定保留哪些恢复信息。

        参数：
            message_id：已领取任务对应的消息 ID。
            failed：True 时按当前阶段领取次数安排退避，耗尽后标记 failed，保留消息和已有回复；
                False 时标记 done，并清空 payload、reply_text 字段。

        返回：
            失败分支返回重试调度结果；成功或任务已不在执行状态时返回 None。
            成功分支还会清理七天前的 done/failed 记录。

        注意：
            退避时间和次数随事务持久化，重启不会重置等待期或复活耗尽任务。
            清空字段不等同于对 SQLite 文件或备份执行物理安全擦除。
        """
        with self._connection() as db:
            if failed:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT attempts FROM jobs WHERE message_id=? AND status IN ('acknowledging', 'running')",
                    (message_id,),
                ).fetchone()
                if row is None:
                    return None
                return self._schedule_retry(db, message_id, row["attempts"], time.time())
            # 完成后只保留 ID 与状态，及时移除消息正文和总结。
            db.execute("UPDATE jobs SET status=?, payload='', reply_text=NULL, next_retry_at=0, "
                       "updated_at=? WHERE message_id=?",
                       ("done", time.time(), message_id))
            self._prune(db)
