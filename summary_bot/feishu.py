"""封装飞书 SDK：读取选中的合并转发、解析姓名，并分阶段引用回复。"""

from dataclasses import dataclass
import json
import logging
import re
import time
from uuid import NAMESPACE_URL, uuid5

import lark_oapi as lark
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest
from lark_oapi.api.im.v1 import (
    GetChatMembersRequest, GetMessageRequest, ListMessageRequest,
    ReplyMessageRequest, ReplyMessageRequestBody,
)
import requests

from .config import Config
from .messages import ChatMessage, content_text


logger = logging.getLogger(__name__)


class FeishuError(RuntimeError):
    """表示飞书请求失败或历史响应结构异常。

    业务代码使用该异常区分接口问题与其他程序异常。错误文本只保留说明、
    接口路径模板、错误码、日志 ID 和提取出的权限标识，不附带原始响应正文。
    """
    pass


class InvalidSelectionError(ValueError):
    """用户没有引用本群中的合并转发记录，或选中的记录已不可用。"""


@dataclass(frozen=True)
class HistoryResult:
    """保存一次群历史查询的有效消息与扫描状态。

    字段：
        messages：过滤后的用户消息，按消息发送时间升序排列，可能为空；
            当前触发消息是否已被索引不作保证，由业务层补入。
        scan_limited：扫描因页数上限结束且仍有下一页时为 True；
            仅达到配置的消息数量上限不会设置此标志。
    """
    messages: list[ChatMessage]
    scan_limited: bool = False


class FeishuGateway:
    """隔离业务逻辑与飞书 SDK 的请求构造、网络重试和消息序列化。

    实例持有运行配置和 SDK 客户端。历史查询与消息回复均为同步操作，
    应由 SummaryBot 的后台线程调用，避免阻塞长连接的事件处理回调。
    """
    def __init__(self, config: Config, client=None):
        """注入 SDK 客户端，或用应用凭据创建默认客户端。

        参数：
            config：飞书应用凭据、历史窗口等运行配置。
            client：可选的 SDK 兼容客户端；测试可注入模拟客户端。

        默认客户端设置 20 秒请求超时和 WARNING 日志级别。
        此处只构造客户端，实际获取访问凭证在后续 SDK 请求时进行。
        """
        self.config = config
        self.client = client or (
            lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
            .timeout(20).log_level(lark.LogLevel.WARNING).build()
        )
        # 名称只短时缓存在内存，群之间隔离；权限不足的查询也缓存以免逐条重试。
        self._member_names: dict[str, tuple[float, dict[str, str]]] = {}

    @staticmethod
    def _api_error(response, request) -> FeishuError:
        """从 SDK 错误响应生成可安全记录、能定位接口的业务异常。

        参数：
            response：失败的 SDK 响应，可能包含 msg、error.permission_violations。
            request：原始 SDK 请求，仅使用 HTTP 方法与 URI 模板，不读取
                headers、body、queries 或 paths 中的凭据及聊天数据。

        返回：
            FeishuError，保留接口、错误码及日志 ID。230027 权限错误另外提取
            返回消息和权限违规信息中的 im: 权限标识；服务未指明权限时也提供
            群历史读取所需的配置提示。提取的权限可能包含服务给出的任选候选项，
            不将其全部认定为必须同时申请的权限。

        隐私边界：
            不拼接原始 msg、违规描述、完整响应或帮助链接，避免服务回显的
            聊天正文、密钥和查询参数进入日志。该方法不访问网络、不执行重试。
        """
        method = getattr(getattr(request, "http_method", None), "name", "UNKNOWN")
        uri = getattr(request, "uri", None) or "unknown"
        error = getattr(response, "error", None)
        # SDK 版本间 error 可能是普通 dict，也可能是 Error 对象。
        def field(value, name):
            """统一读取两种 SDK 错误结构，不序列化原始响应。"""
            return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

        log_id = response.get_log_id() or field(error, "log_id")
        details = f"飞书 API 失败，api={method} {uri}, code={response.code}, log_id={log_id}"
        if response.code == 230027:
            details += ", reason=权限不足"
            sources = [getattr(response, "msg", None)]
            for violation in field(error, "permission_violations") or []:
                sources.extend((field(violation, "subject"), field(violation, "description")))
            scopes = sorted({
                scope for source in sources if isinstance(source, str)
                for scope in re.findall(r"\bim:[a-zA-Z0-9_.]+(?::[a-zA-Z0-9_.]+)*", source)
            })
            if scopes:
                details += ", reported_scopes=" + ",".join(scopes)
            if method == "GET" and uri in {"/open-apis/im/v1/messages", "/open-apis/im/v1/messages/:message_id"}:
                details += (
                    "；读取群消息需应用身份的 im:message.group_msg，且具备 "
                    "im:message:readonly（或 im:message）。请确认权限已生效；"
                    "需审核的权限完成审批/发布，外部群还需核对机器人对外共享设置。"
                )
        return FeishuError(details)

    @staticmethod
    def _call(operation, request):
        """执行一次 SDK 操作，对指定暂时性失败进行有限重试。

        参数：
            operation：接受 request 并返回 SDK 响应的可调用对象，例如 message.list。
            request：已经构造好的 SDK 请求；重试时复用同一对象。

        返回：
            success() 为 True 的原始 SDK 响应。

        重试与异常：
            最多尝试三次，间隔为 1、2 秒。requests 网络异常、HTTP 429/5xx、
            或业务码 99991400 会重试；最终失败或其他业务错误抛出 FeishuError。
            回复请求必须由调用方提供稳定 UUID，以减少重试产生的重复回复。
            230027 权限错误不会重试；异常中提供接口及安全提取的权限标识。
        """
        # 回复使用稳定 uuid，允许网络超时或限流后的安全重试。
        for attempt in range(3):
            try:
                response = operation(request)
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise FeishuError("飞书网络连接失败或请求超时") from None
            if response.success():
                return response
            status = getattr(getattr(response, "raw", None), "status_code", None)
            if (status == 429 or (status and status >= 500) or response.code == 99991400) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            # 不记录原始 msg/raw，只从权限错误中提取权限标识和固定处理提示。
            raise FeishuGateway._api_error(response, request)
        raise FeishuError("飞书 API 重试次数已耗尽")

    def get_bot_open_id(self) -> str:
        """用应用身份查询机器人的 open_id，不依赖本地人工填写。

        仅在正式启动缺少 BOT_OPEN_ID 时由调用方执行；配置检查不调用此方法。
        接口失败或响应缺少合法 open_id 时抛出 FeishuError，不记录原响应。
        """
        request = (BaseRequest.builder().http_method(HttpMethod.GET)
                   .uri("/open-apis/bot/v3/info").token_types({AccessTokenType.TENANT}).build())
        response = self._call(self.client.request, request)
        try:
            payload = json.loads(response.raw.content)
            bot_id = payload["bot"]["open_id"]
            if not isinstance(bot_id, str) or not bot_id.startswith("ou_") or len(bot_id) <= 3:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise FeishuError("飞书机器人信息响应缺少合法 open_id") from None
        return bot_id

    def get_message(self, message_id: str) -> list[dict]:
        """获取一条消息；合并转发响应同时包含包装消息与其子消息。

        返回普通字典列表，不把合并转发的固定占位正文当成真实聊天记录。
        接口失败或响应结构异常抛出 FeishuError，不读取任何其他群历史。
        """
        builder = GetMessageRequest.builder().message_id(message_id).user_id_type("open_id")
        # 新版 SDK 能直接请求发送者名称；旧版仍可通过群成员接口回退。
        if hasattr(builder, "with_sender_name"):
            builder.with_sender_name(True)
        response = self._call(self.client.im.v1.message.get, builder.build())
        if response.data is None or response.data.items is None:
            raise FeishuError("飞书消息内容响应缺少 items")
        try:
            items = [json.loads(lark.JSON.marshal(item)) for item in response.data.items]
        except (TypeError, ValueError):
            raise FeishuError("飞书消息内容响应结构异常") from None
        if not all(isinstance(item, dict) for item in items):
            raise FeishuError("飞书消息内容响应结构异常")
        return items

    def _group_names(self, chat_id: str) -> dict[str, str]:
        """尽力读取群成员名字；官方 name 字段不等同于独立的群昵称字段。

        名称查询失败不影响正文总结。分页结果和短时失败缓存按群隔离，
        不读取通讯录中的别名 nickname 作为群昵称。
        """
        now = time.monotonic()
        self._member_names = {key: value for key, value in self._member_names.items() if value[0] > now}
        cached = self._member_names.get(chat_id)
        if cached:
            return cached[1]
        names: dict[str, str] = {}
        page_token = None
        seen_tokens = set()
        ttl = 300
        try:
            for _ in range(100):
                builder = GetChatMembersRequest.builder().chat_id(chat_id).member_id_type("open_id").page_size(100)
                if page_token:
                    builder.page_token(page_token)
                response = self._call(self.client.im.v1.chat_members.get, builder.build())
                if response.data is None:
                    raise FeishuError("飞书群成员响应缺少 data")
                for member in response.data.items or []:
                    if member.member_id and member.name:
                        names[member.member_id] = member.name.strip()
                if not response.data.has_more:
                    break
                page_token = response.data.page_token
                if not page_token or page_token in seen_tokens:
                    raise FeishuError("飞书群成员分页游标缺失或重复")
                seen_tokens.add(page_token)
            else:
                logger.warning("群成员名称查询达到分页上限")
        except Exception as exc:
            # 只记录错误类型；正文、姓名、用户标识和第三方响应均不进入日志。
            logger.warning("群成员名称查询不可用，error_type=%s", type(exc).__name__)
            ttl = 60
        self._member_names[chat_id] = (now + ttl, names)
        return names

    def fetch_forwarded(self, trigger: ChatMessage) -> list[ChatMessage]:
        """只展开触发消息明确引用的、本群合并转发中的直接子消息。

        包装消息必须属于当前群；子消息可能保留原会话 ID，因此按
        upper_message_id 判断归属。保留所有所选子消息，不受旧的历史条数、
        时间窗口配置影响。同时间消息维持 API 顺序；嵌套转发仅保留占位。
        错误选择抛 InvalidSelectionError，接口/响应异常抛 FeishuError。
        """
        parent_id = trigger.parent_id
        if not parent_id:
            raise InvalidSelectionError("请引用本群中的合并转发记录，再 @机器人发送“总结”。")
        items = self.get_message(parent_id)
        wrapper = next((item for item in items if item.get("message_id") == parent_id), None)
        if wrapper is None:
            raise FeishuError("飞书消息内容响应缺少被引用的消息")
        if wrapper.get("chat_id") != trigger.chat_id:
            raise InvalidSelectionError("请引用当前群中的合并转发记录。")
        if wrapper.get("msg_type") != "merge_forward" or wrapper.get("deleted"):
            raise InvalidSelectionError("请先多选消息并合并转发到本群，再引用这条合并转发记录 @机器人发送“总结”。")
        children = [item for item in items if item.get("upper_message_id") == parent_id
                    and item.get("message_id") not in {parent_id, trigger.message_id}]
        if not children:
            raise InvalidSelectionError("这条合并转发记录中没有可读取的消息，请重新选择并合并转发。")
        # 服务端姓名优先，只有缺失时才查询本群成员；不为每次总结扫描全群。
        needs_group_names = any(not (item.get("sender") or {}).get("sender_name") for item in children)
        group_names = self._group_names(trigger.chat_id) if needs_group_names else {}
        messages: list[ChatMessage] = []
        seen_ids = set()
        for item in children:
            try:
                message_id, message_type = item["message_id"], item["msg_type"]
                timestamp = int(item["create_time"])
                if not message_id or timestamp < 0 or not message_type:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise FeishuError("合并转发中的子消息缺少必要字段，无法完整总结") from None
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            sender = item.get("sender") or {}
            sender_id = sender.get("id") or "unknown"
            sender_name = (sender.get("sender_name") or "").strip() or group_names.get(sender_id, "")
            if item.get("deleted"):
                text = "[消息已撤回；无法读取内容]"
            else:
                body = item.get("body") or {}
                text = content_text(message_type, body.get("content", ""), item.get("mentions"))
            messages.append(ChatMessage(
                message_id=message_id, chat_id=trigger.chat_id, sender_id=sender_id,
                create_time_ms=timestamp, message_type=message_type, text=text,
                sender_name=sender_name,
            ))
        return sorted(messages, key=lambda message: message.create_time_ms)

    def fetch_history(self, trigger: ChatMessage) -> HistoryResult:
        """分页查询触发消息所在群的近期用户历史，并返回时间正序结果。

        参数：
            trigger：提供群 ID 和查询截止时间的触发消息，时间单位为毫秒。

        返回：
            HistoryResult。过滤机器人、系统、撤回、跨群和窗口外消息后，
            按 message_id 去重，优先保留最新的 history_max_messages 条记录。
            达到 history_max_pages 时保留已获取记录，并设置 scan_limited。

        查询细节：
            API 时间参数以秒为单位，因此结束时间扩展至触发消息所在秒的末尾，
            然后在本地按毫秒排除后续消息。每页 50 条，使用降序查询以先拿到
            最新消息；过滤后数量不足时继续翻页，不因机器人消息占位而提前停止。

        异常：
            FeishuError：请求失败、data 缺失，或下一页游标缺失/重复。
            本方法不吞掉接口错误，避免业务层把查询失败误认为没有历史消息。
        """
        start_ms = max(0, trigger.create_time_ms - int(self.config.history_hours * 3_600_000))
        # API 使用秒，消息使用毫秒。向上取整，再按毫秒过滤同一秒内的后续消息。
        end_seconds = trigger.create_time_ms // 1000 + 1
        messages: dict[str, ChatMessage] = {}
        page_token = None
        seen_tokens = set()
        has_more = False
        for _ in range(self.config.history_max_pages):
            builder = (
                ListMessageRequest.builder().container_id_type("chat")
                .container_id(trigger.chat_id).start_time(str(start_ms // 1000))
                .end_time(str(end_seconds)).sort_type("ByCreateTimeDesc").page_size(50)
            )
            if page_token:
                builder.page_token(page_token)
            response = self._call(self.client.im.v1.message.list, builder.build())
            if response.data is None:
                raise FeishuError("飞书历史消息响应缺少 data")
            for item in response.data.items or []:
                raw = json.loads(lark.JSON.marshal(item))
                message = ChatMessage.from_history(raw)
                if message is None:
                    continue
                if message.chat_id and message.chat_id != trigger.chat_id:
                    continue
                if not start_ms <= message.create_time_ms <= trigger.create_time_ms:
                    continue
                messages[message.message_id] = message
            has_more = bool(response.data.has_more)
            if len(messages) >= self.config.history_max_messages or not has_more:
                break
            page_token = response.data.page_token
            if not page_token or page_token in seen_tokens:
                raise FeishuError("飞书历史消息分页游标缺失或重复")
            seen_tokens.add(page_token)
        else:
            if has_more:
                logger.warning("历史扫描达到页数上限，chat_id=%s", trigger.chat_id)
                return HistoryResult(sorted(messages.values(), key=lambda m: m.create_time_ms), True)
        ordered = sorted(messages.values(), key=lambda m: m.create_time_ms)
        return HistoryResult(ordered[-self.config.history_max_messages:])

    def reply(self, message_id: str, text: str, *, phase: str = "result") -> None:
        """将文本分段引用回复到原消息所在会话，成功时返回 None。

        参数：
            message_id：被回复的飞书消息 ID，决定回复落在哪个群。
            text：完整回复正文；空字符串会替换为空总结提示。
            phase：回复阶段（例如 ack、result），使收到提示和最终结果独立去重。

        发送行为：
            每段最多取 3000 个 Unicode 字符，多段时添加序号并间隔 0.25 秒。
            reply_in_thread=False；每段 UUID 由应用 ID、原消息 ID、阶段和分段编号
            确定，同一回复在重试或恢复时复用，减少重复发送。

        异常：
            FeishuError：某一段发送失败且重试无效；此前成功发送的段不会撤回。
            稳定 UUID 的去重能力受飞书服务端去重窗口限制。
        """
        # 保守控制文本消息大小；按 Unicode 字符切分，不破坏 UTF-8。
        chunks = [text[i:i + 3000] for i in range(0, len(text), 3000)] or ["[总结为空]"]
        for index, chunk in enumerate(chunks):
            if index:
                time.sleep(0.25)
            prefix = f"（{index + 1}/{len(chunks)}）\n" if len(chunks) > 1 else ""
            request = (
                ReplyMessageRequest.builder().message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder().msg_type("text")
                    .content(json.dumps({"text": prefix + chunk}, ensure_ascii=False))
                    .reply_in_thread(False)
                    .uuid(str(uuid5(NAMESPACE_URL, f"summary:{self.config.app_id}:{message_id}:{phase}:{index}")))
                    .build()
                ).build()
            )
            self._call(self.client.im.v1.message.reply, request)
