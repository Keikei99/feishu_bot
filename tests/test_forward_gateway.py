"""合并转发网关的离线测试，全部请求由模拟 SDK 接口接管。"""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from lark_oapi.api.im.v1 import ListMember, Message
from lark_oapi.core.enum import AccessTokenType, HttpMethod

from summary_bot.config import Config
from summary_bot.feishu import FeishuError, FeishuGateway, InvalidSelectionError
from summary_bot.messages import ChatMessage


def configuration(**changes):
    """直接构造虚构配置，不加载本地配置文件或真实凭据。"""
    return replace(Config(
        app_id="cli_offline", app_secret="fake-secret", llm_base_url="https://llm.example.test",
        llm_api_key="fake-key", llm_model="offline-model",
    ), **changes)


def selected_message(message_id="om_child", *, time=1000, **changes):
    """构造一条属于选中转发记录的子消息。"""
    result = {
        "message_id": message_id, "chat_id": "oc_original", "create_time": time,
        "upper_message_id": "om_forward", "msg_type": "text",
        "sender": {"id": "ou_alice", "id_type": "open_id", "sender_type": "user", "sender_name": "小明"},
        "body": {"content": json.dumps({"text": message_id})},
    }
    result.update(changes)
    return result


def wrapper(**changes):
    """构造本群中的合并转发包装消息。"""
    result = selected_message("om_forward", msg_type="merge_forward", chat_id="oc_group", upper_message_id=None)
    result.update(changes)
    return result


def trigger(**changes):
    """构造引用合并转发的总结命令，不把命令文本作为总结材料。"""
    return replace(ChatMessage("om_command", "oc_group", "ou_requester", 9000, "text", "总结",
                               parent_id="om_forward"), **changes)


def response(**data):
    """构造成功的 SDK 风格响应。"""
    return SimpleNamespace(success=lambda: True, data=SimpleNamespace(**data))


def gateway(items=(), *, members=()):
    """创建仅由 Mock 接口组成的网关，并返回可检查调用的客户端。"""
    message_api = SimpleNamespace(
        get=Mock(return_value=response(items=[Message(item) for item in items])),
        list=Mock(side_effect=AssertionError("合并转发工作流不得读取群历史")),
        reply=Mock(return_value=response()),
    )
    members_api = SimpleNamespace(get=Mock(return_value=response(
        items=[ListMember(item) for item in members], has_more=False,
    )))
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api, chat_members=members_api)),
        request=Mock(),
    )
    return FeishuGateway(configuration(history_max_messages=1), client), client


class ForwardSelectionTests(unittest.TestCase):
    """验证总结内容严格来自用户明确引用的合并转发记录。"""

    def test_expands_only_selected_wrapper_and_preserves_all_messages(self):
        """超过旧历史条数上限仍取全，并排除外层包装、旁支和总结命令。"""
        children = [selected_message(f"om_{number}", time=number) for number in range(125)]
        bot, client = gateway([
            wrapper(), *children,
            selected_message("om_unrelated", upper_message_id="om_other"),
            selected_message("om_command"),
        ])
        records = bot.fetch_forwarded(trigger())
        self.assertEqual(len(records), 125)
        self.assertEqual(records[0].message_id, "om_0")
        self.assertEqual(records[-1].message_id, "om_124")
        self.assertTrue(all(record.chat_id == "oc_group" for record in records))
        self.assertNotIn("总结", [record.text for record in records])
        client.im.v1.message.list.assert_not_called()
        request = client.im.v1.message.get.call_args.args[0]
        self.assertEqual(request.paths["message_id"], "om_forward")
        self.assertIn(("user_id_type", "open_id"), request.queries)

    def test_stable_time_order_and_duplicate_message_ids(self):
        """按发送时间排列；同时间保持 API 顺序，重复消息 ID 只保留一次。"""
        bot, _ = gateway([wrapper(), selected_message("om_late", time=3000),
                          selected_message("om_equal_1", time=2000), selected_message("om_first", time=1000),
                          selected_message("om_equal_2", time=2000), selected_message("om_first", time=1000)])
        self.assertEqual([item.message_id for item in bot.fetch_forwarded(trigger())],
                         ["om_first", "om_equal_1", "om_equal_2", "om_late"])

    def test_missing_reference_ordinary_reference_and_other_group_are_rejected(self):
        """缺引用、普通消息引用、跨群包装都给用法错误，不读其他内容兜底。"""
        bot, client = gateway([wrapper(), selected_message()])
        with self.assertRaises(InvalidSelectionError):
            bot.fetch_forwarded(trigger(parent_id=""))
        client.im.v1.message.get.assert_not_called()
        for invalid in (wrapper(msg_type="text"), wrapper(chat_id="oc_other"), wrapper(deleted=True)):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidSelectionError):
                bot, client = gateway([invalid, selected_message()])
                bot.fetch_forwarded(trigger())
            client.im.v1.chat_members.get.assert_not_called()

    def test_missing_or_empty_selection_is_not_a_history_summary(self):
        """包装缺失是接口异常，空记录是无效选择，两者都不会调用群历史。"""
        for items, expected_error in (([], FeishuError), ([wrapper()], InvalidSelectionError)):
            with self.subTest(items=items), self.assertRaises(expected_error):
                bot, client = gateway(items)
                bot.fetch_forwarded(trigger())
            client.im.v1.message.list.assert_not_called()

    def test_non_text_deleted_and_nested_forward_are_explicit_placeholders(self):
        """附件、撤回、嵌套转发只占位，选中的应用消息也保留。"""
        bot, client = gateway([
            wrapper(), selected_message("om_image", msg_type="image", body={"content": '{"image_key":"fake"}'}),
            selected_message("om_deleted", deleted=True),
            selected_message("om_nested", msg_type="merge_forward"),
            selected_message("om_grandchild", upper_message_id="om_nested"),
            selected_message("om_app", sender={"id": "cli_app", "sender_type": "app", "sender_name": "其他机器人"}),
        ])
        records = bot.fetch_forwarded(trigger())
        self.assertEqual(len(records), 4)
        self.assertIn("图片", records[0].text)
        self.assertIn("已撤回", records[1].text)
        self.assertIn("合并转发", records[2].text)
        self.assertEqual(records[3].sender_name, "其他机器人")
        client.im.v1.message.get.assert_called_once()

    def test_malformed_child_is_an_error_instead_of_silent_omission(self):
        """必要字段缺失时停止，避免把残缺的选中记录宣称为完整总结。"""
        bot, _ = gateway([wrapper(), selected_message(create_time=None)])
        with self.assertRaises(FeishuError):
            bot.fetch_forwarded(trigger())

    def test_get_message_api_failure_and_missing_data_remain_api_errors(self):
        """接口失败不误分类为选错消息，也不伪装成空记录。"""
        bot, client = gateway()
        client.im.v1.message.get.return_value = SimpleNamespace(
            success=lambda: False, code=230027, msg="missing permissions",
            get_log_id=lambda: "offline-log", error=None,
        )
        with self.assertRaises(FeishuError):
            bot.fetch_forwarded(trigger())
        for bad_response in (SimpleNamespace(success=lambda: True, data=None), response(items=None)):
            client.im.v1.message.get.return_value = bad_response
            with self.assertRaises(FeishuError):
                bot.fetch_forwarded(trigger())


class SenderNameTests(unittest.TestCase):
    """名称查询是可降级的展示能力，不影响消息材料的正确读取。"""

    def test_server_name_avoids_member_and_contact_lookups(self):
        """优先使用消息接口姓名，缺省不查询通讯录。"""
        bot, client = gateway([wrapper(), selected_message()])
        self.assertEqual(bot.fetch_forwarded(trigger())[0].sender_name, "小明")
        client.im.v1.chat_members.get.assert_not_called()
        self.assertFalse(hasattr(client, "contact"))

    def test_member_names_are_fallback_and_cached_per_group(self):
        """缺名时查询群成员；成员名只作显示姓名，群间缓存互不混用。"""
        child = selected_message(sender={"id": "ou_alice", "sender_type": "user"})
        bot, client = gateway([wrapper(), child], members=[{"member_id": "ou_alice", "name": "成员名称"}])
        self.assertEqual(bot.fetch_forwarded(trigger())[0].sender_name, "成员名称")
        bot.fetch_forwarded(trigger())
        self.assertEqual(client.im.v1.chat_members.get.call_count, 1)
        client.im.v1.message.get.return_value = response(items=[Message(wrapper(chat_id="oc_second")), Message(child)])
        client.im.v1.chat_members.get.return_value = response(
            items=[ListMember({"member_id": "ou_alice", "name": "第二群名称"})], has_more=False,
        )
        self.assertEqual(bot.fetch_forwarded(trigger(chat_id="oc_second"))[0].sender_name, "第二群名称")
        self.assertEqual(client.im.v1.chat_members.get.call_count, 2)

    def test_server_name_wins_when_other_sender_needs_member_lookup(self):
        """同批缺名触发成员查询，也不能覆盖另一条消息已有的服务端姓名。"""
        bot, _ = gateway([wrapper(), selected_message(), selected_message("om_bob", sender={"id": "ou_bob", "sender_type": "user"})],
                         members=[{"member_id": "ou_alice", "name": "账户名称"}, {"member_id": "ou_bob", "name": "小红"}])
        self.assertEqual([item.sender_name for item in bot.fetch_forwarded(trigger())], ["小明", "小红"])

    def test_member_pagination_and_failed_name_lookup_do_not_hide_body(self):
        """成员翻页可找到姓名，名称查询失败时仍可总结正文且短时不重试。"""
        child = selected_message(sender={"id": "ou_alice", "sender_type": "user"})
        bot, client = gateway([wrapper(), child])
        client.im.v1.chat_members.get.side_effect = [
            response(items=[], has_more=True, page_token="next"),
            response(items=[ListMember({"member_id": "ou_alice", "name": "小明"})], has_more=False),
        ]
        self.assertEqual(bot.fetch_forwarded(trigger())[0].sender_name, "小明")
        self.assertIn(("page_token", "next"), client.im.v1.chat_members.get.call_args.args[0].queries)
        bot, client = gateway([wrapper(), child])
        client.im.v1.chat_members.get.side_effect = FeishuError("权限不足")
        records = bot.fetch_forwarded(trigger())
        self.assertEqual(records[0].text, "om_child")
        self.assertEqual(records[0].sender_name, "")
        bot.fetch_forwarded(trigger())
        self.assertEqual(client.im.v1.chat_members.get.call_count, 1)
        self.assertFalse(hasattr(client, "contact"))


class ReplyAndBotIdentityTests(unittest.TestCase):
    """验证两阶段回复的去重标识与启动身份查询。"""

    def test_ack_result_have_independent_stable_uuids(self):
        """收到提示和最终结果互不去重，同一阶段重试仍复用 UUID。"""
        bot, client = gateway()
        bot.reply("om_trigger", "收到，正在处理", phase="ack")
        bot.reply("om_trigger", "总结结果")
        bot.reply("om_trigger", "收到，正在处理", phase="ack")
        ids = [call.args[0].body.uuid for call in client.im.v1.message.reply.call_args_list]
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(ids[0], ids[2])

    def test_get_bot_open_id_uses_tenant_identity_and_validates_payload(self):
        """启动查询只使用应用身份；无合法 open_id 时给清晰接口异常。"""
        bot, client = gateway()
        client.request.return_value = SimpleNamespace(
            success=lambda: True, raw=SimpleNamespace(content=b'{"code":0,"bot":{"open_id":"ou_bot"}}'),
        )
        self.assertEqual(bot.get_bot_open_id(), "ou_bot")
        request = client.request.call_args.args[0]
        self.assertEqual(request.http_method, HttpMethod.GET)
        self.assertEqual(request.uri, "/open-apis/bot/v3/info")
        self.assertEqual(request.token_types, {AccessTokenType.TENANT})
        for content in (b'{"bot":{}}', b'{"bot":{"open_id":"cli_app"}}', b"not-json"):
            with self.subTest(content=content), self.assertRaises(FeishuError):
                client.request.return_value.raw.content = content
                bot.get_bot_open_id()


if __name__ == "__main__":
    unittest.main()
