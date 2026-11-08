"""离线验证启动身份解析及纯配置检查，不读取实际配置或创建运行数据库。"""

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from summary_bot.config import Config
from summary_bot.feishu import FeishuError
import main


def config():
    """返回没有机器人ID的虚构配置，不调用from_env。"""
    return Config(
        app_id="cli_entrypoint_test", app_secret="fake-secret",
        llm_base_url="https://model.example.test", llm_api_key="fake-key",
        llm_model="fake-model", state_db_path=Path("/unused/test.db"),
    )


class EntrypointTests(unittest.TestCase):
    """启动时补齐身份，配置检查和身份失败均不启动服务。"""

    def dependencies(self, argv, settings):
        """隔离所有外部依赖，避免配置文件、网络和运行数据库访问。"""
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(sys, "argv", argv))
        stack.enter_context(patch("main.Config.from_env", return_value=settings))
        dependencies = {}
        for name in ("FeishuGateway", "AnthropicMessagesClient", "JobStore", "SummaryBot"):
            dependencies[name] = stack.enter_context(patch("main." + name))
        dependencies["websocket"] = stack.enter_context(patch("main.lark.ws.Client"))
        dependencies["gateway_instance"] = Mock()
        dependencies["gateway_instance"].get_bot_open_id.return_value = "ou_resolved_bot"
        dependencies["FeishuGateway"].return_value = dependencies["gateway_instance"]
        return dependencies

    def test_configuration_check_never_creates_clients_or_queue(self):
        """检查模式不自动查身份、不连接服务、不创建数据库。"""
        dependencies = self.dependencies(["main.py", "--check-config"], config())
        self.assertEqual(main.main(), 0)
        for name in ("FeishuGateway", "AnthropicMessagesClient", "JobStore", "SummaryBot", "websocket"):
            dependencies[name].assert_not_called()

    def test_startup_resolves_missing_bot_id_before_starting_workers(self):
        """留空ID时先向飞书查本机器人身份，并传入业务过滤配置。"""
        dependencies = self.dependencies(["main.py"], config())
        self.assertEqual(main.main(), 0)
        dependencies["gateway_instance"].get_bot_open_id.assert_called_once()
        runtime_config = dependencies["SummaryBot"].call_args.args[0]
        self.assertEqual(runtime_config.bot_open_id, "ou_resolved_bot")
        dependencies["SummaryBot"].return_value.start.assert_called_once()
        dependencies["SummaryBot"].return_value.close.assert_called_once()
        dependencies["AnthropicMessagesClient"].return_value.close.assert_called_once()

    def test_explicit_bot_id_avoids_extra_lookup(self):
        """已填写机器人ID时直接使用，不增加启动请求。"""
        dependencies = self.dependencies(["main.py"], replace(config(), bot_open_id="ou_configured"))
        self.assertEqual(main.main(), 0)
        dependencies["gateway_instance"].get_bot_open_id.assert_not_called()

    def test_identity_failure_does_not_start_or_open_database(self):
        """身份查询失败时明确退出，不能降级到全群触发。"""
        dependencies = self.dependencies(["main.py"], config())
        dependencies["gateway_instance"].get_bot_open_id.side_effect = FeishuError("身份查询失败")
        self.assertEqual(main.main(), 2)
        for name in ("AnthropicMessagesClient", "JobStore", "SummaryBot", "websocket"):
            dependencies[name].assert_not_called()


if __name__ == "__main__":
    unittest.main()
