"""将总结的已知栏目标题统一为群内可读的【】样式，不改写正文。"""

import re


_TITLES = "讨论主题|关键结论|待办事项|待确认问题"
_HEADING = re.compile(
    rf"^[ \t]*(?:#{{1,6}}[ \t]*)?"
    rf"(?:\*\*(?P<bold>{_TITLES})[：:]?\*\*|"
    rf"【(?P<bracket>{_TITLES})】|(?P<plain>{_TITLES}))"
    r"(?:[ \t]*[：:][ \t]*(?P<body>.*)|[ \t]*(?:#{1,6}[ \t]*)?)$"
)


def format_summary_body(text: str) -> str:
    """规范四个已知栏目标题，保留正文列表、空行及其他 Markdown。

    兼容旧粗体标题、Markdown 标题、纯文字标题和已正确的【】标题。
    标题后的冒号及同行正文会转为标题与正文两行；普通句子、列表项和
    代码块中的标题文字不作替换。已正确的格式再次处理时保持不变。
    """
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        stripped = content.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            lines.append(line)
            continue
        match = _HEADING.fullmatch(content) if fence is None else None
        if match is None:
            lines.append(line)
            continue
        title = match.group("bold") or match.group("bracket") or match.group("plain")
        body = match.group("body")
        normalized = f"【{title}】"
        if body:
            normalized += (ending or "\n") + body
        lines.append(normalized + ending)
    return "".join(lines)
