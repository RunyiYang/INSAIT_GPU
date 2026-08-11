"""Submit a catalog job (definition from a repo's gpuviz.toml) with optional
GUI overrides, and record provenance.

Per the design: gpuviz.toml is the initial definition; every submission writes
the *actual* sbatch (overrides applied) as a snapshot inside the repo under
.gpuviz/submissions/, so each run is reproducible and the copy is deletable
later. A central index links project/job_key/job_id/snapshot/log paths so the
management view can tie a live Slurm job back to where it came from.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime

import logs
import sbatch
import slurm

_HOME = os.path.expanduser("~")
STORE_DIR = os.path.join(_HOME, ".gpuviz")
INDEX = os.path.join(STORE_DIR, "submissions.json")
_lock = threading.Lock()

# Which fields a GUI override is allowed to change at submit time.
OVERRIDE_FIELDS = ("name", "gpus", "gpu_type", "nodes", "cpus", "mem", "time",
                   "partition", "qos", "account", "nodelist", "exclude", "array")


# ---- central submission index ---------------------------------------------
def _load() -> dict:
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STORE_DIR, 0o700)
    if not os.path.exists(INDEX):
        return {"seq": 0, "items": []}
    try:
        with open(INDEX) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"seq": 0, "items": []}


def _save(data: dict):
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, INDEX)


def list_submissions() -> list[dict]:
    with _lock:
        return list(reversed(_load()["items"]))  # newest first


def by_job_id() -> dict:
    """Map base job id -> submission record, for annotating the jobs view."""
    out = {}
    for s in list_submissions():
        if s.get("job_id"):
            out[str(s["job_id"]).split("_")[0]] = s
    return out


def _record(rec: dict) -> dict:
    with _lock:
        data = _load()
        data["seq"] += 1
        rec["id"] = data["seq"]
        data["items"].append(rec)
        _save(data)
        return rec


def delete_submission(sid: int, remove_snapshot: bool = True) -> bool:
    with _lock:
        data = _load()
        keep, removed = [], None
        for it in data["items"]:
            if it["id"] == sid:
                removed = it
            else:
                keep.append(it)
        if removed is None:
            return False
        data["items"] = keep
        _save(data)
    if remove_snapshot and removed.get("snapshot"):
        try:
            os.remove(removed["snapshot"])
        except OSError:
            pass
    return True


# ---- submit ----------------------------------------------------------------
def _override_args(d: dict) -> list[str]:
    """sbatch CLI flags for a script_file job (overrides win over its #SBATCH)."""
    args = []
    gpus = d.get("gpus")
    if gpus:
        gt = (d.get("gpu_type") or "").strip()
        args.append(f"--gres=gpu:{gt}:{gpus}" if gt else f"--gres=gpu:{gpus}")
    for k, flag in (("name", "--job-name"), ("nodes", "--nodes"), ("cpus", "--cpus-per-task"),
                    ("mem", "--mem"), ("time", "--time"), ("partition", "--partition"),
                    ("qos", "--qos"), ("account", "--account"), ("nodelist", "--nodelist"),
                    ("exclude", "--exclude"), ("array", "--array")):
        v = d.get(k)
        if v not in (None, "", "0") or (k in ("cpus", "nodes") and v):
            args.append(f"{flag}={v}")
    return args


def submit_spec(project: dict, spec: dict, overrides: dict | None = None) -> dict:
    """Render spec+overrides, snapshot it into the repo, submit, index it."""
    repo = project["path"]
    merged = {**spec}
    for k, v in (overrides or {}).items():
        if k in OVERRIDE_FIELDS and v not in (None, ""):
            merged[k] = v

    snap_dir = os.path.join(repo, ".gpuviz", "submissions")
    os.makedirs(snap_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    key = merged.get("key") or merged.get("name") or "job"
    file_key = re.sub(r"[^A-Za-z0-9._-]+", "-", str(key)).strip(".-")[:80] or "job"
    snap = os.path.join(snap_dir, f"{file_key}-{ts}.sbatch")

    extra = []
    if spec.get("script_file"):
        src = spec["script_file"]
        if not os.path.isabs(src):
            src = os.path.join(repo, src)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"script_file not found: {src}")
        shutil.copyfile(src, snap)
        extra = _override_args(merged)
    else:
        with open(snap, "w") as f:
            f.write(sbatch.render(merged))
    os.chmod(snap, 0o600)

    # ensure the --output directory exists (else sbatch rejects the job)
    outdir = sbatch.required_output_dir(merged)
    if outdir:
        target = outdir if os.path.isabs(outdir) else os.path.join(repo, outdir)
        os.makedirs(target, exist_ok=True)

    job_id = slurm.submit(snap, extra_args=extra, workdir=repo)

    # rename snapshot to embed the job id, then resolve log paths
    final = os.path.join(snap_dir, f"{file_key}-{ts}-job{job_id}.sbatch")
    try:
        os.replace(snap, final)
    except OSError:
        final = snap
    paths = logs.resolve_paths(job_id)

    return _record({
        "project_id": project.get("id"),
        "project": project.get("name"),
        "repo": repo,
        "job_key": key,
        "job_id": job_id,
        "name": merged.get("name", key),
        "snapshot": final,
        "sbatch_cmd": " ".join(["sbatch", *extra, os.path.relpath(final, repo)]),
        "out": paths.get("out"),
        "err": paths.get("err"),
        "submitted_at": ts,
    })
