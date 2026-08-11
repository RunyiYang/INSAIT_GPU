import json
import os
import tempfile
import time
import unittest
from unittest import mock

# Keep migration restore/save operations away from the operator's real state.
_TEST_HOME = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_HOME.name

import migrate


class MigrationJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store_dir = migrate._STORE_DIR
        self.original_store = migrate._STORE
        self.original_items = migrate._migrations
        self.original_seq = migrate._seq
        migrate._STORE_DIR = self.tmp.name
        migrate._STORE = os.path.join(self.tmp.name, "migrations.json")
        migrate._migrations = {}
        migrate._seq = 0

    def tearDown(self):
        migrate._STORE_DIR = self.original_store_dir
        migrate._STORE = self.original_store
        migrate._migrations = self.original_items
        migrate._seq = self.original_seq
        self.tmp.cleanup()

    def restore_item(self, state):
        item = {
            "id": 1, "src_job_id": "10", "target_node": "gpu-1",
            "new_job_id": "11", "state": state, "log": [],
            "created_at": time.time(), "updated_at": time.time(), "timeout_s": 1800,
        }
        with open(migrate._STORE, "w") as f:
            json.dump({"seq": 1, "items": [item]}, f)
        migrate._restore()
        return migrate._migrations[1]

    def test_create_is_journaled_and_public_result_is_a_copy(self):
        with mock.patch.object(migrate, "_ensure_worker"):
            result = migrate.create("10", "gpu-1", lambda: "11")
        self.assertEqual(result["state"], "waiting")
        result["state"] = "tampered"
        self.assertEqual(migrate.list_migrations()[0]["state"], "waiting")
        with open(migrate._STORE) as f:
            saved = json.load(f)
        self.assertEqual(saved["items"][0]["new_job_id"], "11")
        self.assertEqual(os.stat(migrate._STORE).st_mode & 0o777, 0o600)

    def test_timed_out_clone_cancellation_retries_without_touching_original(self):
        item = {
            "id": 1, "src_job_id": "10", "target_node": "gpu-1",
            "new_job_id": "11", "state": "waiting", "log": [],
            "created_at": time.time() - 10, "updated_at": time.time(), "timeout_s": 1,
        }
        migrate._migrations[1] = item
        with mock.patch.object(migrate.slurm, "job_state", return_value="PENDING"), \
             mock.patch.object(migrate.slurm, "cancel", side_effect=[RuntimeError("busy"), None]) as cancel:
            migrate._step(item)
            self.assertEqual(item["state"], "cancelling")
            migrate._step(item)
        self.assertEqual(item["state"], "failed")
        self.assertEqual([call.args[0] for call in cancel.call_args_list], ["11", "11"])

    def test_running_clone_completes_make_before_break(self):
        item = {
            "id": 1, "src_job_id": "10", "target_node": "gpu-1",
            "new_job_id": "11", "state": "waiting", "log": [],
            "created_at": time.time(), "updated_at": time.time(), "timeout_s": 1800,
        }
        migrate._migrations[1] = item
        with mock.patch.object(migrate.slurm, "job_state", return_value="RUNNING"), \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            migrate._step(item)
        self.assertEqual(item["state"], "done")
        cancel.assert_called_once_with("10")

    def test_finishing_clone_never_cancels_healthy_original(self):
        for clone_state in ("COMPLETING", "COMPLETED"):
            with self.subTest(clone_state=clone_state):
                item = {
                    "id": 1, "src_job_id": "10", "target_node": "gpu-1",
                    "new_job_id": "11", "state": "waiting", "log": [],
                    "created_at": time.time(), "updated_at": time.time(),
                    "timeout_s": 1800,
                }
                migrate._migrations[1] = item
                with mock.patch.object(migrate.slurm, "job_state",
                                       return_value=clone_state), \
                     mock.patch.object(migrate.slurm, "cancel") as cancel:
                    migrate._step(item)
                self.assertEqual(item["state"], "failed")
                cancel.assert_not_called()

    def test_abort_claimed_during_submit_cancels_late_clone(self):
        def submit_then_return():
            self.assertTrue(migrate.abort(1))
            return "11"

        with mock.patch.object(migrate, "_ensure_worker"), \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            result = migrate.create("10", "gpu-1", submit_then_return)
        self.assertEqual(result["state"], "aborted")
        cancel.assert_called_once_with("11")

    def test_abort_wins_race_with_stale_running_lookup(self):
        item = {
            "id": 1, "src_job_id": "10", "target_node": "gpu-1",
            "new_job_id": "11", "state": "waiting", "log": [],
            "created_at": time.time(), "updated_at": time.time(), "timeout_s": 1800,
        }
        migrate._migrations[1] = item

        def abort_during_lookup(_):
            self.assertTrue(migrate.abort(1))
            return "RUNNING"

        with mock.patch.object(migrate, "_ensure_worker"), \
             mock.patch.object(migrate.slurm, "job_state", side_effect=abort_during_lookup), \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            migrate._step(item)
        self.assertEqual(item["state"], "aborted")
        cancel.assert_called_once_with("11")

    def test_swapping_state_retries_original_cancellation(self):
        item = {
            "id": 1, "src_job_id": "10", "target_node": "gpu-1",
            "new_job_id": "11", "state": "swapping", "log": [],
            "created_at": time.time(), "updated_at": time.time(), "timeout_s": 1800,
        }
        migrate._migrations[1] = item
        with mock.patch.object(migrate.slurm, "job_state", return_value="RUNNING"), \
             mock.patch.object(migrate.slurm, "cancel", side_effect=[RuntimeError("busy"), None]) as cancel:
            migrate._step(item)
            self.assertEqual(item["state"], "swapping")
            self.assertFalse(migrate.abort(1))
            migrate._step(item)
        self.assertEqual(item["state"], "done")
        self.assertEqual([call.args[0] for call in cancel.call_args_list], ["10", "10"])

    def test_restart_resumes_swapping_by_cancelling_only_original(self):
        item = self.restore_item("swapping")

        with mock.patch.object(migrate.slurm, "job_state", return_value="RUNNING") as state, \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            migrate._step(item)

        self.assertEqual(item["state"], "done")
        state.assert_called_once_with("10")
        cancel.assert_called_once_with("10")

    def test_restart_resumes_timeout_cancellation_without_touching_original(self):
        item = self.restore_item("cancelling")

        with mock.patch.object(migrate.slurm, "job_state", return_value="PENDING") as state, \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            migrate._step(item)

        self.assertEqual(item["state"], "failed")
        state.assert_called_once_with("11")
        cancel.assert_called_once_with("11")

    def test_restart_resumes_abort_without_touching_original(self):
        item = self.restore_item("aborting")

        with mock.patch.object(migrate.slurm, "job_state", return_value="PENDING") as state, \
             mock.patch.object(migrate.slurm, "cancel") as cancel:
            migrate._step(item)

        self.assertEqual(item["state"], "aborted")
        state.assert_called_once_with("11")
        cancel.assert_called_once_with("11")


if __name__ == "__main__":
    unittest.main()
