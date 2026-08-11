import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "server.py"


class UnixServerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.socket_path = self.root / "run" / "gpuviz.sock"
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            process.communicate(timeout=1)
        self.tmp.cleanup()

    def _environment(self):
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _command(self):
        return [sys.executable, str(SERVER), "--socket", str(self.socket_path)]

    def _start_server(self):
        process = subprocess.Popen(
            self._command(),
            cwd=REPO_ROOT,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        self._wait_until_ready(process)
        return process

    def _wait_until_ready(self, process):
        deadline = time.monotonic() + 5
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                self.fail(
                    f"server exited before creating its socket ({process.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            if self.socket_path.exists():
                try:
                    response = self._health_request()
                except OSError as error:
                    last_error = error
                else:
                    if response.startswith(b"HTTP/1.1 200"):
                        return
            time.sleep(0.02)
        self.fail(f"server did not become ready: {last_error!r}")

    def _health_request(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.socket_path))
            client.sendall(
                b"GET /api/health HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def test_socket_is_private_and_serves_health(self):
        self._start_server()

        info = self.socket_path.stat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertIn(b'"ok": true', self._health_request())

    def test_second_server_refuses_to_replace_an_active_socket(self):
        first = self._start_server()

        second = subprocess.run(
            self._command(),
            cwd=REPO_ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("socket is already served by another process", second.stderr)
        self.assertIsNone(first.poll())
        self.assertTrue(stat.S_ISSOCK(self.socket_path.stat().st_mode))
        self.assertIn(b'"ok": true', self._health_request())

    def test_sigterm_performs_graceful_socket_cleanup(self):
        process = self._start_server()

        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertFalse(os.path.lexists(self.socket_path))


if __name__ == "__main__":
    unittest.main()
