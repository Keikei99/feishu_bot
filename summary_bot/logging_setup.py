"""统一业务与 SDK 的滚动文件日志，避免 nohup 输出无限增长。"""

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import traceback
from zoneinfo import ZoneInfo

from .config import Config


class _TimezoneFormatter(logging.Formatter):
    def __init__(self, timezone: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.timezone = ZoneInfo(timezone)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.timezone).isoformat(timespec="milliseconds")


def setup_logging(config: Config) -> None:
    """创建 UTF-8 滚动文件；终端输出可选，第三方调试日志保持关闭。

    日志文件立即打开，以便在启动网络和工作线程前发现目录或权限错误。
    重复初始化仅替换本模块拥有的根处理器，不移除调用方的额外处理器。
    """
    formatter = _TimezoneFormatter(config.display_timezone)
    config.log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log_file, maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count, encoding="utf-8",
    )
    handlers: list[logging.Handler] = [file_handler]
    if config.log_console:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._summary_bot_owned = True

    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, "_summary_bot_owned", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(config.log_level)
    for handler in handlers:
        root.addHandler(handler)

    # Lark 导入时安装了自己的 stdout 处理器；统一转交根处理器，避免重复写入。
    sdk_logger = logging.getLogger("Lark")
    for handler in sdk_logger.handlers[:]:
        sdk_logger.removeHandler(handler)
        handler.close()
    sdk_logger.propagate = True
    # SDK DEBUG 会打印完整事件，不能随业务 LOG_LEVEL=DEBUG 一起开启。
    for name in ("Lark", "httpx", "httpcore", "requests", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_unhandled_error(error: Exception) -> None:
    """记录顶层异常的位置与类型，不输出可能携带凭据或正文的异常全文。"""
    frames = traceback.extract_tb(error.__traceback__, limit=8)
    locations = " -> ".join(f"{frame.filename}:{frame.lineno}({frame.name})" for frame in frames)
    logging.getLogger(__name__).error(
        "程序异常退出，error_type=%s, locations=%s", type(error).__name__, locations,
    )
