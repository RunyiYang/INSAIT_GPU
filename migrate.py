"""Make-before-break job migration.

Move a job onto a target node without ever giving up the old allocation: submit
the new job (pinned to the node), and only `scancel` the original once the new
one is actually RUNNING. A single background daemon thread watches all active
migrations. If the new job fails or you abort, the original is left untouched.

The engine is decoupled from *how* the new job is created: the caller passes a
`submit_fn()` returning the new job id (clone of the old job, or a fresh catalog
job / holder-swap). State is journaled so a restarted server resumes watching
submitted replacement jobs instead of silently orphaning them.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time

import slurm

# terminal Slurm states that mean the new job will never run
_DEAD = {"COMPLETING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
         "NODE_FAIL", "BOOT_FAIL", "OUT_OF_MEMORY", "DEADLINE",
         "PREEMPTED", "SPECIAL_EXIT"}
_RUN_OK = {"RUNNING"}
_TERMINAL = {"done", "failed", "aborted"}
_ACTIVE = {"waiting", "cancelling", "aborting", "swapping"}

_migrations: dict[int, dict] = {}
_seq = 0
_lock = threading.RLock()
_worker: threading.Thread | None = None
_STORE_DIR = os.path.expanduser("~/.gpuviz")
_STORE = os.path.join(_STORE_DIR, "migrations.json")


def _now() -> float:
    return time.time()


def _save():
    with _lock:
        os.makedirs(_STORE_DIR, mode=0o700, exist_ok=True)
        os.chmod(_STORE_DIR, 0o700)
        tmp = _STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"seq": _seq, "items": list(_migrations.values())}, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _STORE)


def _log(m: dict, msg: str):
    with _lock:
        m["log"].append({"t": round(_now()), "msg": msg})
        m["updated_at"] = _now()
        _save()


def _restore():
    global _seq
    try:
        with open(_STORE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    changed = False
    for raw in items:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
            continue
        m = dict(raw)
        if not isinstance(m.get("log"), list):
            m["log"] = []
        state = m.get("state")
        if state == "submitting":
            m["state"] = "failed"
            m["log"].append({"t": round(_now()),
                             "msg": "server restarted during submission; check the queue manually"})
            changed = True
        elif state == "swapping":
            m["log"].append({"t": round(_now()),
                             "msg": "server restarted during swap; reconciling the original job"})
            changed = True
        elif state == "aborting":
            m["log"].append({"t": round(_now()),
                             "msg": "server restarted during abort; resuming clone cancellation"})
            changed = True
        _migrations[m["id"]] = m
        _seq = max(_seq, m["id"])
    try:
        saved_seq = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        saved_seq = 0
    _seq = max(_seq, saved_seq)
    if changed:
        _save()


def list_migrations() -> list[dict]:
    with _lock:
        return copy.deepcopy(sorted(_migrations.values(), key=lambda x: -x["id"]))


def create(src_job_id, target_node, submit_fn, label: str = "",
           timeout_s: float = 1800) -> dict:
    """Start a migration. submit_fn() must submit the new job and return its id.
    The old job is NOT touched until the new one is RUNNING. If the new job has
    not started within timeout_s, give up: cancel the still-pending clone and
    keep the original (so a late start can't leave both running)."""
    global _seq
    with _lock:
        _seq += 1
        m = {
            "id": _seq,
            "src_job_id": str(src_job_id),
            "target_node": target_node,
            "label": label,
            "new_job_id": None,
            # submitting|waiting|cancelling|aborting|swapping|done|failed|aborted
            "state": "submitting",
            "log": [],
            "created_at": _now(),
            "updated_at": _now(),
            "timeout_s": timeout_s,
        }
        _migrations[m["id"]] = m
        _save()
    _log(m, f"submitting new job → {target_node}")
    try:
        new_id = submit_fn()
    except Exception as e:  # noqa: BLE001
        with _lock:
            abort_requested = m["state"] == "aborting"
            m["state"] = "failed"
            _log(m, (f"submission result was lost during abort: {e}; original kept, "
                     "check the queue for an untracked clone"
                     if abort_requested else
                     f"submit failed: {e}; original job left intact; check the queue if uncertain"))
        return copy.deepcopy(m)

    with _lock:
        m["new_job_id"] = str(new_id)
        abort_requested = m["state"] == "aborting"
        if abort_requested:
            _log(m, f"new job {new_id} appeared after abort was requested; cancelling it")
        else:
            m["state"] = "waiting"
            _log(m, f"submitted new job {new_id}; waiting for it to RUN before "
                    f"cancelling {src_job_id}")

    if abort_requested:
        try:
            slurm.cancel(str(new_id))
        except Exception as e:  # noqa: BLE001
            _log(m, f"abort cancellation will be retried: {e}")
        else:
            with _lock:
                if m["state"] == "aborting":
                    m["state"] = "aborted"
                    _log(m, f"aborted: new job {new_id} stopped; original {src_job_id} kept")
    _ensure_worker()
    return copy.deepcopy(m)


def abort(mid: int) -> bool:
    with _lock:
        m = _migrations.get(mid)
        if not m or m["state"] in _TERMINAL | {"aborting", "swapping"}:
            return False
        m["state"] = "aborting"
        m["_misses"] = 0
        new_id = m.get("new_job_id")
        _log(m, "abort requested; the original job will be kept")
    if not new_id:
        # Submission is still in flight. create() will observe the claimed
        # aborting state and cancel the returned job ID before it can be watched.
        _ensure_worker()
        return True
    try:
        slurm.cancel(new_id)
    except Exception as e:  # noqa: BLE001
        _log(m, f"abort cancellation will be retried: {e}")
        _ensure_worker()
        return True
    with _lock:
        if m["state"] == "aborting":
            m["state"] = "aborted"
            _log(m, f"aborted: new job {new_id} stopped; original {m['src_job_id']} kept")
    return True


def clear_finished():
    with _lock:
        for mid in [k for k, v in _migrations.items()
                    if v["state"] in ("done", "failed", "aborted")]:
            del _migrations[mid]
        _save()


def _retry_log(m: dict, key: str, message: str, every_s: float = 60):
    """Journal recurring controller errors without growing the file every 5s."""
    with _lock:
        now = _now()
        if now - m.get(key, 0) < every_s:
            return
        m[key] = now
        _log(m, message)


def _reconcile_swap(m: dict):
    """Finish the committed half of a swap; abort is no longer safe here."""
    with _lock:
        if m["state"] != "swapping":
            return
        src_id = m["src_job_id"]
    src_state = slurm.job_state(src_id)
    with _lock:
        if m["state"] != "swapping":
            return
    if src_state is None:
        with _lock:
            if m["state"] == "swapping":
                m["state"] = "done"
                _log(m, "migration complete; original job is no longer queued")
        return
    try:
        slurm.cancel(src_id)
    except Exception as e:  # noqa: BLE001
        _retry_log(m, "_swap_error_at",
                   f"new job is up; retrying cancellation of original {src_id}: {e}")
        return
    with _lock:
        if m["state"] == "swapping":
            m["state"] = "done"
            _log(m, "migration complete (make-before-break, no gap)")


def _step(m: dict):
    with _lock:
        state = m.get("state")
        new_id = m.get("new_job_id")
    if state not in _ACTIVE or not new_id:
        return
    if state == "swapping":
        _reconcile_swap(m)
        return

    st = slurm.job_state(new_id)
    # Any action may have raced with the controller lookup. Only the winner of
    # this state check is allowed to perform the corresponding Slurm mutation.
    with _lock:
        if m["state"] != state:
            return

    if state == "aborting":
        if st in _DEAD:
            with _lock:
                if m["state"] == "aborting":
                    m["state"] = "aborted"
                    _log(m, f"aborted clone ended as {st}; original kept running")
            return
        if st is None:
            with _lock:
                if m["state"] != "aborting":
                    return
                m["_misses"] = m.get("_misses", 0) + 1
                if m["_misses"] >= 4:
                    m["state"] = "aborted"
                    _log(m, "aborted clone is no longer queued; original kept running")
            return
        try:
            slurm.cancel(new_id)
        except Exception as e:  # noqa: BLE001
            _retry_log(m, "_abort_error_at", f"retrying abort cancellation: {e}")
            return
        with _lock:
            if m["state"] == "aborting":
                m["state"] = "aborted"
                _log(m, f"aborted: new job {new_id} stopped; original {m['src_job_id']} kept")
        return

    if state == "cancelling":
        if st in _DEAD:
            with _lock:
                if m["state"] == "cancelling":
                    m["state"] = "failed"
                    _log(m, f"timed-out clone ended as {st}; original kept running")
            return
        if st is None:
            with _lock:
                if m["state"] != "cancelling":
                    return
                m["_misses"] = m.get("_misses", 0) + 1
                if m["_misses"] >= 4:
                    m["state"] = "failed"
                    _log(m, "timed-out clone is no longer queued; original kept running")
            return
        try:
            slurm.cancel(new_id)
        except Exception as e:  # noqa: BLE001
            _retry_log(m, "_cancel_error_at",
                       f"retrying cancellation of timed-out clone: {e}")
            return
        with _lock:
            if m["state"] == "cancelling":
                m["state"] = "failed"
                _log(m, "timed-out clone cancelled; original kept running")
        return

    # From this point the state observed before the lookup was "waiting".
    if st in _RUN_OK:
        with _lock:
            if m["state"] != "waiting":
                return
            m["state"] = "swapping"
            _log(m, f"new job {new_id} is {st} → cancelling original {m['src_job_id']}")
        _reconcile_swap(m)
    elif st in _DEAD:
        with _lock:
            if m["state"] == "waiting":
                m["state"] = "failed"
                _log(m, f"new job ended as {st}; original kept running")
    elif (timeout := (m.get("timeout_s") or 0)) and (_now() - m["created_at"]) > timeout:
        # Check the state first: a job that became RUNNING at the timeout
        # boundary must complete the swap, not be mistaken for a late pending job.
        with _lock:
            if m["state"] != "waiting":
                return
            m["state"] = "cancelling"
            m["_misses"] = 0
            mins = round(timeout / 60)
            _log(m, f"timed out after {mins} minutes; cancelling clone and keeping original")
        try:
            slurm.cancel(new_id)
        except Exception as e:  # noqa: BLE001
            _retry_log(m, "_cancel_error_at",
                       f"initial clone cancellation failed; watcher will retry: {e}")
            return
        with _lock:
            if m["state"] == "cancelling":
                m["state"] = "failed"
                _log(m, "timed-out clone cancelled; original kept running")
    elif st is None:
        with _lock:
            if m["state"] != "waiting":
                return
            m["_misses"] = m.get("_misses", 0) + 1
            if m["_misses"] >= 4:
                m["state"] = "failed"
                _log(m, "lost track of new job; original kept running")
    else:
        with _lock:
            if m["state"] == "waiting":
                m["_misses"] = 0  # still PENDING/CONFIGURING


def _loop():
    while True:
        time.sleep(5)
        with _lock:
            active = [m for m in _migrations.values()
                      if m["state"] in _ACTIVE]
        for m in active:
            try:
                _step(m)
            except Exception as e:  # noqa: BLE001
                _retry_log(m, "_watch_error_at", f"watcher error; will retry: {e}")


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True, name="migrate-worker")
            _worker.start()


_restore()
if any(m.get("state") in _ACTIVE for m in _migrations.values()):
    _ensure_worker()
