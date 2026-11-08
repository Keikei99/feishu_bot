"""离线验证队列重试配置；替换 dotenv，不读取实际配置文件。"""

import os
import unittest
from unittest.mock import patch

from summary_bot.config import Config


class RetryConfigTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "FEISHU_APP_ID": "cli_retry_config_test", "FEISHU_APP_SECRET": "fake-secret",
            "LLM_BASE_URL": "https://model.example.test", "LLM_API_KEY": "fake-key",
            "LLM_MODEL": "fake-model",
        }
        self.dotenv = patch("summary_bot.config.load_dotenv")
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        self.environ = patch.dict(os.environ, self.environment, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_defaults_and_zero_retries(self):
        config = Config.from_env()
        self.assertEqual(config.job_max_retries, 5)
        self.assertEqual(config.job_retry_base_seconds, 5)
        self.assertEqual(config.job_retry_max_seconds, 300)
        os.environ["JOB_MAX_RETRIES"] = "0"
        self.assertEqual(Config.from_env().job_max_retries, 0)

    def test_custom_retry_policy_and_equal_intervals(self):
        os.environ.update({
            "JOB_MAX_RETRIES": "2", "JOB_RETRY_BASE_SECONDS": "0.5",
            "JOB_RETRY_MAX_SECONDS": "0.5",
        })
        config = Config.from_env()
        self.assertEqual(config.job_max_retries, 2)
        self.assertEqual(config.job_retry_base_seconds, 0.5)
        self.assertEqual(config.job_retry_max_seconds, 0.5)

    def test_invalid_retry_values_are_rejected(self):
        cases = (
            ("JOB_MAX_RETRIES", "-1"), ("JOB_MAX_RETRIES", "1.5"),
            ("JOB_MAX_RETRIES", ""), ("JOB_RETRY_BASE_SECONDS", "nan"),
            ("JOB_RETRY_BASE_SECONDS", "0"), ("JOB_RETRY_BASE_SECONDS", "-1"),
            ("JOB_RETRY_MAX_SECONDS", "inf"), ("JOB_RETRY_MAX_SECONDS", "0.01"),
            ("JOB_RETRY_MAX_SECONDS", "text"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(ValueError, name):
                    Config.from_env()

    def test_max_interval_must_cover_base_interval(self):
        os.environ.update({"JOB_RETRY_BASE_SECONDS": "10", "JOB_RETRY_MAX_SECONDS": "5"})
        with self.assertRaisesRegex(ValueError, "JOB_RETRY_MAX_SECONDS.*JOB_RETRY_BASE_SECONDS"):
            Config.from_env()


if __name__ == "__main__":
    unittest.main()
