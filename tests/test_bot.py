"""机器人离线测试：使用虚构凭据、临时数据库和模拟网络验证各模块及完整流程。"""

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import ListMessageResponse, Message, P2ImMessageReceiveV1
import requests

from summary_bot.config import Config, messages_endpoint
from summary_bot.feishu import FeishuError, FeishuGateway, HistoryResult, InvalidSelectionError
from summary_bot.llm import AnthropicMessagesClient, LLMError, SummaryResult
from summary_bot.messages import ChatMessage, content_text
from summary_bot.prompt import InputTooLarge, build_input
from summary_bot.service import SummaryBot
from summary_bot.store import JobStore


NOW = 1_700_000_000_500


def configuration(**changes):
    """返回可直接注入业务对象的测试配置，不加载 .env 或使用真实密钥。

    changes 是需要覆盖的 Config 字段。默认关闭模型额外重试，使失败场景
    可快速、确定地执行；需要验证重试时由相应测试单独覆盖该参数。
    """
    return replace(Config(
        app_id="cli_test_summary", app_secret="test-app-secret",
        llm_base_url="https://llm.example.test", llm_api_key="test-api-key",
        llm_model="test-model", llm_max_retries=0, bot_open_id="ou_summary_bot",
    ), **changes)


def event(message_id="om_new", chat_id="oc_group", text="总结", parent_id="om_forward"):
    """构造一条群内用户文本消息的 event 字典。

    message_id、chat_id 和 text 可独立指定，发送时间固定为 NOW。
    返回结构模拟飞书事件正文，便于测试修改发送者类型、@列表或群聊类型。
    """
    return {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id, "chat_id": chat_id, "chat_type": "group",
            "create_time": str(NOW), "message_type": "text",
            "content": json.dumps({"text": "@_user_1 " + text}, ensure_ascii=False),
            "parent_id": parent_id,
            "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_summary_bot"}, "name": "消息总结"}],
        },
    }


def history_item(message_id="om_old", timestamp=NOW - 1000, **changes):
    """构造历史 API items 中的一条用户消息，返回普通字典。

    message_id 为消息 ID，timestamp 为 Unix 毫秒时间；changes 覆盖任意
    顶层字段，用于构造机器人、撤回、跨群和边界时间等过滤场景。
    """
    item = {
        "message_id": message_id, "chat_id": "oc_group", "create_time": str(timestamp),
        "sender": {"sender_type": "user", "id": "ou_user", "id_type": "open_id"},
        "msg_type": "text", "body": {"content": '{"text":"讨论发布安排"}'},
    }
    item.update(changes)
    return item


def page(items, has_more=False, page_token=None):
    """将历史字典包装为 SDK 兼容的成功分页响应。

    items 会转换为真实 SDK Message 对象，覆盖实际序列化路径；has_more
    和 page_token 模拟下一页状态。返回对象提供 success() 与 data 属性。
    """
    return SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(items=[Message(item) for item in items], has_more=has_more, page_token=page_token),
    )


def sdk_client(pages=()):
    """返回模拟 SDK 客户端与可检查调用记录的 message API 对象。

    pages 指定 list 按次返回的分页响应；reply 默认返回成功响应。
    客户端保持 im.v1.message 的真实访问层级，整个辅助函数不访问网络。
    """
    api = SimpleNamespace(list=Mock(side_effect=pages), reply=Mock(return_value=page([])))
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api))), api


class ConfigTests(unittest.TestCase):
    """验证地址补全、环境变量校验和配置对象的密钥隐藏行为。

    环境读取测试替换 dotenv 并隔离 os.environ，结果不依赖开发者本机配置。
    """
    def test_endpoint_normalization(self):
        """检查根地址、/v1、完整 Messages 路径及代理前缀均能正确补全。

        同时覆盖末尾斜杠，防止拼接出重复的 /v1/messages 路径。
        """
        for base in ("https://a.test", "https://a.test/", "https://a.test/v1", "https://a.test/v1/messages/"):
            with self.subTest(base=base):
                self.assertEqual(messages_endpoint(base), "https://a.test/v1/messages")
        self.assertEqual(messages_endpoint("https://a.test/proxy/v1"), "https://a.test/proxy/v1/messages")

    def test_rejects_unsafe_or_invalid_url(self):
        """对无协议、非 HTTP 协议及包含 URL 凭据/查询参数的地址断言 ValueError。

        验证错误地址在配置阶段被拒绝，而非进入模型网络请求阶段。
        """
        for base in ("a.test", "ftp://a.test", "https://name:secret@a.test", "https://a.test?key=secret"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                messages_endpoint(base)

    def test_missing_config_and_invalid_numeric_values(self):
        """在隔离环境中验证默认值、必填项、数值下限与 mention 模式约束。

        通过 NaN、无穷大、过小预算和负重试次数验证有限性及范围校验；
        配置缺失或 mention 模式缺少机器人 open_id 时均应抛出 ValueError。
        """
        valid = {
            "FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "secret",
            "LLM_BASE_URL": "https://a.test", "LLM_API_KEY": "key", "LLM_MODEL": "model",
        }
        with patch("summary_bot.config.load_dotenv"), patch.dict(os.environ, valid, clear=True):
            self.assertEqual(Config.from_env().history_max_messages, 100)
            for name, value in (("HISTORY_HOURS", "nan"), ("LLM_TIMEOUT_SECONDS", "inf"),
                                ("MAX_INPUT_CHARS", "0"), ("LLM_MAX_RETRIES", "-1")):
                with self.subTest(name=name), patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                    Config.from_env()
            self.assertEqual(Config.from_env().trigger_mode, "mention")
            with patch.dict(os.environ, {"TRIGGER_MODE": "all"}):
                self.assertEqual(Config.from_env().trigger_mode, "mention")
            with patch.dict(os.environ, {"BOT_OPEN_ID": "cli_wrong"}), self.assertRaises(ValueError):
                Config.from_env()
        with patch("summary_bot.config.load_dotenv"), patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            Config.from_env()

    def test_secrets_not_in_config_repr(self):
        """断言 Config 的对象展示不包含飞书应用密钥和模型 API 密钥。

        这里只验证 dataclass 的 repr 行为，不打印或使用实际凭据。
        """
        self.assertNotIn("test-app-secret", repr(configuration()))
        self.assertNotIn("test-api-key", repr(configuration()))


class MessageTests(unittest.TestCase):
    """验证正文提取和历史消息过滤，确保各消息类型具有明确的文本表示。

    所有输入均为本地构造的字典或 JSON，不下载任何附件资源。
    """
    def test_mentions_replaced_without_prefix_collision(self):
        """构造 @_user_1 与 @_user_10 同时出现的正文，检查替换结果。

        预期较长占位符先被处理，两个名称均完整且不会留下尾部数字。
        """
        text = content_text("text", '{"text":"@_user_10 与 @_user_1"}', [
            {"key": "@_user_1", "name": "甲"}, {"key": "@_user_10", "name": "乙"},
        ])
        self.assertEqual(text, "@乙 与 @甲")

    def test_rich_text_direct_and_localized(self):
        """用直接 post 结构和 zh_cn 包装结构验证相同富文本提取结果。

        检查标题、文字、链接及图片占位符，确保提取保留可读信息和段落顺序。
        """
        body = {"title": "进度", "content": [[
            {"tag": "text", "text": "查看"},
            {"tag": "a", "text": "方案", "href": "https://example.test"},
            {"tag": "img", "image_key": "ignored"},
        ]]}
        for content in (body, {"zh_cn": body}):
            with self.subTest(content=content):
                text = content_text("post", json.dumps(content))
                self.assertIn("进度\n查看方案 (https://example.test)[图片]", text)

    def test_attachment_only_has_placeholder(self):
        """验证文件只保留文件名占位，图片只保留类型占位。

        资源密钥不得进入整理后的文本，也不会据附件类型推断附件内容。
        """
        self.assertEqual(content_text("file", '{"file_name":"计划.pdf","file_key":"secret"}'),
                         "[文件：计划.pdf；未读取内容]")
        self.assertNotIn("secret", content_text("image", '{"image_key":"secret"}'))

    def test_malformed_content_is_explicit(self):
        """输入损坏 JSON、数组与 null，检查均得到明确的解析失败提示。

        验证无法解析的正文不会被误认为正常文本或引发本组输入对应的异常。
        """
        for content in ("{broken", "[]", "null"):
            self.assertEqual(content_text("text", content), "[消息内容无法解析]")

    def test_history_ignores_bots_and_recalled_messages(self):
        """构造应用、已撤回与系统历史消息，检查解析结果均为 None。

        这些记录不应被计入用户历史，避免机器人回复再次进入总结上下文。
        """
        self.assertIsNone(ChatMessage.from_history(history_item(sender={"sender_type": "app"})))
        self.assertIsNone(ChatMessage.from_history(history_item(deleted=True)))
        self.assertIsNone(ChatMessage.from_history(history_item(msg_type="system")))


class HistoryTests(unittest.TestCase):
    """验证历史分页、时间窗口、扫描上限及分段回复的请求构造。

    注入模拟 SDK 接口，直接检查真实请求对象中的参数和回复内容。
    """
    def test_paginates_filters_and_uses_seconds(self):
        """用两页混合记录验证过滤后不足数量时继续翻页，且结果按时间升序。

        检查机器人、未来毫秒和其他群的记录被过滤，同时核对秒级查询时间、
        降序请求参数、50 条单页大小及第二页游标。
        """
        client, api = sdk_client([
            page([history_item("om_bot", sender={"sender_type": "app"}),
                  history_item("om_later", NOW + 1), history_item("om_2", NOW - 10)], True, "p2"),
            page([history_item("om_1", NOW - 2000), history_item("om_other", chat_id="oc_other")]),
        ])
        config = configuration(history_max_messages=2)
        result = FeishuGateway(config, client).fetch_history(ChatMessage.from_event(event()))
        self.assertEqual([m.message_id for m in result.messages], ["om_1", "om_2"])
        first, second = [call.args[0] for call in api.list.call_args_list]
        self.assertEqual(first.end_time, str(NOW // 1000 + 1))
        self.assertEqual(first.start_time, str((NOW - 86400000) // 1000))
        self.assertEqual(first.sort_type, "ByCreateTimeDesc")
        self.assertEqual(first.page_size, 50)
        self.assertIsNone(first.page_token)
        self.assertEqual(second.page_token, "p2")

    def test_scan_limit_is_reported(self):
        """将扫描页数限制为一页，并模拟仍有下一页的响应。

        预期返回已有记录且 scan_limited 为 True，供总结提示历史被截取。
        """
        client, _ = sdk_client([page([history_item()], True, "p2")])
        result = FeishuGateway(configuration(history_max_pages=1), client).fetch_history(ChatMessage.from_event(event()))
        self.assertTrue(result.scan_limited)

    def test_repeated_page_token_is_an_error(self):
        """模拟接口连续返回同一分页游标，断言抛出 FeishuError。

        验证分页异常不会造成无限循环或被静默当作完整历史。
        """
        client, _ = sdk_client([page([], True, "same"), page([], True, "same")])
        with self.assertRaises(FeishuError):
            FeishuGateway(configuration(), client).fetch_history(ChatMessage.from_event(event()))

    def test_permission_error_reports_api_and_scope_without_response_body(self):
        """模拟实际 230027 错误，检查接口、缺失权限和日志 ID 可定位。

        使用真实 SDK 响应解析器处理 msg 与权限违规结构；原始消息中的
        私密文字和任意标记不能进入异常，权限错误也不应被重复请求。
        """
        response = ListMessageResponse({
            "code": 230027,
            "msg": "Lack of necessary permissions, ext=requires im:message.group_msg scope. PRIVATE_CHAT",
            "error": {
                "log_id": "test-permission-log",
                "permission_violations": [{
                    "subject": "im:message:readonly",
                    "description": "requires im:message:readonly; PRIVATE_KEY",
                }],
            },
        })
        client, api = sdk_client([response])
        with self.assertRaises(FeishuError) as raised:
            FeishuGateway(configuration(), client).fetch_history(ChatMessage.from_event(event()))
        details = str(raised.exception)
        self.assertIn("api=GET /open-apis/im/v1/messages", details)
        self.assertIn("code=230027", details)
        self.assertIn("log_id=test-permission-log", details)
        self.assertIn("reported_scopes=im:message.group_msg,im:message:readonly", details)
        self.assertNotIn("PRIVATE_CHAT", details)
        self.assertNotIn("PRIVATE_KEY", details)
        api.list.assert_called_once()

    def test_permission_error_without_scope_still_provides_history_guidance(self):
        """服务只返回通用权限错误时，提供群历史权限提示而不虚构具体缺失项。

        reported_scopes 应缺省，固定提示说明需检查应用身份与权限生效情况。
        """
        client, _ = sdk_client([ListMessageResponse({"code": 230027, "msg": "Lack of necessary permissions."})])
        with self.assertRaises(FeishuError) as raised:
            FeishuGateway(configuration(), client).fetch_history(ChatMessage.from_event(event()))
        details = str(raised.exception)
        self.assertIn("应用身份", details)
        self.assertIn("im:message.group_msg", details)
        self.assertNotIn("reported_scopes=", details)

    def test_other_api_errors_do_not_expose_response_message_or_permission_hints(self):
        """普通参数错误只输出接口和状态，不泄漏响应原文或误报为群历史权限不足。

        验证新增权限诊断不会改变其他飞书业务错误的分类与不重试行为。
        """
        client, api = sdk_client([ListMessageResponse({"code": 230001, "msg": "PRIVATE_RESPONSE"})])
        with self.assertRaises(FeishuError) as raised:
            FeishuGateway(configuration(), client).fetch_history(ChatMessage.from_event(event()))
        details = str(raised.exception)
        self.assertIn("code=230001", details)
        self.assertNotIn("PRIVATE_RESPONSE", details)
        self.assertNotIn("应用身份", details)
        api.list.assert_called_once()

    def test_filters_exact_start_time_and_keeps_newest(self):
        """输入多条有效消息及比窗口起点早一毫秒的消息。

        数量上限为两条时，只应保留最新两条有效记录，并按时间正序返回。
        """
        client, _ = sdk_client([page([
            history_item("om_3", NOW - 1), history_item("om_2", NOW - 2),
            history_item("om_1", NOW - 3), history_item("om_early", NOW - 86400001),
        ])])
        result = FeishuGateway(configuration(history_max_messages=2), client).fetch_history(ChatMessage.from_event(event()))
        self.assertEqual([m.message_id for m in result.messages], ["om_2", "om_3"])

    @patch("summary_bot.feishu.time.sleep")
    def test_long_replies_have_stable_distinct_uuids(self, sleep):
        """发送 6001 个中文字符，检查拆成三段且各段 UUID 不同。

        sleep 是 patch 注入的等待替身，使测试不实际停顿。再次发送相同内容
        时，各分段 UUID 应与第一次一致，并使用非话题回复和分段序号。
        """
        client, api = sdk_client()
        gateway = FeishuGateway(configuration(), client)
        gateway.reply("om_new", "中" * 6001)
        first = [call.args[0].request_body for call in api.reply.call_args_list]
        self.assertEqual(len(first), 3)
        self.assertEqual(len({body.uuid for body in first}), 3)
        self.assertTrue(all(len(body.uuid) <= 50 and body.reply_in_thread is False for body in first))
        self.assertIn("（1/3）", json.loads(first[0].content)["text"])
        api.reply.reset_mock()
        gateway.reply("om_new", "中" * 6001)
        self.assertEqual([body.uuid for body in first],
                         [call.args[0].request_body.uuid for call in api.reply.call_args_list])


class PromptTests(unittest.TestCase):
    """所选消息完整覆盖，预算超限须明确拒绝，展示名称不再编号。"""

    def test_selected_messages_are_not_silently_truncated(self):
        """超过单条或总输入预算时，不生成仅含部分记录的总结输入。"""
        messages = [replace(ChatMessage.from_event(event()), message_id=f"om_{i}",
                            create_time_ms=NOW + i, text="中" * 900) for i in range(12)]
        with self.assertRaises(InputTooLarge):
            build_input(messages, configuration(max_input_chars=4000, max_message_chars=1000))
        with self.assertRaises(InputTooLarge):
            build_input([replace(messages[0], text="中" * 1001)], configuration(max_message_chars=1000))

    def test_json_escaping_counts_toward_budget(self):
        """控制字符的 JSON 转义膨胀不能导致发送超出总预算的输入。"""
        message = replace(ChatMessage.from_event(event()), text="\x01" * 900)
        with self.assertRaises(InputTooLarge):
            build_input([message], configuration(max_input_chars=4000))

    def test_sender_names_are_preserved(self):
        """名称直接来自接口，缺失时说明未知，不暴露原始用户ID。"""
        first = replace(ChatMessage.from_event(event()), sender_name="张三")
        result = build_input([first, replace(first, message_id="om_another", sender_name="")], configuration())
        self.assertEqual([m["sender"] for m in json.loads(result.content)["messages"]], ["张三", "姓名未知"])
        self.assertNotIn(first.sender_id, result.content)
        self.assertFalse(result.shortened)


class LLMTests(unittest.TestCase):
    """通过 HTTPX MockTransport 验证 Anthropic 协议和错误处理。

    请求不会离开本机，响应包括成功文本、思考块、限流、认证失败和超时。
    """
    def test_anthropic_request_and_text_blocks(self):
        """核对 Messages 地址、鉴权头、system/user 布局及非流式设置。

        模拟包含 thinking 与两个文本块的响应，预期只拼接文本，并识别
        max_tokens 停止原因，确保群回复不会包含思考块。
        """
        def transport(request):
            """检查传入的 httpx.Request，并返回带多种内容块的模拟 HTTP 200 响应。

            断言失败会直接使测试失败；响应内容用于验证文本过滤及输出截断标志。
            """
            self.assertEqual(str(request.url), "https://llm.example.test/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "test-api-key")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            body = json.loads(request.content)
            self.assertEqual(body["messages"], [{"role": "user", "content": "聊天记录"}])
            self.assertIn("system", body)
            self.assertNotIn("choices", body)
            self.assertFalse(body["stream"])
            return httpx.Response(200, json={
                "type": "message", "content": [
                    {"type": "thinking", "thinking": "不应发到群内"},
                    {"type": "text", "text": "讨论主题"}, {"type": "text", "text": "关键结论"},
                ], "stop_reason": "max_tokens",
            })
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = AnthropicMessagesClient(configuration(), client).summarize("聊天记录")
        self.assertEqual(result.text, "讨论主题\n关键结论")
        self.assertTrue(result.truncated)

    @patch("summary_bot.llm.time.sleep")
    def test_rate_limit_is_retried(self, sleep):
        """依次返回 HTTP 429 和成功响应，检查允许一次重试时共发出两次请求。

        sleep 是注入的等待替身，避免测试按真实退避时间停顿。
        """
        transport = Mock(side_effect=[httpx.Response(429, headers={"retry-after": "0"}),
                                     httpx.Response(200, json={"content": [{"type": "text", "text": "总结"}]})])
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = AnthropicMessagesClient(configuration(llm_max_retries=1), client).summarize("data")
        self.assertEqual(result.text, "总结")
        self.assertEqual(transport.call_count, 2)

    def test_authentication_failure_does_not_retry_or_leak_body(self):
        """模拟带敏感正文的 HTTP 401，检查只请求一次并抛出 LLMError。

        异常说明不得包含模拟服务回显的私密内容，认证错误也不应消耗重试次数。
        """
        transport = Mock(return_value=httpx.Response(401, text="echoed secret and private chat"))
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(LLMError) as raised:
                AnthropicMessagesClient(configuration(llm_max_retries=2), client).summarize("data")
        self.assertEqual(transport.call_count, 1)
        self.assertNotIn("private", str(raised.exception))

    def test_malformed_and_empty_responses_fail(self):
        """逐一模拟错误对象、空内容、仅思考块及不兼容协议响应。

        所有场景均应抛出 LLMError，避免将空总结或 OpenAI choices 误当成结果。
        """
        for payload in ({"choices": []}, {"content": []}, {"content": [{"type": "thinking"}]},
                        {"type": "error"}, []):
            with self.subTest(payload=payload):
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
                    with self.assertRaises(LLMError):
                        AnthropicMessagesClient(configuration(), client).summarize("data")

    def test_timeout_is_reported_without_request_text(self):
        """模拟读取超时，检查转换后的 LLMError 不包含原始异常的敏感细节。

        本场景使用零额外重试，确保错误路径可立即验证。
        """
        def transport(request):
            """接收模拟请求并抛出关联该请求的 ReadTimeout，不执行网络操作。

            异常附带的 sensitive details 用于检查业务层错误是否隔离原始细节。
            """
            raise httpx.ReadTimeout("sensitive details", request=request)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(LLMError) as raised:
                AnthropicMessagesClient(configuration(), client).summarize("data")
        self.assertNotIn("sensitive", str(raised.exception))


class ServiceTests(unittest.TestCase):
    """验证群@、请求路由、双阶段回复、恢复及独立回执的完整行为。"""

    def setUp(self):
        """使用临时队列与虚构接口，不加载任何本地凭据。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = configuration(state_db_path=Path(self.temp.name) / "state.db")
        self.store = JobStore(self.config.state_db_path)
        self.feishu = Mock()
        self.selected = replace(ChatMessage.from_history(history_item()), sender_name="张三")
        self.feishu.fetch_forwarded.return_value = [self.selected]
        self.llm = Mock()
        self.llm.summarize.return_value = SummaryResult("关键结论：周五发布。")
        self.bot = SummaryBot(self.config, self.feishu, self.llm, self.store)

    def process(self, summary=True):
        """同步校验请求，只有有效总结才继续交给模型线程。"""
        self.assertTrue(self.bot.process_ack_next())
        self.assertEqual(self.bot.process_next(), summary)

    def test_dedup_and_only_selected_content(self):
        """重复事件只发一对回复；只把明确选中的消息送入模型。"""
        self.assertTrue(self.bot.submit(event()))
        self.assertFalse(self.bot.submit(event()))
        self.assertFalse(self.bot.process_next())
        self.process()
        self.assertFalse(self.bot.process_ack_next())
        self.assertFalse(self.bot.process_next())
        content = json.loads(self.llm.summarize.call_args.args[0])
        self.assertEqual([m["message_id"] for m in content["messages"]], ["om_old"])
        self.feishu.fetch_history.assert_not_called()
        self.assertEqual(self.feishu.reply.call_count, 2)
        receipt, result = self.feishu.reply.call_args_list
        self.assertEqual(receipt.args, ("om_new", "收到，正在处理"))
        self.assertEqual(receipt.kwargs, {"phase": "receipt"})
        self.assertEqual(result.kwargs, {"phase": "result"})
        self.assertIn("周五发布", result.args[1])

    def test_summary_reply_uses_time_label_and_bracket_headings(self):
        """完整回复使用时间标签和四个方括号栏目，保留编号与项目符号正文。"""
        body = (
            "**讨论主题**\n发布进展。\n\n"
            "**关键结论**\n1. 周五发布。\n\n"
            "**待办事项**\n* 张三负责验收。\n\n"
            "**待确认问题**\n* 上线时间待确认。"
        )
        self.llm.summarize.return_value = SummaryResult(body)
        self.bot.submit(event())
        self.process()
        reply = self.feishu.reply.call_args.args[1]
        self.assertEqual(reply.splitlines()[0], "所选聊天记录总结｜1 条消息")
        self.assertTrue(reply.splitlines()[1].startswith("【时间】"))
        self.assertIn("（Asia/Shanghai）", reply.splitlines()[1])
        for heading in ("讨论主题", "关键结论", "待办事项", "待确认问题"):
            self.assertIn(f"【{heading}】\n", reply)
            self.assertNotIn(f"**{heading}**", reply)
        self.assertIn("1. 周五发布。", reply)
        self.assertIn("* 张三负责验收。", reply)
        self.assertIn("* 上线时间待确认。", reply)

    def test_bots_private_messages_and_system_events_are_ignored(self):
        """机器人、私聊及系统事件不会入队，也不发送回执。"""
        for sender in ("bot", "app"):
            data = event()
            data["sender"]["sender_type"] = sender
            self.assertFalse(self.bot.submit(data))
        data = event()
        data["message"]["chat_type"] = "p2p"
        self.assertFalse(self.bot.submit(data))
        data = event()
        data["message"]["message_type"] = "system"
        self.assertFalse(self.bot.submit(data))
        self.assertFalse(self.bot.submit({}))
        self.assertFalse(self.bot.process_ack_next())
        self.feishu.reply.assert_not_called()

    def test_mentions_only_match_current_bot_even_with_legacy_all_mode(self):
        """历史 all 配置不放宽要求，无@或@别人都忽略。"""
        self.bot.config = replace(self.config, trigger_mode="all")
        data = event()
        data["message"]["mentions"] = []
        self.assertFalse(self.bot.submit(data))
        data["message"]["mentions"] = [{"id": {"open_id": "ou_someone_else"}}]
        self.assertFalse(self.bot.submit(data))
        self.assertTrue(self.bot.submit(event()))

    def test_missing_bot_identity_fails_closed(self):
        """未解析机器人身份时，不根据文本猜测被@对象。"""
        self.bot.config = replace(self.config, bot_open_id="")
        self.assertFalse(self.bot.submit(event()))

    def test_group_allowlist(self):
        """白名单之外不发回执也不处理。"""
        self.bot.config = replace(self.config, allowed_chat_ids=frozenset({"oc_allowed"}))
        self.assertFalse(self.bot.submit(event()))
        self.assertTrue(self.bot.submit(event(chat_id="oc_allowed")))

    def test_unknown_keywords_show_menu_without_model_or_message_fetch(self):
        """否定、功能询问、无关文字或空指令都回菜单。"""
        for index, keyword in enumerate(("不要总结", "取消总结", "总结功能怎么用", "总结\n额外要求", "帮助", "")):
            with self.subTest(keyword=keyword):
                self.bot.submit(event(f"om_menu_{index}", text=keyword))
                self.process(summary=False)
                self.assertEqual(self.feishu.reply.call_count, index + 1)
                reply = self.feishu.reply.call_args
                self.assertEqual(reply.kwargs, {"phase": "result"})
                self.assertNotIn("收到，正在处理", reply.args[1])
                self.assertIn("功能菜单", reply.args[1])
                self.assertIn("合并转发", reply.args[1])
        self.feishu.fetch_forwarded.assert_not_called()
        self.llm.summarize.assert_not_called()

    def test_missing_reference_gives_usage_without_receipt(self):
        """识别到总结但缺少引用时直接提示，不发回执、不读其他历史。"""
        self.bot.submit(event(parent_id=""))
        self.process(summary=False)
        self.assertEqual(self.feishu.reply.call_count, 1)
        self.assertIn("请先引用", self.feishu.reply.call_args.args[1])
        self.assertIn("多选", self.feishu.reply.call_args.args[1])
        self.feishu.fetch_forwarded.assert_not_called()
        self.llm.summarize.assert_not_called()

    def test_ordinary_reference_gives_usage(self):
        """普通单条消息不能被当作合并转发记录或触发历史总结。"""
        self.feishu.fetch_forwarded.side_effect = InvalidSelectionError("不是合并转发")
        self.bot.submit(event())
        self.process(summary=False)
        self.assertEqual(self.feishu.reply.call_count, 1)
        self.assertIn("请引用一条本群中的合并转发记录", self.feishu.reply.call_args.args[1])
        self.llm.summarize.assert_not_called()

    def test_validation_precedes_receipt_and_model(self):
        """先读取校验材料，通过后才发回执、调用模型并发结果。"""
        order = []
        self.feishu.reply.side_effect = lambda *args, phase: order.append(phase)
        self.feishu.fetch_forwarded.side_effect = lambda trigger: order.append("fetch") or [self.selected]
        self.llm.summarize.side_effect = lambda text: order.append("model") or SummaryResult("结论")
        self.bot.submit(event())
        self.process()
        self.assertEqual(order, ["fetch", "receipt", "model", "result"])
        self.feishu.fetch_forwarded.assert_called_once()

    def test_receipt_failure_blocks_processing_and_recovers(self):
        """回执没发成功时不启动功能；重启后先重新发回执。"""
        self.feishu.reply.side_effect = FeishuError("回执网络失败")
        self.bot.submit(event())
        self.assertTrue(self.bot.process_ack_next())
        self.assertFalse(self.bot.process_next())
        self.llm.summarize.assert_not_called()
        self.feishu.reply.side_effect = None
        restarted = SummaryBot(self.config, self.feishu, self.llm, JobStore(self.config.state_db_path))
        self.assertTrue(restarted.process_ack_next())
        self.assertTrue(restarted.process_next())
        self.assertEqual(self.llm.summarize.call_count, 1)

    def test_read_failure_does_not_summarize_partial_data(self):
        """消息权限失败时不把命令或其他历史冒充总结材料。"""
        self.feishu.fetch_forwarded.side_effect = FeishuError("权限不足")
        self.bot.submit(event())
        self.process(summary=False)
        self.assertEqual(self.feishu.reply.call_count, 1)
        self.llm.summarize.assert_not_called()
        self.assertIn("暂时失败", self.feishu.reply.call_args.args[1])
        self.feishu.fetch_history.assert_not_called()

    def test_oversized_selection_is_explained_without_model_call(self):
        """超预算明确提示拆成多份转发记录，不静默丢弃消息。"""
        self.feishu.fetch_forwarded.return_value = [replace(self.selected, text="中" * 2001)]
        self.bot.submit(event())
        self.process(summary=False)
        self.assertEqual(self.feishu.reply.call_count, 1)
        self.assertIn("过长", self.feishu.reply.call_args.args[1])
        self.assertIn("拆分", self.feishu.reply.call_args.args[1])
        self.llm.summarize.assert_not_called()

    def test_llm_failure_returns_safe_error(self):
        """模型错误发简短提示，群内不显示密钥或原始响应。"""
        self.llm.summarize.side_effect = LLMError("LLM 请求失败，HTTP 401")
        self.bot.submit(event())
        self.process()
        reply = self.feishu.reply.call_args.args[1]
        self.assertIn("暂时失败", reply)
        self.assertNotIn("test-api-key", reply)

    def test_restart_reuses_saved_summary_without_repeat_receipt(self):
        """回执和结果已落盘后重启，只恢复未完成的结果发送。"""
        self.bot.submit(event())
        self.bot.process_ack_next()
        job = self.store.next_job()
        self.store.save_reply(job.message.message_id, "已经生成的总结")
        resumed = JobStore(self.config.state_db_path)
        bot = SummaryBot(self.config, self.feishu, self.llm, resumed)
        self.assertFalse(bot.process_ack_next())
        self.assertTrue(bot.process_next())
        self.llm.summarize.assert_not_called()
        self.feishu.reply.assert_called_with("om_new", "已经生成的总结", phase="result")
        self.assertEqual(self.feishu.reply.call_count, 2)

    def test_failed_result_delivery_resumes_without_calling_llm(self):
        """结果发送失败后保留生成文本，重启不重复调用模型。"""
        self.bot.submit(event())
        self.bot.process_ack_next()
        self.feishu.reply.side_effect = FeishuError("回复网络失败")
        self.bot.process_next()
        self.feishu.reply.side_effect = None
        self.llm.reset_mock()
        restarted = SummaryBot(self.config, self.feishu, self.llm, JobStore(self.config.state_db_path))
        self.assertFalse(restarted.process_ack_next())
        self.assertTrue(restarted.process_next())
        self.llm.summarize.assert_not_called()
        self.assertIn("周五发布", self.feishu.reply.call_args.args[1])

    def test_receipt_for_next_request_does_not_wait_for_blocked_model(self):
        """第一条总结阻塞时，第二个有效总结仍及时发回执。"""
        model_started, release_model, second_receipt = Event(), Event(), Event()

        def summarize(text):
            model_started.set()
            if not release_model.wait(timeout=4):
                raise AssertionError("测试未释放模型")
            return SummaryResult("结论")

        def reply(message_id, text, *, phase):
            if message_id == "om_second" and phase == "receipt":
                second_receipt.set()

        self.llm.summarize.side_effect = summarize
        self.feishu.reply.side_effect = reply
        self.bot.submit(event())
        self.bot.start()
        try:
            self.assertTrue(model_started.wait(timeout=3))
            self.bot.submit(event("om_second"))
            self.assertTrue(second_receipt.wait(timeout=2))
            self.assertFalse(release_model.is_set())
        finally:
            release_model.set()
            self.bot.close()

    def test_completed_job_discards_content_but_remembers_id(self):
        """完成后清空正文与回复，仍按消息ID去重。"""
        self.bot.submit(event())
        self.process()
        with closing(sqlite3.connect(self.config.state_db_path)) as db, db:
            row = db.execute("SELECT payload, reply_text, status FROM jobs").fetchone()
        self.assertEqual(row, ("", None, "done"))
        self.assertFalse(JobStore(self.config.state_db_path).enqueue(ChatMessage.from_event(event())))

    def test_fifo_keeps_replies_in_the_correct_group(self):
        """不同群的请求只按各自引用定位并回复，不混入其他群历史。"""
        self.bot.submit(event("om_a", "oc_a"))
        self.bot.submit(event("om_b", "oc_b"))
        self.process()
        self.process()
        self.assertEqual([call.args[0].chat_id for call in self.feishu.fetch_forwarded.call_args_list], ["oc_a", "oc_b"])
        self.assertEqual([call.args[0] for call in self.feishu.reply.call_args_list], ["om_a", "om_a", "om_b", "om_b"])

    def test_legacy_pending_tasks_are_preserved_without_executing(self):
        """旧版未保存@依据，升级后保留旧队列但不自动发历史总结。"""
        legacy_path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute("CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, "
                       "payload TEXT NOT NULL, reply_text TEXT, status TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL)")
            db.execute("INSERT INTO jobs(message_id,payload,status,updated_at) VALUES ('old','{}','pending',1)")
        migrated = JobStore(legacy_path)
        self.assertIsNone(migrated.next_ack())
        self.assertIsNone(migrated.next_job())
        with closing(sqlite3.connect(legacy_path)) as db, db:
            self.assertEqual(db.execute("SELECT status,payload FROM jobs").fetchone(), ("legacy", "{}"))

    def test_real_sdk_and_anthropic_wire_format_end_to_end(self):
        """使用真实SDK构造事件、展开合并转发、发回执/结果，网络全部拦截。"""
        outgoing = []
        wrapper = history_item("om_forward", msg_type="merge_forward", body={"content": '{"content":"Merged and Forwarded Message"}'})
        selected = history_item(upper_message_id="om_forward", sender={
            "sender_type": "user", "id": "ou_user", "id_type": "open_id", "sender_name": "张三",
        })

        def feishu_transport(method, url, **kwargs):
            if url.endswith("/auth/v3/tenant_access_token/internal"):
                body = {"code": 0, "tenant_access_token": "fake-token", "expire": 7200}
            elif url.endswith("/im/v1/messages/om_forward"):
                self.assertEqual(method, "GET")
                body = {"code": 0, "data": {"items": [wrapper, selected]}}
            elif url.endswith("/im/v1/messages/om_new/reply"):
                outgoing.append(json.loads(kwargs["data"]))
                body = {"code": 0, "data": {"message_id": "om_reply"}}
            else:
                self.fail(f"Unexpected Feishu URL: {url}")
            response = requests.Response()
            response.status_code = 200
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(body).encode()
            return response

        def llm_transport(request):
            payload = json.loads(request.content)
            records = json.loads(payload["messages"][0]["content"])["messages"]
            self.assertEqual([r["message_id"] for r in records], ["om_old"])
            self.assertEqual(records[0]["sender"], "张三")
            return httpx.Response(200, json={"type": "message", "content": [
                {"type": "text", "text": "关键结论：周五发布，张三负责验收。"},
            ], "stop_reason": "end_turn"})

        with httpx.Client(transport=httpx.MockTransport(llm_transport)) as client:
            bot = SummaryBot(self.config, FeishuGateway(self.config),
                             AnthropicMessagesClient(self.config, client), self.store)
            data = lark.JSON.unmarshal(json.dumps({"schema": "2.0", "event": event()}), P2ImMessageReceiveV1)
            with patch("requests.request", side_effect=feishu_transport):
                self.assertTrue(bot.submit(json.loads(lark.JSON.marshal(data.event))))
                self.assertTrue(bot.process_ack_next())
                self.assertTrue(bot.process_next())
        self.assertEqual(len(outgoing), 2)
        self.assertEqual(outgoing[0]["msg_type"], "text")
        self.assertEqual(json.loads(outgoing[0]["content"])["text"], "收到，正在处理")
        self.assertIn("1 条消息", json.loads(outgoing[1]["content"])["text"])
        self.assertNotEqual(outgoing[0]["uuid"], outgoing[1]["uuid"])


if __name__ == "__main__":
    unittest.main()
