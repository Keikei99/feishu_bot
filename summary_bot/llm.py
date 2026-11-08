"""以 Anthropic Messages 格式请求模型，并将响应归一化为可回复的文本。"""

from dataclasses import dataclass
import time

import httpx

from .config import Config, messages_endpoint
from .prompt import SYSTEM_PROMPT


class LLMError(RuntimeError):
    """表示模型请求失败，或响应不符合本项目需要的文本内容块结构。

    本模块构造异常时只使用概括性说明和 HTTP 状态码，不包含密钥、
    聊天提示词或服务响应正文，业务层可据此记录原因并回复通用失败提示。
    """


@dataclass(frozen=True)
class SummaryResult:
    """保存从 Anthropic 响应中提取的总结结果。

    字段：
        text：所有 type=text 内容块按换行拼接后的非空正文。
        truncated：stop_reason 为 max_tokens 时为 True，表示达到输出上限；
            业务层据此附加提示，而不丢弃已经生成的内容。
    """
    text: str
    truncated: bool = False


class AnthropicMessagesClient:
    """管理同步 HTTP 请求、有限重试以及 Anthropic 内容块解析。

    只使用 Messages 协议，不解析 OpenAI choices 格式。系统提示词与用户
    聊天数据分开提交，不启用流式输出或工具；响应只取文本块。
    """
    def __init__(self, config: Config, client: httpx.Client | None = None):
        """保存配置并创建或接收 HTTPX 客户端。

        参数：
            config：服务地址、密钥、模型名称及超时、重试参数。
            client：可选外部客户端，测试中可配置 MockTransport 以拦截网络。

        默认客户端使用配置中的超时、10 秒连接超时，并禁止自动跟随重定向。
        _owns_client 记录所有权：close 只释放本对象自己创建的客户端。
        """
        self.config = config
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(config.llm_timeout_seconds, connect=10),
            follow_redirects=False,
        )
        self._owns_client = client is None

    def summarize(self, content: str) -> SummaryResult:
        """发送聊天记录并返回模型的文本总结。

        参数：
            content：已由 build_input 整理的聊天 JSON 字符串，放入一条 user 消息。
                SYSTEM_PROMPT 单独放在请求顶层的 system 字段。

        返回：
            SummaryResult，只拼接 type=text 且 text 为字符串的内容块；
            thinking 和工具调用块不会进入群回复。

        重试：
            网络错误和 HTTP 429/5xx 最多额外尝试 llm_max_retries 次。
            退避采用指数间隔；服务返回数字 Retry-After 时优先使用该值，
            单次等待限制在 0～10 秒。认证等其他 HTTP 错误不重试。

        异常：
            LLMError：请求最终失败、响应不是合法 JSON、返回错误对象，
            或没有可用文本块。抛出的错误不携带原始请求和响应正文。
        """
        request = {
            "model": self.config.llm_model,
            "max_tokens": self.config.llm_max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        }
        for attempt in range(self.config.llm_max_retries + 1):
            try:
                response = self.client.post(
                    messages_endpoint(self.config.llm_base_url),
                    headers={
                        "x-api-key": self.config.llm_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=request,
                )
            except httpx.TransportError:
                if attempt < self.config.llm_max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise LLMError("LLM 网络连接失败或请求超时") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.config.llm_max_retries:
                    try:
                        delay = float(response.headers.get("retry-after", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(max(0, min(delay, 10)))
                    continue
            if not response.is_success:
                raise LLMError(f"LLM 请求失败，HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError:
                raise LLMError("LLM 未返回合法 JSON") from None
            if not isinstance(body, dict) or body.get("type") == "error":
                raise LLMError("LLM 返回错误响应")
            blocks = body.get("content")
            if not isinstance(blocks, list):
                raise LLMError("LLM 响应缺少 Anthropic content 内容块")
            text = "\n".join(
                block["text"] for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
            if not text:
                raise LLMError("LLM 未返回文本总结")
            return SummaryResult(text, body.get("stop_reason") == "max_tokens")
        raise LLMError("LLM 重试次数已耗尽")

    def close(self) -> None:
        """关闭本实例自行创建的连接池，返回 None。

        外部传入的客户端仍由调用方负责关闭，避免测试或共享客户端被提前释放。
        """
        if self._owns_client:
            self.client.close()
