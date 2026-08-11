"""Local draft queue — the user's personal staging area of jobs they intend to
submit. Persisted as JSON under ~/.gpuviz/ so it survives restarts. This is the
"build a queue of my own jobs and submit them from the GUI" feature; nothing
here touches Slurm until the user explicitly submits.
"""
from __future__ import annotations

import json
import os
import threading

import sbatch

_HOME = os.path.expanduser("~")
STORE_DIR = os.path.join(_HOME, ".gpuviz")
STORE = os.path.join(STORE_DIR, "drafts.json")
SBATCH_DIR = os.path.join(STORE_DIR, "sbatch")  # rendered files we submit

_lock = threading.Lock()


def _ensure():
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(SBATCH_DIR, mode=0o700, exist_ok=True)
    os.chmod(STORE_DIR, 0o700)
    os.chmod(SBATCH_DIR, 0o700)


def _load() -> dict:
    _ensure()
    if not os.path.exists(STORE):
        return {"seq": 0, "items": []}
    try:
        with open(STORE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"seq": 0, "items": []}


def _save(data: dict):
    _ensure()
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def list_drafts() -> list[dict]:
    with _lock:
        return _load()["items"]


def get(draft_id: int) -> dict | None:
    with _lock:
        for it in _load()["items"]:
            if it["id"] == draft_id:
                return it
    return None


def create(fields: dict | None = None, kind: str = "normal") -> dict:
    with _lock:
        data = _load()
        data["seq"] += 1
        base = sbatch.holder_defaults() if kind == "holder" else sbatch.new_draft_defaults()
        item = {**base, **(fields or {}), "id": data["seq"], "kind": kind,
                "submitted_job_id": None}
        data["items"].append(item)
        _save(data)
        return item


def update(draft_id: int, fields: dict) -> dict | None:
    with _lock:
        data = _load()
        for it in data["items"]:
            if it["id"] == draft_id:
                # id and kind are not user-editable
                for k, v in fields.items():
                    if k not in ("id", "kind"):
                        it[k] = v
                _save(data)
                return it
    return None


def delete(draft_id: int) -> bool:
    with _lock:
        data = _load()
        n = len(data["items"])
        data["items"] = [it for it in data["items"] if it["id"] != draft_id]
        if len(data["items"]) != n:
            _save(data)
            return True
    return False


def mark_submitted(draft_id: int, job_id: str):
    with _lock:
        data = _load()
        for it in data["items"]:
            if it["id"] == draft_id:
                it["submitted_job_id"] = job_id
                _save(data)
                return


def write_sbatch_file(draft: dict) -> str:
    """Render a draft to a file under SBATCH_DIR and return its path."""
    _ensure()
    path = os.path.join(SBATCH_DIR, f"draft-{draft['id']}.sbatch")
    with open(path, "w") as f:
        f.write(sbatch.render(draft))
    os.chmod(path, 0o600)
    return path
