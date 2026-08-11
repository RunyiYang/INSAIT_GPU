"""Project registry — the list of repos this WebUI manages.

The user registers project repos by path; each repo carries its own job
definitions in a `gpuviz.toml` (see catalog.py). This is just the registry;
reading job definitions lives in catalog.py.
"""
from __future__ import annotations

import json
import os
import threading

_HOME = os.path.expanduser("~")
STORE_DIR = os.path.join(_HOME, ".gpuviz")
STORE = os.path.join(STORE_DIR, "projects.json")
_lock = threading.Lock()


def _load() -> dict:
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STORE_DIR, 0o700)
    if not os.path.exists(STORE):
        return {"seq": 0, "items": []}
    try:
        with open(STORE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"seq": 0, "items": []}


def _save(data: dict):
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STORE_DIR, 0o700)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def list_projects() -> list[dict]:
    with _lock:
        return _load()["items"]


def get(pid: int) -> dict | None:
    with _lock:
        for it in _load()["items"]:
            if it["id"] == pid:
                return it
    return None


def add(path: str, name: str | None = None) -> dict:
    """Register a repo. Returns {id,name,path,exists,has_config}."""
    path = os.path.abspath(os.path.expanduser(path.strip()))
    with _lock:
        data = _load()
        for it in data["items"]:
            if it["path"] == path:
                return it  # already registered
        data["seq"] += 1
        item = {
            "id": data["seq"],
            "name": name or os.path.basename(path) or path,
            "path": path,
        }
        data["items"].append(item)
        _save(data)
        return item


def remove(pid: int) -> bool:
    with _lock:
        data = _load()
        n = len(data["items"])
        data["items"] = [it for it in data["items"] if it["id"] != pid]
        if len(data["items"]) != n:
            _save(data)
            return True
    return False


def status(item: dict) -> dict:
    """Lightweight existence/config probe for the project list UI."""
    p = item["path"]
    cfg = None
    for cand in ("gpuviz.toml", os.path.join(".gpuviz", "jobs.toml")):
        if os.path.isfile(os.path.join(p, cand)):
            cfg = cand
            break
    return {**item, "exists": os.path.isdir(p), "config": cfg}
