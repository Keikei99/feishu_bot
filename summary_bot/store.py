"""使用 SQLite 保存待处理任务、已生成回复和消息去重状态，支持单进程恢复。"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import time

from .messages import ChatMessage


@dataclass(frozen=True)
class Job:
    """表示从持久化队列中领取的一条不可变任务快照。

    字段：
        message：触发功能的统一消息，包含精确指令和被引用消息 ID。
        reply_text：发送前已保存的回复文本；为 None 时需要生成总结，
            有值时可直接恢复发送，无需再次调用模型。
    """
    message: ChatMessage
    reply_text: str | None


class JobStore:
    """提供单进程使用的持久化先进先出队列，并以 message_id 去重。

    任务状态：
        pending 等待校验或处理；acknowledging 正在校验请求或发送回执；next_job 将已回执
        任务领取为 running；回复成功后成为 done；legacy 保留升级前未完成任务；
        处理或发送异常后成为 failed。初始化时将未过期的 running/failed 恢复
        为 pending。done 和 failed 的过期清理期限为七天。

    连接模型：
        每个操作独立打开连接，允许事件接收、回执和总结线程分别使用。
        不支持多个服务进程共同使用同一数据库，因为初始化包含任务恢复操作。
    """

    def __init__(self, path: Path):
        """初始化数据库、清理过期记录，并恢复上次未完成的任务。

        参数：
            path：数据库文件路径；父目录不存在时自动创建。

        副作用：
            创建 jobs 表及状态索引，启用 WAL 日志模式，先清理超过七天的
            done/failed 记录，再恢复剩余 running/failed 任务。保存过的回复保持不变。

        异常：
            目录权限、磁盘写入或 SQLite 初始化错误向调用方传播。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    reply_text TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
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
            self._prune(db)
            # 单进程恢复未完成任务及上次发送失败的任务；已有总结无需再次调用 LLM。
            db.execute("UPDATE jobs SET status='pending' WHERE status IN ('running', 'failed', 'acknowledging')")

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
        pending/running 任务不受影响，过期去重记录被删除后同 ID 可再次入队。
        """
        db.execute("DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
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
                "SELECT * FROM jobs WHERE status='pending' AND acknowledged=0 ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='acknowledging', updated_at=? WHERE seq=?",
                       (time.time(), row["seq"]))
            return Job(ChatMessage(**json.loads(row["payload"])), row["reply_text"])

    def mark_acknowledged(self, message_id: str) -> None:
        """回执成功后才允许执行功能；重启保留该状态，避免再次发送回执。"""
        with self._connection() as db:
            db.execute(
                "UPDATE jobs SET acknowledged=1, status='pending', updated_at=? "
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
                "SELECT * FROM jobs WHERE status='pending' AND acknowledged=1 ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', updated_at=? WHERE seq=?", (time.time(), row["seq"]))
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

    def finish(self, message_id: str, failed: bool = False) -> None:
        """写入任务处理结果，并按是否成功决定保留哪些恢复信息。

        参数：
            message_id：已领取任务对应的消息 ID。
            failed：True 时标记 failed，保留消息和已有回复供重启恢复；
                False 时标记 done，并清空 payload、reply_text 字段。

        返回：
            None。成功分支还会清理七天前的 done/failed 记录。

        注意：
            failed 不在当前工作循环中立即重试，而在下一次 JobStore 初始化时恢复。
            清空字段不等同于对 SQLite 文件或备份执行物理安全擦除。
        """
        with self._connection() as db:
            if failed:
                db.execute("UPDATE jobs SET status='failed', updated_at=? WHERE message_id=?",
                           (time.time(), message_id))
                return
            # 完成后只保留 ID 与状态，及时移除消息正文和总结。
            db.execute("UPDATE jobs SET status=?, payload='', reply_text=NULL, updated_at=? WHERE message_id=?",
                       ("done", time.time(), message_id))
            self._prune(db)
