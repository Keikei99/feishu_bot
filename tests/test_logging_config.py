"""离线验证滚动日志配置；替换 dotenv，不读取实际配置文件。"""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from summary_bot.config import Config, PROJECT_DIR


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        environment = {
            "FEISHU_APP_ID": "cli_logging_config_test", "FEISHU_APP_SECRET": "fake-secret",
            "LLM_BASE_URL": "https://model.example.test", "LLM_API_KEY": "fake-key",
            "LLM_MODEL": "fake-model",
        }
        dotenv = patch("summary_bot.config.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)
        environ = patch.dict(os.environ, environment, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def test_default_policy_uses_project_log_directory(self):
        config = Config.from_env()
        self.assertEqual(config.log_file, PROJECT_DIR / "logs/bot.log")
        self.assertEqual(config.log_max_bytes, 20 * 1024 * 1024)
        self.assertEqual(config.log_backup_count, 4)
        self.assertFalse(config.log_console)

    def test_relative_path_and_custom_rotation_policy(self):
        os.environ.update({
            "LOG_FILE": "runtime/logs/worker.log", "LOG_MAX_BYTES": "1048576",
            "LOG_BACKUP_COUNT": "2", "LOG_CONSOLE": " TrUe ",
        })
        config = Config.from_env()
        self.assertEqual(config.log_file, PROJECT_DIR / "runtime/logs/worker.log")
        self.assertEqual(config.log_max_bytes, 1048576)
        self.assertEqual(config.log_backup_count, 2)
        self.assertTrue(config.log_console)

    def test_absolute_path_is_preserved(self):
        os.environ["LOG_FILE"] = "/var/log/feishu-summary/bot.log"
        os.environ["LOG_CONSOLE"] = "FALSE"
        config = Config.from_env()
        self.assertEqual(config.log_file, Path("/var/log/feishu-summary/bot.log"))
        self.assertFalse(config.log_console)

    def test_invalid_rotation_and_console_values_are_rejected(self):
        cases = (
            ("LOG_FILE", ""), ("LOG_FILE", "  "),
            ("LOG_MAX_BYTES", "0"), ("LOG_MAX_BYTES", "1023"),
            ("LOG_MAX_BYTES", "1.5"), ("LOG_MAX_BYTES", "inf"),
            ("LOG_MAX_BYTES", ""), ("LOG_BACKUP_COUNT", "0"),
            ("LOG_BACKUP_COUNT", "-1"), ("LOG_BACKUP_COUNT", "2.5"),
            ("LOG_CONSOLE", "yes"), ("LOG_CONSOLE", "1"),
            ("LOG_CONSOLE", ""),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(ValueError, name):
                    Config.from_env()


if __name__ == "__main__":
    unittest.main()
