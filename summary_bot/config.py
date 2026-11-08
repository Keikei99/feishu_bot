"""配置定义与校验：固定 .env 位置，解析环境变量，规范化模型请求地址。"""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent


def messages_endpoint(base_url: str) -> str:
    """将模型服务基础地址转换为 Anthropic Messages 请求端点。

    参数：
        base_url：完整 HTTP/HTTPS 地址，可包含代理前缀路径。
            域名根地址补上 ``/v1/messages``，以 ``/v1`` 结尾时补上
            ``/messages``，已以 ``/messages`` 结尾的路径直接保留。

    返回：
        移除末尾斜杠并补全路径后的 URL，不执行网络请求。

    异常：
        ValueError：协议或主机缺失，或 URL 包含账号、密码、查询参数、片段。
    """
    parts = urlsplit(base_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("LLM_BASE_URL 必须是完整的 http/https 地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("LLM_BASE_URL 请勿包含账号、密码、查询参数或片段")
    path = parts.path.rstrip("/")
    if not path.endswith("/messages"):
        path += "/messages" if path.endswith("/v1") else "/v1/messages"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class Config:
    """保存一次启动所使用的不可变配置，供各模块共享。

    字段：
        app_id / app_secret：飞书自建应用的身份凭据。
        llm_base_url / llm_api_key / llm_model：模型服务地址、密钥及模型 ID。
        llm_max_tokens：模型输出 token 上限。
        llm_timeout_seconds：HTTPX 的请求超时设置；连接超时另设为 10 秒。
        llm_max_retries：首次请求之外允许的模型重试次数。
        trigger_mode：固定为 mention，仅 @当前机器人触发；旧 all 配置仅兼容读取。
        bot_open_id：精确匹配被 @者，留空时由正式启动流程向飞书查询。
        allowed_chat_ids：群 ID 白名单；空集合表示不额外限制群。
        history_hours / history_max_messages / history_max_pages：旧历史接口的兼容配置，
            选中记录总结不使用这些字段，不按固定消息条数取历史。
        max_input_chars / max_message_chars：聊天 JSON 和单条正文的字符预算。
        display_timezone：聊天时间和回复标题使用的 IANA 时区名称。
        state_db_path：SQLite 数据库路径，由 from_env 解析为项目相对路径或绝对路径。
        job_max_retries：队列每阶段首次执行失败后允许的额外重试次数。
        job_retry_base_seconds / job_retry_max_seconds：队列指数退避的初始间隔和上限。
        log_level：业务日志级别。

    使用说明：
        frozen=True 防止初始化后意外修改字段，repr=False 避免在对象展示中输出
        两项密钥。生产入口通过 from_env 校验配置；直接构造对象不会执行这些校验，
        便于测试按需注入配置。
    """
    app_id: str
    app_secret: str = field(repr=False)
    llm_base_url: str
    llm_api_key: str = field(repr=False)
    llm_model: str
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 90
    llm_max_retries: int = 2
    trigger_mode: str = "mention"
    bot_open_id: str = ""
    allowed_chat_ids: frozenset[str] = frozenset()
    history_hours: float = 24
    history_max_messages: int = 100
    history_max_pages: int = 20
    max_input_chars: int = 24000
    max_message_chars: int = 2000
    display_timezone: str = "Asia/Shanghai"
    state_db_path: Path = PROJECT_DIR / "data/bot.sqlite3"
    log_level: str = "INFO"
    job_max_retries: int = 5
    job_retry_base_seconds: float = 5
    job_retry_max_seconds: float = 300

    @classmethod
    def from_env(cls) -> "Config":
        """读取项目 .env 与进程环境变量，校验后构造配置对象。

        读取顺序：
            使用固定项目路径加载 .env，已有进程环境变量保持优先。必填字符串
            去除首尾空白；数字校验类型、有限性及下限；同时校验触发模式、
            机器人 ID、时区、日志级别和模型 URL。

        返回：
            cls 的实例。数据库路径支持 ``~``，相对路径以 PROJECT_DIR 为基准。

        异常：
            ValueError：必填项为空或配置格式无效，错误信息只指出配置项问题。

        副作用：
            python-dotenv 可能将 .env 中尚不存在的变量加入进程环境；不请求网络。
        """
        # 无论从哪个目录启动，都只加载本项目的 .env；已有环境变量优先。
        load_dotenv(PROJECT_DIR / ".env")

        def required(name: str) -> str:
            """读取必填环境变量，返回去除首尾空白的值；缺失或为空时抛出 ValueError。

            name 是变量名称，例如 LLM_MODEL。异常仅包含变量名，不包含变量值。
            """
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"缺少 {name}，请先填写 .env")
            return value

        def number(name: str, default: str, kind: type = int, minimum: float = 1):
            """读取并校验数字环境变量，返回转换后的 int 或 float。

            参数：
                name：环境变量名称；未设置时使用字符串 default。
                kind：数字转换函数，本模块使用 int 或 float。
                minimum：允许的最小值，包含该边界。

            异常：
                ValueError：无法转换、数值为 NaN/无穷大，或小于 minimum。
                已设置的空字符串视为非法数字，不会回退到默认值。
            """
            try:
                value = kind(os.getenv(name, default))
            except ValueError:
                raise ValueError(f"{name} 必须是有效数字") from None
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"{name} 必须不小于 {minimum}")
            return value

        mode = os.getenv("TRIGGER_MODE", "mention").strip().lower()
        bot_id = os.getenv("BOT_OPEN_ID", "").strip()
        if mode not in {"all", "mention"}:
            raise ValueError("TRIGGER_MODE 只能是 all 或 mention")
        if bot_id and not bot_id.startswith("ou_"):
            raise ValueError("BOT_OPEN_ID 必须是当前机器人的 open_id（ou_ 开头），留空可自动查询")
        timezone = os.getenv("DISPLAY_TIMEZONE", "Asia/Shanghai").strip()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("DISPLAY_TIMEZONE 不是有效时区") from None
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 无效")
        db_path = Path(os.getenv("STATE_DB_PATH", "data/bot.sqlite3")).expanduser()
        base_url = required("LLM_BASE_URL")
        messages_endpoint(base_url)
        job_retry_base = number("JOB_RETRY_BASE_SECONDS", "5", float, 0.1)
        job_retry_max = number("JOB_RETRY_MAX_SECONDS", "300", float, 0.1)
        if job_retry_max < job_retry_base:
            raise ValueError("JOB_RETRY_MAX_SECONDS 必须不小于 JOB_RETRY_BASE_SECONDS")
        return cls(
            app_id=required("FEISHU_APP_ID"),
            app_secret=required("FEISHU_APP_SECRET"),
            llm_base_url=base_url,
            llm_api_key=required("LLM_API_KEY"),
            llm_model=required("LLM_MODEL"),
            llm_max_tokens=number("LLM_MAX_TOKENS", "2048"),
            llm_timeout_seconds=number("LLM_TIMEOUT_SECONDS", "90", float, 0.1),
            llm_max_retries=number("LLM_MAX_RETRIES", "2", minimum=0),
            trigger_mode="mention",
            bot_open_id=bot_id,
            allowed_chat_ids=frozenset(
                value.strip() for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
                if value.strip()
            ),
            history_hours=number("HISTORY_HOURS", "24", float, 0.01),
            history_max_messages=number("HISTORY_MAX_MESSAGES", "100"),
            history_max_pages=number("HISTORY_MAX_PAGES", "20"),
            max_input_chars=number("MAX_INPUT_CHARS", "24000", minimum=4000),
            max_message_chars=number("MAX_MESSAGE_CHARS", "2000", minimum=100),
            display_timezone=timezone,
            state_db_path=db_path if db_path.is_absolute() else PROJECT_DIR / db_path,
            log_level=log_level,
            job_max_retries=number("JOB_MAX_RETRIES", "5", minimum=0),
            job_retry_base_seconds=job_retry_base,
            job_retry_max_seconds=job_retry_max,
        )
