import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

# Importing the server also restores migration journals and may start its
# watcher. Keep the entire test process away from the operator's real state.
_TEST_HOME = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_HOME.name

import server


class AuthTokenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "auth-token")
        self.ack_path = os.path.join(self.tmp.name, "weak-auth-ack")

    def tearDown(self):
        self.tmp.cleanup()

    def write_token(self, token="a" * 40, mode=0o600):
        with open(self.path, "w") as f:
            f.write(token)
        os.chmod(self.path, mode)

    def write_ack(self, text="I accept weak public authentication", mode=0o600):
        with open(self.ack_path, "w") as f:
            f.write(text)
        os.chmod(self.ack_path, mode)

    def test_loads_secure_token_once(self):
        self.write_token("strong-token-" + "x" * 32)
        self.assertEqual(server.load_auth_token(self.path), b"strong-token-" + b"x" * 32)

    def test_weak_token_requires_valid_acknowledgement(self):
        self.write_token("12345678")
        self.assertEqual(server.auth_token_min_chars(None), 32)
        with self.assertRaisesRegex(ValueError, "at least 32"):
            server.load_auth_token(self.path)
        self.write_ack()
        min_chars = server.auth_token_min_chars(self.ack_path)
        self.assertEqual(min_chars, 8)
        self.assertEqual(server.load_auth_token(self.path, min_chars=min_chars), b"12345678")

    def test_missing_or_wrong_acknowledgement_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot open"):
            server.auth_token_min_chars(self.ack_path)

        for text in (
            "I understand",
            "I accept weak public authentication\n",
            " I accept weak public authentication",
            "I accept weak public authentication\0",
        ):
            with self.subTest(text=text):
                self.write_ack(text)
                with self.assertRaisesRegex(ValueError, "must contain exactly"):
                    server.auth_token_min_chars(self.ack_path)

    def test_symlink_acknowledgement_is_rejected(self):
        target = os.path.join(self.tmp.name, "ack-target")
        with open(target, "w") as f:
            f.write("I accept weak public authentication")
        os.chmod(target, 0o600)
        os.symlink(target, self.ack_path)
        with self.assertRaisesRegex(ValueError, "cannot open"):
            server.auth_token_min_chars(self.ack_path)

    def test_overexposed_acknowledgement_is_rejected(self):
        self.write_ack(mode=0o640)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            server.auth_token_min_chars(self.ack_path)

    def test_rejects_short_or_overexposed_token_files(self):
        self.write_token("short")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            server.load_auth_token(self.path)

        self.write_token(mode=0o640)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            server.load_auth_token(self.path)

    def test_rejects_non_regular_token_file(self):
        os.mkdir(self.path)
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(ValueError, "regular file"):
            server.load_auth_token(self.path)

    def test_auth_user_validation_accepts_default_and_rejects_unsafe_values(self):
        self.assertEqual(server.validate_auth_user(server.slurm.USER), server.slurm.USER.encode())
        for value in ("", "space user", "user@example.com", "x" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.validate_auth_user(value)


class AuthServerTests(unittest.TestCase):
    token = "correct-" + "z" * 32
    auth_user = "gpu.operator"

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.GpuvizHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.auth_token = cls.token.encode()
        cls.httpd.auth_user = cls.auth_user.encode()
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def authorization(cls, username=None, token=None):
        username = cls.auth_user if username is None else username
        credential = f"{username}:{token if token is not None else cls.token}".encode()
        return "Basic " + base64.b64encode(credential).decode()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    def test_authentication_gates_every_implemented_http_method(self):
        requests = (
            ("GET", "/", None, {}),
            ("GET", "/static/style.css", None, {}),
            ("GET", "/api/health", None, {}),
            ("HEAD", "/api/health", None, {}),
            ("POST", "/api/preview", "{}", {"Content-Type": "application/json"}),
            ("PUT", "/api/preview", "{}", {"Content-Type": "application/json"}),
            ("DELETE", "/api/projects/999", None, {}),
        )
        with mock.patch.object(server.sbatch, "render") as render:
            for method, path, body, headers in requests:
                with self.subTest(method=method, path=path):
                    status, response_headers, payload = self.request(method, path, body, headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(
                        response_headers["WWW-Authenticate"],
                        'Basic realm="GPU Visualizer", charset="UTF-8"',
                    )
                    if method == "HEAD":
                        self.assertEqual(payload, b"")
            render.assert_not_called()

    def test_wrong_username_token_and_malformed_header_are_rejected(self):
        headers = (
            {"Authorization": self.authorization(username="admin")},
            {"Authorization": self.authorization(username="gpuviz")},
            {"Authorization": self.authorization(token="wrong-" + "x" * 32)},
            {"Authorization": "Basic not-base64!"},
        )
        for header in headers:
            with self.subTest(header=header):
                status, _, _ = self.request("GET", "/api/health", headers=header)
                self.assertEqual(status, 401)

    def test_forwarded_http_is_rejected_without_basic_challenge(self):
        headers = {
            "Authorization": self.authorization(),
            "Content-Type": "application/json",
            "X-Forwarded-Proto": "http, https",
        }
        with mock.patch.object(server.sbatch, "render") as render:
            status, response_headers, body = self.request(
                "POST", "/api/preview", body="{}", headers=headers,
            )
        self.assertEqual(status, 403)
        self.assertNotIn("WWW-Authenticate", response_headers)
        self.assertNotIn("Strict-Transport-Security", response_headers)
        self.assertIn("HTTPS", json.loads(body)["error"])
        render.assert_not_called()

    def test_forwarded_https_is_case_insensitive_and_adds_hsts(self):
        status, headers, _ = self.request(
            "GET", "/api/health", headers={"X-Forwarded-Proto": "HTTPS, http"},
        )
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")

        request_headers = {
            "Authorization": self.authorization(),
            "X-Forwarded-Proto": "HTTPS, http",
        }
        status, headers, _ = self.request("GET", "/api/health", headers=request_headers)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")

    def test_basic_auth_mutation_log_does_not_trust_access_email_header(self):
        headers = {
            "Authorization": self.authorization(),
            "Cf-Access-Authenticated-User-Email": "spoofed@example.com",
        }
        with mock.patch.object(server.monitors, "delete", return_value=True), \
             mock.patch("builtins.print") as audit:
            status, _, _ = self.request("DELETE", "/api/monitors/run%3A42", headers=headers)
        self.assertEqual(status, 200)
        event = json.loads(audit.call_args.args[0])
        self.assertEqual(event["actor"], "basic:gpu.operator")

    def test_valid_credentials_allow_static_api_and_mutation_routes(self):
        auth = {"Authorization": self.authorization()}
        status, _, body = self.request("GET", "/api/health", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _, body = self.request("GET", "/static/style.css", headers=auth)
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 100)

        headers = {**auth, "Content-Type": "application/json"}
        status, _, body = self.request("POST", "/api/preview", body="{}", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("sbatch", json.loads(body))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.GpuvizHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    def test_health_is_lightweight_and_hardened(self):
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_forwarded_https_adds_hsts_without_enabling_app_auth(self):
        status, headers, _ = self.request(
            "GET", "/api/health", headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")

    def test_static_path_cannot_escape_static_root(self):
        status, _, body = self.request("GET", "/static/../server.py")
        self.assertEqual(status, 404)
        self.assertNotIn(b"ThreadingHTTPServer", body)

    def test_static_assets_support_conditional_requests(self):
        status, headers, body = self.request("GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 100)
        self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")
        etag = headers["ETag"]
        status, _, body = self.request("GET", "/static/style.css", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_mutations_require_json(self):
        status, _, body = self.request("POST", "/api/preview", body="x", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])

    def test_cross_site_mutation_is_rejected(self):
        headers = {"Content-Type": "application/json", "Origin": "https://evil.example"}
        status, _, _ = self.request("POST", "/api/preview", body="{}", headers=headers)
        self.assertEqual(status, 403)

    def test_draft_defaults_do_not_persist_a_draft(self):
        status, _, body = self.request("GET", "/api/drafts/defaults?kind=holder")
        draft = json.loads(body)["draft"]
        self.assertEqual(status, 200)
        self.assertEqual(draft["kind"], "holder")
        self.assertNotIn("id", draft)
        self.assertIn("HOLDER-swap-me", draft["name"])

    def test_encoded_monitor_key_can_be_deleted(self):
        with mock.patch.object(server.monitors, "delete", return_value=True) as delete:
            status, _, body = self.request("DELETE", "/api/monitors/run%3A42")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        delete.assert_called_once_with("run:42")


class MigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.migration = {
            "id": 1,
            "src_job_id": "101",
            "target_node": "gpu-node-2",
            "new_job_id": "202",
            "state": "waiting",
            "log": [],
            "created_at": 100,
            "updated_at": 100,
            "timeout_s": 10,
        }

    @staticmethod
    def record_log(migration, message):
        migration["log"].append({"msg": message})

    def test_running_clone_at_timeout_boundary_swaps_instead_of_cancelling_clone(self):
        cancelled = []
        with (
            mock.patch.object(server.migrate, "_now", return_value=111),
            mock.patch.object(server.migrate, "_log", side_effect=self.record_log),
            mock.patch.object(server.migrate.slurm, "job_state", return_value="RUNNING"),
            mock.patch.object(server.migrate.slurm, "cancel", side_effect=cancelled.append),
        ):
            server.migrate._step(self.migration)

        self.assertEqual(self.migration["state"], "done")
        self.assertEqual(cancelled, ["101"])

    def test_timed_out_pending_clone_is_cancelled_but_original_is_kept(self):
        cancelled = []
        with (
            mock.patch.object(server.migrate, "_now", return_value=111),
            mock.patch.object(server.migrate, "_log", side_effect=self.record_log),
            mock.patch.object(server.migrate.slurm, "job_state", return_value="PENDING"),
            mock.patch.object(server.migrate.slurm, "cancel", side_effect=cancelled.append),
        ):
            server.migrate._step(self.migration)

        self.assertEqual(self.migration["state"], "failed")
        self.assertEqual(cancelled, ["202"])

    def test_abort_cancel_failure_stays_active_for_retry(self):
        with server.migrate._lock:
            previous = server.migrate._migrations.get(1)
            server.migrate._migrations[1] = self.migration
        try:
            with (
                mock.patch.object(server.migrate, "_log", side_effect=self.record_log),
                mock.patch.object(server.migrate.slurm, "cancel", side_effect=RuntimeError("down")),
                mock.patch.object(server.migrate, "_ensure_worker"),
            ):
                self.assertTrue(server.migrate.abort(1))
            self.assertEqual(self.migration["state"], "aborting")
            self.assertTrue(any("retried" in row["msg"] for row in self.migration["log"]))
        finally:
            with server.migrate._lock:
                if previous is None:
                    server.migrate._migrations.pop(1, None)
                else:
                    server.migrate._migrations[1] = previous


if __name__ == "__main__":
    unittest.main()
