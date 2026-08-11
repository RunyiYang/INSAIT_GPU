"""External progress monitors.

Any script in any repo can report progress (a migration, a training loop, an
eval sweep, a data-staging step) and have it shown live in the WebUI. Two ingest
paths, merged, so it works regardless of where the reporter runs:

  1. HTTP   — POST /api/monitors            (reporter can reach the server)
  2. file   — write ~/.gpuviz/monitors/<key>.json   (shared /home, no network)

A monitor that stops updating is flagged `stale` so a crashed reporter doesn't
look like it's still running. See gpuviz_monitor.py for the tiny client.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

_HOME = os.path.expanduser("~")
DIR = os.path.join(_HOME, ".gpuviz", "monitors")
_lock = threading.Lock()
_mem: dict[str, dict] = {}          # key -> record reported over HTTP

# fields a reporter may set (everything else is ignored)
_ALLOWED = {"key", "label", "repo", "kind", "status", "percent", "message",
            "step", "total", "node", "job_id"}
_KEY_RE = re.compile(r"^[\w.:-]{1,80}$")
STALE_AFTER = 90.0                  # seconds without an update -> stale


def _clean(d: dict) -> dict:
    rec = {k: d[k] for k in d if k in _ALLOWED}
    key = str(rec.get("key") or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError("invalid or missing monitor key")
    rec["key"] = key
    rec["status"] = rec.get("status") or "running"
    if "percent" in rec and rec["percent"] is not None:
        try:
            rec["percent"] = max(0.0, min(100.0, float(rec["percent"])))
        except (TypeError, ValueError):
            rec.pop("percent")
    return rec


def upsert(d: dict, when: float | None = None) -> dict:
    rec = _clean(d)
    rec["updated"] = when if when is not None else time.time()
    rec["source"] = "http"
    with _lock:
        prev = _mem.get(rec["key"])
        if prev:  # keep a stable start time + carry label/repo if omitted
            rec.setdefault("label", prev.get("label"))
            rec.setdefault("repo", prev.get("repo"))
            rec.setdefault("kind", prev.get("kind"))
            rec["started"] = prev.get("started", rec["updated"])
        else:
            rec["started"] = rec["updated"]
        _mem[rec["key"]] = rec
    return rec


def _read_files() -> dict:
    out = {}
    try:
        names = os.listdir(DIR)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        path = os.path.join(DIR, fn)
        try:
            with open(path) as f:
                rec = _clean(json.load(f))
            rec["updated"] = float(rec.get("updated") or os.path.getmtime(path))
            rec["started"] = float(rec.get("started") or rec["updated"])
            rec["source"] = "file"
            out[rec["key"]] = rec
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return out


def list_monitors() -> list[dict]:
    now = time.time()
    merged: dict[str, dict] = {}
    with _lock:
        for k, v in _mem.items():
            merged[k] = dict(v)
    for k, v in _read_files().items():
        if k not in merged or v["updated"] >= merged[k]["updated"]:
            merged[k] = v
    out = []
    for rec in merged.values():
        age = now - rec.get("updated", now)
        rec["age_s"] = round(age)
        rec["stale"] = rec.get("status") == "running" and age > STALE_AFTER
        out.append(rec)
    order = {"running": 0, "failed": 1, "done": 2}
    out.sort(key=lambda r: (order.get(r["status"], 3), -r.get("updated", 0)))
    return out


def delete(key: str) -> bool:
    ok = False
    with _lock:
        if key in _mem:
            del _mem[key]
            ok = True
    try:
        os.remove(os.path.join(DIR, f"{key}.json"))
        ok = True
    except OSError:
        pass
    return ok


def clear_finished() -> int:
    n = 0
    with _lock:
        for k in [k for k, v in _mem.items() if v.get("status") in ("done", "failed")]:
            del _mem[k]
            n += 1
    for rec in _read_files().values():
        if rec.get("status") in ("done", "failed"):
            try:
                os.remove(os.path.join(DIR, f"{rec['key']}.json"))
                n += 1
            except OSError:
                pass
    return n
