"""在临时目录离线验证滚动日志、SDK 去重和初始化隔离，不读取实际配置。"""

from dataclasses import replace
import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from summary_bot.config import Config
from summary_bot.logging_setup import log_unhandled_error, setup_logging


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = Config(
            app_id="cli_logging_test", app_secret="fake-secret",
            llm_base_url="https://model.example.test", llm_api_key="fake-key",
            llm_model="fake-model", log_file=self.directory / "logs" / "bot.log",
        )
        names = ("", "Lark", "httpx", "httpcore", "requests", "urllib3", "summary_bot.logging_test")
        self.states = []
        for name in names:
            logger = logging.getLogger(name)
            self.states.append((logger, logger.handlers[:], logger.level, logger.propagate,
                                logger.disabled, logger.filters[:]))
            logger.handlers = []
            logger.filters = []
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            logger.disabled = False
        self.addCleanup(self.restore_loggers)
        self.logger = logging.getLogger("summary_bot.logging_test")

    def restore_loggers(self):
        original_handlers = {handler for _, handlers, *_ in self.states for handler in handlers}
        for logger, handlers, level, propagate, disabled, filters in self.states:
            for handler in logger.handlers:
                if handler not in original_handlers:
                    handler.close()
            logger.handlers = handlers
            logger.filters = filters
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled

    def contents(self):
        for handler in logging.getLogger().handlers:
            handler.flush()
        return self.config.log_file.read_text(encoding="utf-8")

    def test_default_file_output_creates_directory_and_keeps_unicode_and_timezone(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            setup_logging(self.config)
            self.logger.info("已完成总结，内容可读")
        contents = self.contents()
        self.assertIn("已完成总结，内容可读", contents)
        self.assertEqual(contents.count("已完成总结，内容可读"), 1)
        self.assertRegex(contents, r"\+08:?00")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse((self.directory / "nohup.out").exists())

    def test_real_rotation_retains_only_configured_number_of_backups(self):
        self.config = replace(self.config, log_max_bytes=1024, log_backup_count=2)
        setup_logging(self.config)
        for index in range(60):
            self.logger.info("record=%s %s", index, "x" * 150)
        self.contents()
        files = sorted(path.name for path in self.config.log_file.parent.iterdir())
        self.assertEqual(files, ["bot.log", "bot.log.1", "bot.log.2"])
        self.assertIn("record=59 ", self.contents())
        self.assertNotIn("record=0 ", "".join(
            path.read_text(encoding="utf-8") for path in self.config.log_file.parent.iterdir()
        ))
        self.assertTrue(all(path.stat().st_size <= 1024
                            for path in self.config.log_file.parent.iterdir()))

    def test_sdk_warning_is_written_once_and_uses_shared_handler(self):
        sdk_output = io.StringIO()
        sdk = logging.getLogger("Lark")
        sdk.addHandler(logging.StreamHandler(sdk_output))
        setup_logging(self.config)
        sdk.warning("sdk-warning-marker")
        self.assertEqual(self.contents().count("sdk-warning-marker"), 1)
        self.assertEqual(sdk_output.getvalue(), "")
        self.assertEqual(sdk.handlers, [])
        self.assertTrue(sdk.propagate)

    def test_business_debug_does_not_enable_raw_sdk_or_http_debug(self):
        setup_logging(replace(self.config, log_level="DEBUG"))
        self.logger.debug("business-debug-marker")
        for name in ("Lark", "httpx", "httpcore", "requests", "urllib3"):
            logging.getLogger(name).debug("sensitive-payload-marker")
            self.assertEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)
        contents = self.contents()
        self.assertIn("business-debug-marker", contents)
        self.assertNotIn("sensitive-payload-marker", contents)

    def test_reinitializing_closes_old_file_and_does_not_duplicate_output(self):
        setup_logging(self.config)
        old_handlers = [handler for handler in logging.getLogger().handlers
                        if isinstance(handler, RotatingFileHandler)]
        self.assertEqual(len(old_handlers), 1)
        old_stream = old_handlers[0].stream
        setup_logging(self.config)
        self.logger.info("single-after-reinitialization")
        self.assertTrue(old_stream.closed)
        self.assertEqual(self.contents().count("single-after-reinitialization"), 1)
        self.assertEqual(sum(isinstance(handler, RotatingFileHandler)
                             for handler in logging.getLogger().handlers), 1)

    def test_optional_console_output_has_one_line_and_matches_file(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            setup_logging(replace(self.config, log_console=True))
            self.logger.info("console-and-file-marker")
        self.assertEqual(stderr.getvalue().count("console-and-file-marker"), 1)
        self.assertEqual(self.contents().count("console-and-file-marker"), 1)

    def test_setup_preserves_a_caller_supplied_root_handler(self):
        external_output = io.StringIO()
        external_handler = logging.StreamHandler(external_output)
        logging.getLogger().addHandler(external_handler)
        setup_logging(self.config)
        setup_logging(self.config)
        self.logger.info("external-handler-marker")
        self.assertIn(external_handler, logging.getLogger().handlers)
        self.assertEqual(external_output.getvalue().count("external-handler-marker"), 1)
        self.assertEqual(self.contents().count("external-handler-marker"), 1)

    def test_unusable_log_path_fails_instead_of_silently_dropping_logs(self):
        occupied = self.directory / "occupied"
        occupied.write_text("a regular file blocks parent directory creation", encoding="utf-8")
        with self.assertRaises(OSError):
            setup_logging(replace(self.config, log_file=occupied / "bot.log"))

    def test_unhandled_error_keeps_location_without_exception_text_or_cause(self):
        setup_logging(self.config)
        try:
            try:
                raise ValueError("fake-sensitive-key-marker")
            except ValueError as cause:
                raise RuntimeError("fake-private-message-marker") from cause
        except RuntimeError as error:
            log_unhandled_error(error)
        contents = self.contents()
        self.assertIn("error_type=RuntimeError", contents)
        self.assertIn("test_logging_setup.py:", contents)
        self.assertIn("test_unhandled_error_keeps_location_without_exception_text_or_cause", contents)
        self.assertNotIn("fake-sensitive-key-marker", contents)
        self.assertNotIn("fake-private-message-marker", contents)


if __name__ == "__main__":
    unittest.main()
