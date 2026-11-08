"""关键词、常见请求表达和文字菜单；新增功能在此登记并注册处理函数。"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Command:
    """保存关键词、说明、用法及可选的整句请求匹配规则。"""
    keyword: str
    description: str
    usage: str
    pattern: str = ""


SUMMARY_USAGE = (
    "总结操作：\n"
    "(1) 多选需要总结的消息。\n"
    "(2) 将它们合并转发到本群。\n"
    "(3) 引用回复这条合并转发记录，发送「@机器人 总结」。\n"
    "请使用飞书的 @ 功能选择本机器人，可发送「总结」「帮我总结一下」「总结重点」等。"
)

# 仅接受请求总结的完整表达，避免“不要总结”“总结功能怎么用”等被包含匹配误触发。
SUMMARY_REQUEST_PATTERN = (
    r"(?:(?:请(?:你)?|麻烦(?:你)?|劳驾)\s*)?"
    r"(?:(?:你\s*)?(?:能不能|能否|可以|能)\s*)?"
    r"(?:(?:帮我|帮忙|给我|为我)\s*)?"
    r"总结(?:\s*(?:一下|下))?"
    r"(?:\s*(?:(?:这(?:些|份|段|条)|所选的?|选中的?|上述|以上|本次)\s*)?"
    r"(?:聊天记录|聊天|记录|消息|内容|讨论))?"
    r"(?:\s*(?:的\s*)?(?:重点|要点))?"
    r"(?:\s*(?:一下|下))?"
    r"(?:\s*[,，]?\s*(?:吧|谢谢))?"
    r"\s*[。.!！?？]?"
)

COMMANDS = (
    Command("总结", "总结你挑选的聊天记录，整理讨论要点、结论和待办事项。",
            SUMMARY_USAGE, pattern=SUMMARY_REQUEST_PATTERN),
)


def resolve_command(text: str) -> Command | None:
    """先匹配注册关键词，再尝试整句请求规则；不按关键词出现与否猜测意图。"""
    text = text.strip()
    exact = next((command for command in COMMANDS if text == command.keyword), None)
    if exact is not None:
        return exact
    return next((command for command in COMMANDS
                 if command.pattern and re.fullmatch(command.pattern, text)), None)


def menu_text() -> str:
    """从同一份功能注册表生成群内文字说明，保持菜单和指令一致。"""
    functions = [
        f"{index}. {command.keyword}\n{command.description}\n{command.usage}"
        for index, command in enumerate(COMMANDS, 1)
    ]
    return (
        "【功能菜单】\n\n" + "\n\n".join(functions)
        # + "\n\n图片、语音、视频和文件暂时只标注存在，不识别其中内容。"
        # + "\n所有功能都需要先 @ 本机器人；未识别的指令会显示本菜单。"
        + "\n\n更多功能正在开发中，敬请期待。"
    )
