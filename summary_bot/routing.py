"""精确指令注册与文字菜单；新增功能在此登记，再在业务层注册处理函数。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    """保存用户可见的功能名称、说明及操作方式。"""
    keyword: str
    description: str
    usage: str


SUMMARY_USAGE = (
    "总结操作：\n"
    "1. 多选需要总结的消息。\n"
    "2. 将它们合并转发到本群。\n"
    "3. 引用回复这条合并转发记录，发送「@机器人 总结」。\n"
    "请使用飞书的 @ 功能选择本机器人，指令只写「总结」。"
)

COMMANDS = (
    Command("总结", "总结你挑选的聊天记录，整理讨论要点、结论和待办事项。", SUMMARY_USAGE),
)


def resolve_command(text: str) -> Command | None:
    """仅去除首尾空格后完全匹配，不做包含判断或模型意图识别。"""
    return next((command for command in COMMANDS if text.strip() == command.keyword), None)


def menu_text() -> str:
    """从同一份功能注册表生成群内文字说明，保持菜单和指令一致。"""
    functions = [
        f"{index}. {command.keyword}\n{command.description}\n{command.usage}"
        for index, command in enumerate(COMMANDS, 1)
    ]
    return (
        "功能菜单\n\n" + "\n\n".join(functions)
        + "\n\n图片、语音、视频和文件暂时只标注存在，不识别其中内容。"
        + "\n所有功能都需要先 @ 本机器人；未识别的指令会显示本菜单。"
    )
