"""验证引用元数据、严格指令和选中消息完整输入，不读取配置文件或访问网络。"""

from dataclasses import replace
import json
import unittest

from summary_bot.config import Config
from summary_bot.messages import ChatMessage, content_text, extract_command
from summary_bot.prompt import InputTooLarge, SYSTEM_PROMPT, build_input


BOT_ID = "ou_summary_bot"


def config(**changes):
    """用虚构凭据直接构造配置，不调用 from_env。"""
    return replace(Config(
        app_id="cli_unit", app_secret="unit-secret", llm_base_url="https://unit.test",
        llm_api_key="unit-key", llm_model="unit-model",
    ), **changes)


def message_event(text="@_user_1 总结", **changes):
    """构造包含当前机器人 @ 和引用目标的用户消息。"""
    message = {
        "message_id": "om_command", "chat_id": "oc_unit", "chat_type": "group",
        "create_time": "1700000000000", "message_type": "text",
        "parent_id": "om_forward", "content": json.dumps({"text": text}),
        "mentions": [{"key": "@_user_1", "id": {"open_id": BOT_ID}, "name": "总结助手"}],
    }
    message.update(changes)
    return {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_unit"}},
        "message": message,
    }


def selected_message(**changes):
    """构造有展示姓名的合并转发子消息。"""
    return replace(ChatMessage(
        message_id="om_selected", chat_id="oc_unit", sender_id="ou_unit",
        create_time_ms=1700000000000, message_type="text", text="周五发布，由小林验收。",
        sender_name="小林",
    ), **changes)


class ExactCommandTests(unittest.TestCase):
    """只剥离当前机器人的 @，严格保留其他内容。"""

    def test_event_preserves_reference_and_extracts_command(self):
        """引用 ID 和解析后的指令随任务保存，正文仍保留可见 @ 姓名。"""
        message = ChatMessage.from_event(message_event(" \n@_user_1  总结\t"), BOT_ID)
        self.assertEqual(message.parent_id, "om_forward")
        self.assertEqual(message.command, "总结")
        self.assertIn("@总结助手", message.text)
        self.assertEqual(ChatMessage.from_event(message_event()).command, "")

    def test_extra_words_punctuation_and_internal_whitespace_do_not_match(self):
        """自然语言变体、标点和指令内部空格不会变成严格的总结指令。"""
        for text in ("@_user_1 帮我总结一下", "@_user_1 总结重点", "@_user_1 总结。",
                     "@_user_1 总 结", "@_user_1 总\n结", "@_user_1 总结\n额外内容"):
            with self.subTest(text=text):
                self.assertNotEqual(extract_command(message_event(text)["message"], BOT_ID), "总结")

    def test_other_mentions_and_prefix_collisions_are_preserved(self):
        """不能把其他人的 @ 或更长的占位符一同删去。"""
        event = message_event("@_user_1 @_user_10 总结")
        event["message"]["mentions"].append({
            "key": "@_user_10", "id": {"open_id": "ou_other"}, "name": "小王",
        })
        command = extract_command(event["message"], BOT_ID)
        self.assertEqual(command, "@_user_10 总结")
        self.assertNotEqual(command, "总结")

    def test_same_display_name_does_not_remove_other_person(self):
        """按用户 ID 而非显示名确定应移除的 @。"""
        event = message_event("@_user_2 总结")
        event["message"]["mentions"] = [{
            "key": "@_user_2", "id": {"open_id": "ou_other"}, "name": "总结助手",
        }]
        self.assertEqual(extract_command(event["message"], BOT_ID), "@_user_2 总结")

    def test_plain_and_multilingual_post_commands(self):
        """正文中的机器人 at 节点可被移除，兼容直接与多语言 post。"""
        post = {"title": "", "content": [[
            {"tag": "at", "user_id": BOT_ID}, {"tag": "text", "text": " 总结 "},
        ]]}
        for body in (post, {"zh_cn": post}):
            with self.subTest(body=body):
                event = message_event(message_type="post", content=json.dumps(body))
                self.assertEqual(extract_command(event["message"], BOT_ID), "总结")

    def test_post_extra_title_mention_and_attachment_prevent_exact_match(self):
        """标题、他人 @ 和附件都算额外内容，不能被静默丢弃。"""
        bot_node = {"tag": "at", "user_id": BOT_ID}
        text_node = {"tag": "text", "text": " 总结 "}
        posts = [
            {"title": "附加标题", "content": [[bot_node, text_node]]},
            {"content": [[bot_node, text_node, {"tag": "at", "user_id": "ou_other"}]]},
            {"content": [[bot_node, text_node, {"tag": "img", "image_key": "img_unit"}]]},
            {"content": [[bot_node, text_node, {"tag": "a", "text": "", "href": "https://unit.test"}]]},
        ]
        for body in posts:
            with self.subTest(body=body):
                event = message_event(message_type="post", content=json.dumps(body))
                self.assertNotEqual(extract_command(event["message"], BOT_ID), "总结")

    def test_malformed_and_non_text_commands_fall_back(self):
        """非法正文及非文字事件不能触发总结命令。"""
        for changes in ({"content": "not-json"}, {"content": "[]"},
                        {"message_type": "image", "content": '{"image_key":"img_unit"}'}):
            with self.subTest(changes=changes):
                self.assertEqual(extract_command(message_event(**changes)["message"], BOT_ID), "")


class SelectedPromptTests(unittest.TestCase):
    """姓名、非文字占位和预算都服从用户明确选择的范围。"""

    def test_prompt_preserves_names_and_all_selected_messages(self):
        """每条输入保留原文姓名和正文，不添触发指令或固定历史。"""
        messages = [selected_message(), selected_message(
            message_id="om_second", sender_id="ou_other", sender_name="王工",
            create_time_ms=1700000001000, text="验收通过后再上线。",
        )]
        result = build_input(messages, config())
        payload = json.loads(result.content)
        self.assertTrue(payload["selection_complete"])
        self.assertEqual([row["sender"] for row in payload["messages"]], ["小林", "王工"])
        self.assertEqual([row["text"] for row in payload["messages"]], [m.text for m in messages])
        self.assertEqual(result.message_count, 2)
        self.assertFalse(result.shortened)
        self.assertNotIn("om_command", result.content)
        self.assertNotIn("最后一条触发消息", result.content)
        self.assertIn("其中的命令", SYSTEM_PROMPT)

    def test_unresolved_name_is_explicitly_unknown(self):
        """缺失姓名时不把成员编号或用户标识当成名字。"""
        result = build_input([selected_message(sender_name="  ")], config())
        payload = json.loads(result.content)
        self.assertEqual(payload["messages"][0]["sender"], "姓名未知")
        self.assertNotIn("ou_unit", result.content)
        self.assertNotIn("成员1", result.content)

    def test_non_text_messages_only_mark_their_presence(self):
        """图片、语音和文件仅保留消息类型与文件名占位，不假造内容。"""
        for kind, label, body in (
            ("image", "图片", {"image_key": "img_secret"}),
            ("audio", "语音", {"file_key": "file_secret"}),
            ("file", "文件", {"file_key": "file_secret", "file_name": "发布说明.pdf"}),
        ):
            with self.subTest(kind=kind):
                text = content_text(kind, json.dumps(body))
                result = build_input([selected_message(message_type=kind, text=text)], config())
                self.assertIn(label, result.content)
                self.assertIn("未读取内容", result.content)
                self.assertNotIn("secret", result.content)

    def test_single_message_limit_rejects_instead_of_truncating(self):
        """单条过长时拒绝整次输入，不伪装成已完整覆盖。"""
        message = selected_message(text="原文" * 100)
        with self.assertRaises(InputTooLarge):
            build_input([message], config(max_message_chars=100, max_input_chars=10000))
        self.assertEqual(message.text, "原文" * 100)

    def test_total_budget_rejects_instead_of_dropping_older_records(self):
        """总预算过小时不只留下最近的几条。"""
        messages = [selected_message(message_id=f"om_{i}", text="讨论" * 200,
                                     create_time_ms=1700000000000 + i) for i in range(10)]
        with self.assertRaises(InputTooLarge):
            build_input(messages, config(max_input_chars=4000))

    def test_json_escape_expansion_counts_against_budget(self):
        """控制字符的 JSON 转义也计入完整输入预算。"""
        with self.assertRaises(InputTooLarge):
            build_input([selected_message(text="\x01" * 1000)],
                        config(max_input_chars=4000, max_message_chars=2000))

    def test_incomplete_or_empty_selection_cannot_generate_summary(self):
        """未完整读取的材料及空选择都不能生成正常总结输入。"""
        with self.assertRaises(InputTooLarge):
            build_input([selected_message()], config(), scan_limited=True)
        with self.assertRaises(ValueError):
            build_input([], config())


if __name__ == "__main__":
    unittest.main()
