import threading
import unittest

import slurm


class SlurmCacheTests(unittest.TestCase):
    def setUp(self):
        with slurm._cache_lock:
            slurm._cache.clear()
            slurm._cache_inflight.clear()

    def test_concurrent_misses_are_coalesced(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        results = []

        def load():
            calls.append(1)
            entered.set()
            release.wait(timeout=2)
            return {"ok": True}

        workers = [threading.Thread(target=lambda: results.append(
            slurm._cached("same", 30, load))) for _ in range(2)]
        workers[0].start()
        self.assertTrue(entered.wait(timeout=1))
        workers[1].start()
        release.set()
        for worker in workers:
            worker.join(timeout=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_invalidation_does_not_publish_an_inflight_stale_value(self):
        entered = threading.Event()
        release = threading.Event()

        def stale_load():
            entered.set()
            release.wait(timeout=2)
            return "stale"

        worker = threading.Thread(target=lambda: slurm._cached("state", 30, stale_load))
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        slurm.invalidate()
        release.set()
        worker.join(timeout=2)

        self.assertEqual(slurm._cached("state", 30, lambda: "fresh"), "fresh")


if __name__ == "__main__":
    unittest.main()
