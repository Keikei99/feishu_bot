"""仅处理群内 @：校验请求，有效总结先回执，菜单和操作提示直接回复。"""

from datetime import datetime
import logging
from threading import Event, Thread
from zoneinfo import ZoneInfo

from .config import Config
from .feishu import FeishuError, FeishuGateway, InvalidSelectionError
from .llm import AnthropicMessagesClient, LLMError
from .messages import ChatMessage
from .prompt import InputTooLarge, SummaryInput, build_input
from .routing import SUMMARY_USAGE, menu_text, resolve_command
from .store import JobStore


logger = logging.getLogger(__name__)
RECEIPT_TEXT = "收到，正在处理"


class SummaryBot:
    """校验线程独立于总结线程，使后续请求不必等前一个模型调用结束。

    事件回调只做群/@过滤和持久化。校验线程直接回复菜单和无效请求，
    仅在所选记录及预算通过校验后发送总结回执，并交给模型线程。
    已准备材料只缓存在内存；重启复用已生成结果，未生成结果时重新读取材料。
    """
    def __init__(self, config: Config, feishu: FeishuGateway,
                 llm: AnthropicMessagesClient, store: JobStore):
        """保存依赖并准备两个后台线程，此时不请求网络或启动线程。"""
        self.config, self.feishu, self.llm, self.store = config, feishu, llm, store
        self._wake = Event()
        self._receipt_wake = Event()
        self._stop = Event()
        self._worker = Thread(target=self._run, name="summary-worker", daemon=True)
        self._receipt_worker = Thread(target=self._run_receipts, name="receipt-worker", daemon=True)
        self._prepared_inputs: dict[str, tuple[SummaryInput, bool]] = {}
        self._handlers = {"总结": self._summary_reply}

    def start(self) -> None:
        """开始校验待处理请求，并依次总结已成功回执的记录。"""
        self._receipt_worker.start()
        self._worker.start()

    def close(self) -> None:
        """停止领取新任务，等待在途请求结束，未完成任务留待重启恢复。"""
        self._stop.set()
        self._wake.set()
        self._receipt_wake.set()
        for worker in (self._receipt_worker, self._worker):
            if worker.is_alive():
                worker.join()

    def submit(self, event: dict) -> bool:
        """只接受允许群中明确 @ 当前机器人的用户消息，按消息 ID 去重。

        私聊、机器人消息、只 @其他成员、缺少关键字段和重复推送均忽略。
        TRIGGER_MODE 的历史 all 配置不会放宽此过滤。此方法不访问外部接口。
        """
        sender, message = event.get("sender") or {}, event.get("message") or {}
        if sender.get("sender_type") != "user" or message.get("chat_type") != "group":
            return False
        if message.get("message_type") == "system":
            return False
        if self.config.allowed_chat_ids and message.get("chat_id") not in self.config.allowed_chat_ids:
            return False
        mentions = message.get("mentions") or []
        if not self.config.bot_open_id or not any(
            isinstance(mention, dict)
            and isinstance(mention.get("id"), dict)
            and mention["id"].get("open_id") == self.config.bot_open_id
            for mention in mentions
        ):
            return False
        try:
            trigger = ChatMessage.from_event(event, self.config.bot_open_id)
            if not trigger.message_id or not trigger.chat_id or trigger.create_time_ms <= 0:
                raise ValueError("缺少消息标识或时间")
        except (KeyError, ValueError, TypeError, AttributeError):
            logger.warning("忽略缺少必要字段的消息事件")
            return False
        accepted = self.store.enqueue(trigger)
        if accepted:
            self._receipt_wake.set()
            logger.info("消息已入队，message_id=%s, chat_id=%s", trigger.message_id, trigger.chat_id)
        return accepted

    def process_ack_next(self) -> bool:
        """校验一条请求；有效总结才回执，其他请求直接保存并发送提示。

        引用类型、群归属、记录读取和输入预算均须在回执前验证。已保存的
        菜单或错误提示优先恢复发送，不重新路由或追加回执。
        """
        job = self.store.next_ack()
        if job is None:
            return False
        trigger = job.message
        try:
            if job.reply_text is not None:
                self._send_final(trigger, job.reply_text)
                return True
            command = resolve_command(trigger.command)
            if command is None or command.keyword != "总结":
                self._send_final(trigger, self._route_reply(trigger))
                return True
            try:
                prepared = self._prepare_summary(trigger)
            except (InvalidSelectionError, InputTooLarge, FeishuError) as exc:
                self._send_final(trigger, self._error_reply(trigger, exc))
                return True
            self._prepared_inputs[trigger.message_id] = prepared
            self.feishu.reply(trigger.message_id, RECEIPT_TEXT, phase="receipt")
            self.store.mark_acknowledged(trigger.message_id)
        except Exception as exc:
            self._prepared_inputs.pop(trigger.message_id, None)
            logger.error("请求校验或回复失败，message_id=%s, error_type=%s", trigger.message_id, type(exc).__name__)
            self.store.finish(trigger.message_id, failed=True)
        else:
            self._wake.set()
        return True

    def _prepare_summary(self, trigger: ChatMessage) -> tuple[SummaryInput, bool]:
        """验证引用并准备完整模型输入；此步骤不发送回执、不调用模型。"""
        if not trigger.parent_id:
            raise InvalidSelectionError("未引用合并转发记录")
        messages = self.feishu.fetch_forwarded(trigger)
        if not messages:
            raise InvalidSelectionError("合并转发记录中没有可读取的消息")
        summary_input = build_input(messages, self.config)
        return summary_input, any(not message.sender_name for message in messages)

    def _summary_reply(self, trigger: ChatMessage) -> str:
        """复用回执前已校验的材料，重启丢失缓存时重新准备，不补发回执。"""
        prepared = self._prepared_inputs.pop(trigger.message_id, None)
        summary_input, has_unknown_names = prepared if prepared is not None else self._prepare_summary(trigger)
        result = self.llm.summarize(summary_input.content)
        timezone = ZoneInfo(self.config.display_timezone)
        start = datetime.fromtimestamp(summary_input.first_time_ms / 1000, timezone).strftime("%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(summary_input.last_time_ms / 1000, timezone).strftime("%m-%d %H:%M:%S")
        header = (
            f"所选聊天记录总结｜{summary_input.message_count} 条消息\n"
            f"{start} ～ {end}（{self.config.display_timezone}）"
        )
        notes = []
        if has_unknown_names:
            notes.append("部分发言者名称暂时无法获取，已标注为“姓名未知”。")
        if result.truncated:
            notes.append("模型达到输出长度上限，总结可能未完整生成。")
        return f"{header}\n\n{result.text}" + ("\n\n" + "\n".join(notes) if notes else "")

    def _route_reply(self, trigger: ChatMessage) -> str:
        """精确指令执行已注册功能，未识别输入返回文字菜单。"""
        command = resolve_command(trigger.command)
        if command is None:
            return menu_text()
        return self._handlers[command.keyword](trigger)

    @staticmethod
    def _error_reply(trigger: ChatMessage, error: Exception) -> str:
        """把已知校验或服务错误转换为安全的操作提示。"""
        if isinstance(error, InvalidSelectionError):
            prefix = "请先引用一条合并转发记录。" if not trigger.parent_id else "请引用一条本群中的合并转发记录。"
            return prefix + "\n\n" + SUMMARY_USAGE
        if isinstance(error, InputTooLarge):
            return f"{error}\n调整后，请重新合并转发到本群，再引用该记录发送「@机器人 总结」。"
        logger.error("功能处理失败，message_id=%s, reason=%s", trigger.message_id, error)
        return (
            "本次处理暂时失败，请稍后再次引用这条合并转发记录，发送「@机器人 总结」。"
            "\n管理员可检查群消息读取权限、模型配置和服务日志。"
        )

    def _send_final(self, trigger: ChatMessage, reply_text: str) -> None:
        """保存并发送最终文本；菜单和校验提示也使用独立结果阶段以支持恢复。"""
        self.store.save_reply(trigger.message_id, reply_text)
        self.feishu.reply(trigger.message_id, reply_text, phase="result")
        self.store.finish(trigger.message_id)
        logger.info("已回复，message_id=%s", trigger.message_id)

    def process_next(self) -> bool:
        """领取一条已成功回执的任务，保存结果后发送，支持失败后重启恢复。"""
        job = self.store.next_job()
        if job is None:
            return False
        trigger = job.message
        try:
            reply_text = job.reply_text
            if reply_text is None:
                try:
                    reply_text = self._route_reply(trigger)
                except (InvalidSelectionError, InputTooLarge, FeishuError, LLMError) as exc:
                    reply_text = self._error_reply(trigger, exc)
            self._send_final(trigger, reply_text)
        except Exception as exc:
            self._prepared_inputs.pop(trigger.message_id, None)
            logger.error("任务处理失败，message_id=%s, error_type=%s", trigger.message_id, type(exc).__name__)
            self.store.finish(trigger.message_id, failed=True)
        return True

    def _run_receipts(self) -> None:
        """持续校验请求，仅为有效总结发送回执并唤醒模型线程。"""
        while not self._stop.is_set():
            self._receipt_wake.clear()
            try:
                if self.process_ack_next():
                    continue
            except Exception as exc:
                logger.error("回执队列读取失败，error_type=%s", type(exc).__name__)
            self._receipt_wake.wait(1)

    def _run(self) -> None:
        """持续消费已回执任务，没有任务时等待事件或一秒超时。"""
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.process_next():
                    continue
            except Exception as exc:
                logger.error("任务队列读取失败，error_type=%s", type(exc).__name__)
            self._wake.wait(1)
