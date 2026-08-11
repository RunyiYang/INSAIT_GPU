"""Read a repo's `gpuviz.toml` job catalog.

Each registered project repo declares the jobs it wants to submit. We merge the
[defaults] table into every [[job]], resolve the run command, and surface data
dependencies (`needs`) for presence checks. tomllib (stdlib, py3.11+) parses it.
"""
from __future__ import annotations

import os
import tomllib

# Fields that flow straight into the sbatch renderer.
_PASS = ("name", "gpus", "gpu_type", "nodes", "cpus", "mem", "time", "partition",
         "qos", "account", "nodelist", "exclude", "array", "open_mode", "output")


def config_path(repo: str) -> str | None:
    for cand in ("gpuviz.toml", os.path.join(".gpuviz", "jobs.toml")):
        p = os.path.join(repo, cand)
        if os.path.isfile(p):
            return p
    return None


def _expand(val, repo: str):
    if isinstance(val, str):
        return os.path.expandvars(val.replace("$REPO", repo))
    return val


def _resolve_path(p: str, repo: str) -> str:
    p = os.path.expandvars(p)
    if not os.path.isabs(p):
        p = os.path.join(repo, p)
    return os.path.normpath(p)


def load(repo: str) -> dict:
    """Return {project, defaults, jobs:[spec,...], path, error?}."""
    cfg = config_path(repo)
    if not cfg:
        return {"project": os.path.basename(repo), "jobs": [], "path": None,
                "error": "no gpuviz.toml found"}
    try:
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        return {"project": os.path.basename(repo), "jobs": [], "path": cfg,
                "error": f"parse error: {e}"}

    proj = (raw.get("project") or {})
    pname = proj.get("name") or os.path.basename(repo)
    defaults = raw.get("defaults") or {}
    jobs = []
    for j in raw.get("job") or []:
        merged = {**defaults, **j}
        key = merged.get("key") or merged.get("name")
        if not key:
            continue
        spec = {
            "key": key,
            "kind": merged.get("kind", "normal"),
            "setup": merged.get("setup", ""),
            "command": merged.get("command", ""),
            "script": merged.get("script", ""),
            "script_file": merged.get("script_file", ""),
            "needs": _norm_needs(merged.get("needs"), repo),
        }
        for f in _PASS:
            if f in merged:
                spec[f] = _expand(merged[f], repo)
        spec.setdefault("name", key)
        jobs.append(spec)
    return {"project": pname, "defaults": defaults, "jobs": jobs, "path": cfg}


def _norm_needs(needs, repo: str) -> list[dict]:
    out = []
    for n in needs or []:
        if isinstance(n, str):
            n = {"path": n}
        if not isinstance(n, dict) or "path" not in n:
            continue
        out.append({
            "path": _resolve_path(n["path"], repo),
            "raw_path": n["path"],
            "min_gb": n.get("min_gb"),
            "site": n.get("site"),
            "produced_by": n.get("produced_by"),
        })
    return out
