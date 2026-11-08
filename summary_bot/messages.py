"""提取飞书消息正文、请求表达和引用对象，保存可总结的消息记录。"""

from dataclasses import dataclass
from html import unescape
import json
import re
from typing import Any


def _post_body(body: dict[str, Any]) -> dict[str, Any]:
    """返回直接结构或多语言结构中的富文本正文，不修改原始消息。"""
    if "content" in body:
        return body
    return next(
        (body[key] for key in ("zh_cn", "en_us", *body)
         if isinstance(body.get(key), dict) and "content" in body[key]), {}
    )


def extract_command(message: dict[str, Any], bot_open_id: str) -> str:
    """仅移除当前机器人的真实 @ 标记及首尾空白，保留其余可见内容。

    text 从原始 JSON 文本读取；post 支持文字、当前机器人的 at 节点和
    多语言正文。其他人的 @、标题、附件和不支持的富文本节点仍会留下内容，
    交给路由完整匹配，避免删除附加信息后误触发总结。消息格式非法或
    非文字类型时返回空字符串，由路由层展示功能菜单。
    """
    if not bot_open_id:
        return ""
    mentions = [mention for mention in message.get("mentions") or []
                if isinstance(mention, dict)]
    bot_keys = []
    for mention in mentions:
        identity = mention.get("id")
        open_id = identity.get("open_id") if isinstance(identity, dict) else identity
        if open_id == bot_open_id and isinstance(mention.get("key"), str):
            bot_keys.append(mention["key"])

    def without_bot_keys(text: str) -> str:
        """避免 @_user_1 匹配并破坏 @_user_10 等其他成员的标记。"""
        for key in sorted(set(bot_keys), key=len, reverse=True):
            if key:
                text = re.sub(re.escape(key) + r"(?![A-Za-z0-9_])", "", text)
        return text

    try:
        body = json.loads(message.get("content", ""))
    except (ValueError, TypeError):
        return ""
    if not isinstance(body, dict):
        return ""
    message_type = message.get("message_type")
    if message_type == "text":
        text = body.get("text")
        return without_bot_keys(text).strip() if isinstance(text, str) else ""
    if message_type != "post":
        return ""
    post = _post_body(body)
    lines = [str(post.get("title") or "")]
    rows = post.get("content")
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, list):
            return ""
        fragments = []
        for node in row:
            if not isinstance(node, dict):
                return ""
            tag = node.get("tag")
            if tag == "at":
                user_id = node.get("user_id")
                if user_id == bot_open_id or user_id in bot_keys:
                    continue
                fragments.append("@" + str(node.get("user_name") or user_id or "姓名未知"))
            elif tag == "text":
                fragments.append(without_bot_keys(str(node.get("text") or "")))
            else:
                # 链接、图片等不能被静默删去后只剩下「总结」。
                fragments.append("[附加内容]")
        lines.append("".join(fragments))
    return "\n".join(lines).strip()


def content_text(message_type: str, content: str, mentions: list | None = None) -> str:
    """提取消息的可见文本，并用明确占位符表示无法读取的内容。

    参数：
        message_type：飞书消息类型，例如 text、post、image 或 file。
        content：飞书正文的 JSON 字符串，不是完整的事件对象。
        mentions：可选的 @信息列表，用 key 和 name 将 @_user_N 替换为可读名称。

    返回：
        去除首尾空白、还原 HTML 实体后的字符串。text 读取 text 字段；post
        提取标题、文字、链接和 @名称，并兼容直接结构与多语言结构。其他类型
        返回类型及可用文件名占位符，不下载附件。JSON 非法时返回解析失败提示，
        最终没有正文时返回空消息提示。

    关键行为：
        @占位符按 key 长度降序替换，避免 @_user_1 错误替换 @_user_10 的前缀。
        历史正文中的 <at> 标签也会转为普通 @名称文本。
    """
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return "[消息内容无法解析]"
    if not isinstance(body, dict):
        return "[消息内容无法解析]"
    if message_type == "text":
        text = str(body.get("text", ""))
    elif message_type == "post":
        post = _post_body(body)
        lines = [str(post.get("title", ""))]
        rows = post.get("content", [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list):
                continue
            fragments = []
            for node in row:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                if tag in {"text", "md", "code_block"}:
                    fragments.append(str(node.get("text", "")))
                elif tag == "a":
                    label, href = node.get("text", ""), node.get("href", "")
                    fragments.append(f"{label} ({href})" if label else str(href))
                elif tag == "at":
                    fragments.append("@" + str(node.get("user_name") or "成员"))
                elif tag in {"img", "media"}:
                    fragments.append("[图片]" if tag == "img" else "[视频]")
                elif tag == "emotion":
                    fragments.append(f"[表情：{node.get('emoji_type', '')}]")
            lines.append("".join(fragments))
        text = "\n".join(line for line in lines if line)
    else:
        labels = {
            "image": "图片", "file": "文件", "audio": "语音", "media": "视频",
            "sticker": "表情", "interactive": "卡片", "merge_forward": "合并转发",
            "share_chat": "群名片", "share_user": "个人名片",
        }
        label = labels.get(message_type, "非文本")
        name = body.get("file_name", "")
        text = f"[{label}{'：' + str(name) if name else ''}；未读取内容]"
    for mention in sorted(mentions or [], key=lambda item: len(item.get("key") or ""), reverse=True):
        key = mention.get("key")
        if key:
            text = text.replace(key, "@" + str(mention.get("name") or "成员"))
    # 历史消息中的 @ 也可能使用 <at user_id="...">姓名</at>。
    text = re.sub(r"<at\b[^>]*>(.*?)</at>", r"@\1", text, flags=re.S)
    return unescape(text).strip() or "[空消息]"


@dataclass(frozen=True)
class ChatMessage:
    """统一保存一条用户消息，供历史整理、队列持久化和提示词构造使用。

    字段：
        message_id：飞书消息唯一 ID，用于去重和引用回复。
        chat_id：消息所属群 ID，用于隔离历史查询。
        sender_id：优先使用用户 open_id，供网关查询发送者姓名使用。
        create_time_ms：消息发送时间，Unix 毫秒时间戳，不是事件推送时间。
        message_type：原始消息类型，用于向模型说明正文的来源。
        text：content_text 提取的正文或明确的非文本占位提示。
        parent_id：用户引用回复的上一层消息 ID，用于定位合并转发记录。
        sender_name：由群成员信息或账号信息取得的展示姓名；未知时留空。
        command：移除当前机器人 @ 标记后的请求表达，不是待总结材料。

    对象不可变；原始 SDK 对象和附件资源密钥不作为独立字段保存。
    """
    message_id: str
    chat_id: str
    sender_id: str
    create_time_ms: int
    message_type: str
    text: str
    parent_id: str = ""
    sender_name: str = ""
    command: str = ""

    @classmethod
    def from_event(cls, event: dict[str, Any], bot_open_id: str = "") -> "ChatMessage":
        """将接收消息事件的 event 字典转换为统一消息。

        参数：
            event：包含 message 与 sender 的事件正文，不包含外层 header/schema。
            bot_open_id：当前机器人 open_id；提供时提取请求表达。

        返回：
            cls 实例。发送者 ID 优先取 open_id，其次取 user_id，均缺失则用 unknown。

        异常：
            KeyError、ValueError 或 TypeError：关键结构缺失或时间无法转为整数。
            群聊类型、发送者类型及消息 ID 的有效性由调用方 SummaryBot.submit 检查。
        """
        message, sender = event["message"], event["sender"]
        sender_id = sender.get("sender_id") or {}
        return cls(
            message_id=message["message_id"], chat_id=message["chat_id"],
            sender_id=sender_id.get("open_id") or sender_id.get("user_id") or "unknown",
            create_time_ms=int(message["create_time"]),
            message_type=message["message_type"],
            text=content_text(message["message_type"], message.get("content", ""), message.get("mentions")),
            parent_id=message.get("parent_id") or "",
            sender_name=sender.get("name") or "",
            command=extract_command(message, bot_open_id),
        )

    @classmethod
    def from_history(cls, message: dict[str, Any]) -> "ChatMessage | None":
        """解析历史接口的一条消息，并跳过不适合总结的记录。

        参数：
            message：历史 API items 中的一项，正文位于 body.content，
                类型字段名为 msg_type，与消息事件的 message_type 不同。

        返回：
            有效用户消息返回 cls 实例；应用消息、系统消息、已撤回消息，
            或关键字段缺失/时间无效的记录返回 None。

        边界：
            本方法不判断群 ID 或查询时间范围，相关过滤由 FeishuGateway 完成。
            正文解析失败仍保留该消息，并使用 content_text 给出的占位提示。
        """
        sender = message.get("sender") or {}
        if sender.get("sender_type") != "user" or message.get("deleted"):
            return None
        if message.get("msg_type") == "system":
            return None
        body = message.get("body") or {}
        try:
            return cls(
                message_id=message["message_id"], chat_id=message.get("chat_id", ""),
                sender_id=sender.get("id") or "unknown",
                create_time_ms=int(message["create_time"]),
                message_type=message["msg_type"],
                text=content_text(message["msg_type"], body.get("content", ""), message.get("mentions")),
                parent_id=message.get("parent_id") or "",
                sender_name=sender.get("name") or "",
            )
        except (KeyError, ValueError, TypeError):
            return None
