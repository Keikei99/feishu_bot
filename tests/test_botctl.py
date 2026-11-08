"""用临时项目和虚构服务验证管理控制流；实际 Linux flock/ps 组合另行验证。

ps 仅识别本测试的服务标记，规避受限环境禁止查询宿主进程列表的问题。
信号及进程存活仍由操作系统处理；缺少 flock 时用 fcntl 锁继承文件描述符。
"""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/botctl.sh"


@unittest.skipUnless(os.name == "posix", "botctl.sh 需要 POSIX shell")
class BotctlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "fake-project"
        (self.project / "scripts").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.project / "scripts/botctl.sh")
        python_path = self.project / ".venv/bin/python"
        python_path.parent.mkdir(parents=True)
        python_path.symlink_to(sys.executable)
        (self.project / "main.py").write_text(textwrap.dedent("""
            import os
            from pathlib import Path
            import signal
            import sys
            import time

            directory = Path(__file__).resolve().parent
            if '--check-logging' in sys.argv:
                if (directory / 'reject-precheck').exists():
                    print('虚构日志预检失败', file=sys.stderr)
                    raise SystemExit(2)
                raise SystemExit(0)

            def stop(signum, frame):
                with (directory / 'stopped.pids').open('a') as stream:
                    stream.write(str(os.getpid()) + '\\n')
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            with (directory / 'started.pids').open('a') as stream:
                stream.write(str(os.getpid()) + '\\n')
            while not (directory / 'cleanup.request').exists():
                time.sleep(0.05)
            stop(signal.SIGTERM, None)
        """), encoding="utf-8")
        self.environment = dict(os.environ)
        fake_bin = self.project / "fake-bin"
        fake_bin.mkdir()
        if shutil.which("flock") is None:
            fake_flock = fake_bin / "flock"
            fake_flock.write_text(
                f"#!{sys.executable}\n"
                "import fcntl, sys\n"
                "try:\n"
                "    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except BlockingIOError:\n"
                "    raise SystemExit(1)\n",
                encoding="utf-8",
            )
            fake_flock.chmod(0o700)
        fake_ps = fake_bin / "ps"
        fake_ps.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            f"directory = Path({str(self.project)!r})\n"
            "pid = int(sys.argv[sys.argv.index('-p') + 1])\n"
            "def pids(name):\n"
            "    path = directory / name\n"
            "    return path.read_text().split() if path.exists() else []\n"
            "try:\n"
            "    os.kill(pid, 0)\n"
            "except ProcessLookupError:\n"
            "    raise SystemExit(1)\n"
            "if str(pid) in pids('stopped.pids'):\n"
            "    raise SystemExit(1)\n"
            "if sys.argv[-1] == 'stat=':\n"
            "    print('S')\n"
            "elif str(pid) in pids('started.pids'):\n"
            "    print(str(directory / '.venv/bin/python') + ' -u ' + str(directory / 'main.py'))\n"
            "else:\n"
            "    print('nano ' + str(directory / 'main.py'))\n",
            encoding="utf-8",
        )
        fake_ps.chmod(0o700)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment.get("PATH", "")
        self.unrelated = []
        self.addCleanup(self.cleanup_processes)

    def control(self, action, *, timeout=8):
        return subprocess.run(
            ["bash", str(self.project / "scripts/botctl.sh"), action],
            cwd=self.project, env=self.environment, capture_output=True,
            text=True, timeout=timeout,
        )

    def recorded_pids(self, filename):
        path = self.project / filename
        return [int(value) for value in path.read_text().split()] if path.exists() else []

    def wait_for_start(self, expected_count):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            values = self.recorded_pids("started.pids")
            if len(values) >= expected_count:
                return values[-1]
            time.sleep(0.02)
        self.fail("虚构服务未启动")

    def start(self, expected_count=1):
        result = self.control("start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.wait_for_start(expected_count)

    def cleanup_processes(self):
        # 只处理本测试临时项目记录的虚构服务；不使用实际项目的管理脚本。
        (self.project / "cleanup.request").touch()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if set(self.recorded_pids("started.pids")) <= set(self.recorded_pids("stopped.pids")):
                break
            time.sleep(0.02)
        for pid in self.recorded_pids("started.pids"):
            if pid not in self.recorded_pids("stopped.pids"):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in self.unrelated:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def test_start_status_and_duplicate_start_keep_one_process(self):
        pid = self.start()
        status = self.control("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn(str(pid), status.stdout)
        repeated = self.control("start")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("已在运行", repeated.stdout)
        self.assertEqual(self.recorded_pids("started.pids"), [pid])
        self.assertEqual((self.project / "run/bot.pid").read_text().strip(), str(pid))

    def test_lock_prevents_second_process_even_without_pid_file(self):
        pid = self.start()
        (self.project / "run/bot.pid").unlink()
        result = self.control("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.recorded_pids("started.pids"), [pid])

    def test_stop_uses_sigterm_and_removes_pid_file(self):
        pid = self.start()
        result = self.control("stop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(pid, self.recorded_pids("stopped.pids"))
        self.assertFalse((self.project / "run/bot.pid").exists())
        self.assertEqual(self.control("status").returncode, 1)

    def test_restart_stops_old_process_and_starts_new_pid(self):
        old_pid = self.start()
        result = self.control("restart")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        new_pid = self.wait_for_start(2)
        self.assertNotEqual(new_pid, old_pid)
        self.assertIn(old_pid, self.recorded_pids("stopped.pids"))
        self.assertEqual((self.project / "run/bot.pid").read_text().strip(), str(new_pid))

    def test_stale_pid_pointing_to_other_process_is_not_signalled(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.unrelated.append(process)
        (self.project / "run").mkdir()
        (self.project / "run/bot.pid").write_text(str(process.pid), encoding="utf-8")
        self.assertEqual(self.control("status").returncode, 1)
        result = self.control("stop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(process.poll())

    def test_background_start_does_not_create_nohup_output(self):
        self.start()
        self.assertEqual(list(self.project.rglob("nohup.out")), [])

    def test_failed_logging_precheck_does_not_launch_service(self):
        (self.project / "reject-precheck").touch()
        result = self.control("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("虚构日志预检失败", result.stderr)
        self.assertEqual(self.recorded_pids("started.pids"), [])
        self.assertFalse((self.project / "run/bot.pid").exists())
        self.assertEqual(list(self.project.rglob("nohup.out")), [])


if __name__ == "__main__":
    unittest.main()
