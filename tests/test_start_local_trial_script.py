from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_local_trial.sh"


class StartLocalTrialScriptTests(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["QE_TRIAL_PYTHON"] = sys.executable
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_help_describes_browser_cli_and_messaging_boundary(self) -> None:
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-open", result.stdout)
        self.assertIn("--cli", result.stdout)
        self.assertIn("不连接飞书、企微", result.stdout)

    def test_unknown_option_fails_without_starting_the_server(self) -> None:
        result = self.run_script("--unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("未知选项", result.stderr)

    def test_cli_runs_the_real_synthetic_demo(self) -> None:
        result = self.run_script("--cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("本地合成 Agent demo", result.stdout)
        payload = json.loads(result.stdout.split("\n", maxsplit=1)[1])
        self.assertEqual(len(payload["events"]), 25)
        self.assertEqual(len(payload["run"]["artifacts"]), 3)

    def test_no_open_starts_a_loopback_server_and_stops_on_signal(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        environment = os.environ.copy()
        environment["QE_TRIAL_PYTHON"] = sys.executable
        process = subprocess.Popen(
            [str(SCRIPT), "--no-open", "--port", str(port)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with socket.socket() as client:
                    if client.connect_ex(("127.0.0.1", port)) == 0:
                        break
                if process.poll() is not None:
                    self.fail("本地体验服务提前退出")
                time.sleep(0.05)
            else:
                self.fail("本地体验服务未在期限内监听 loopback 端口")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("仅合成数据，不连接任何聊天平台", stdout)
        self.assertIn(f"http://127.0.0.1:{port}/#token=", stdout)


if __name__ == "__main__":
    unittest.main()
