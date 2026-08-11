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
