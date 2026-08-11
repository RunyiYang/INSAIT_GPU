"""Slurm data layer for gpu-visualizer.

Reads cluster GPU state and the current user's jobs via Slurm's JSON output,
and performs job-control actions (submit / cancel / hold / release / update).

Everything here is read-mostly and cached briefly so the polling frontend does
not hammer the controller. Actions are explicit and never run on a schedule.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from getpass import getuser

USER = os.environ.get("USER") or getuser()

# ---------------------------------------------------------------------------
# low-level command runner
# ---------------------------------------------------------------------------


class SlurmError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 20, cwd: str | None = None) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        raise SlurmError(f"`{' '.join(cmd)}` timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise SlurmError(f"command not found: {cmd[0]}") from e
    if p.returncode != 0:
        raise SlurmError((p.stderr or p.stdout or "").strip() or f"exit {p.returncode}")
    return p.stdout


# tiny TTL cache so repeated polls within a window reuse one slurm call
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_cache_inflight: dict[str, threading.Event] = {}
_cache_generation = 0


def _cached(key: str, ttl: float, fn):
    # Coalesce concurrent dashboard polls so several tabs do not stampede the
    # Slurm controller with the same expensive query.
    while True:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            pending = _cache_inflight.get(key)
            if pending is None:
                pending = threading.Event()
                _cache_inflight[key] = pending
                generation = _cache_generation
                break
        pending.wait()
    try:
        val = fn()
    except BaseException:
        with _cache_lock:
            _cache_inflight.pop(key, None)
            pending.set()
        raise
    with _cache_lock:
        if generation == _cache_generation:
            _cache[key] = (time.monotonic(), val)
        _cache_inflight.pop(key, None)
        pending.set()
    return val


def invalidate():
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _cache.clear()


# ---------------------------------------------------------------------------
# gres / tres parsing helpers
# ---------------------------------------------------------------------------

_GRES_RE = re.compile(r"gpu:([^:(\s]+):(\d+)")
_TRES_GPU_RE = re.compile(r"gres/gpu(?::([^=,\s]+))?=(\d+)")


def _parse_gres_gpus(gres: str | None) -> dict[str, int]:
    """'gpu:a6000:8(IDX:0-7),gpu:a100:2' -> {'a6000': 8, 'a100': 2}."""
    out: dict[str, int] = {}
    if not gres or gres == "N/A":
        return out
    for typ, n in _GRES_RE.findall(gres):
        out[typ] = out.get(typ, 0) + int(n)
    return out


def _parse_tres_gpu(tres: str | None) -> tuple[int, str | None]:
    """Return (total gpu count, gpu type or None) from a tres string."""
    if not tres:
        return 0, None
    total = 0
    gtype = None
    for typ, n in _TRES_GPU_RE.findall(tres):
        if typ:
            gtype = typ
        else:
            total = int(n)
    if total == 0:
        # fall back to the typed entry if the bare gres/gpu= was absent
        for typ, n in _TRES_GPU_RE.findall(tres):
            if typ:
                total = int(n)
    return total, gtype


def _num(obj, default=None):
    """Slurm JSON wraps numbers as {set,infinite,number}."""
    if isinstance(obj, dict):
        if obj.get("infinite"):
            return float("inf")
        if obj.get("set"):
            return obj.get("number")
        return default
    return obj if obj is not None else default


# ---------------------------------------------------------------------------
# partitions the current user may submit to (for "highlight what I can use")
# ---------------------------------------------------------------------------


def _my_accounts_qos() -> tuple[set[str], set[str]]:
    def fn():
        accts: set[str] = set()
        qos: set[str] = set()
        try:
            out = _run([
                "sacctmgr", "-nP", "show", "assoc",
                f"user={USER}", "format=Account,QOS",
            ])
        except SlurmError:
            return accts, qos
        for line in out.splitlines():
            parts = line.split("|")
            if parts and parts[0]:
                accts.add(parts[0].strip())
            if len(parts) > 1 and parts[1]:
                qos.update(q.strip() for q in parts[1].split(",") if q.strip())
        return accts, qos

    return _cached("acctqos", 300, fn)


def my_partitions() -> set[str]:
    """Partition names the user is allowed to submit to (best-effort)."""

    def fn():
        accts, qos = _my_accounts_qos()
        allowed: set[str] = set()
        try:
            data = json.loads(_run(["scontrol", "show", "partition", "--json"]))
        except SlurmError:
            return allowed
        for part in data.get("partitions", []):
            name = part.get("name")
            if not name:
                continue
            # state up?
            states = part.get("partition", {}).get("state") or part.get("state") or []
            if isinstance(states, str):
                states = [states]
            if states and not any("UP" == s.upper() for s in states):
                continue

            def _allow(field, mine):
                vals = part.get(field) or []
                if isinstance(vals, str):
                    vals = [v for v in vals.split(",") if v]
                if not vals or "ALL" in [str(v).upper() for v in vals]:
                    return True
                return bool(set(vals) & mine) if mine else True

            def _deny(field, mine):
                vals = part.get(field) or []
                if isinstance(vals, str):
                    vals = [v for v in vals.split(",") if v]
                return bool(set(vals) & mine)

            if not _allow("allowed_accounts", accts):
                continue
            if not _allow("allowed_qos", qos):
                continue
            if _deny("denied_accounts", accts):
                continue
            allowed.add(name)
        return allowed

    return _cached("mypart", 300, fn)


# ---------------------------------------------------------------------------
# cluster node / GPU state
# ---------------------------------------------------------------------------


_IDX_RE = re.compile(r"\(IDX:([\d,\-]+)\)")


def _parse_idxs(s: str | None) -> list[int]:
    """'gpu:h200:4(IDX:0-2,5)' -> [0,1,2,5]."""
    m = _IDX_RE.search(s or "")
    if not m:
        return []
    out: list[int] = []
    for part in m.group(1).split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def occupancy(ttl: float = 8.0) -> dict:
    """node name -> [{job_id,user,name,gpus,gpu_idxs}] for all RUNNING jobs
    cluster-wide (who is occupying which GPUs on which node)."""

    def fn():
        data = json.loads(_run(["squeue", "-t", "RUNNING,COMPLETING", "--json"],
                               timeout=30))
        occ: dict[str, list[dict]] = {}
        for j in data.get("jobs", []):
            gres = j.get("gres_detail") or []
            alloc = ((j.get("job_resources") or {}).get("nodes") or {}).get("allocation") or []
            for i, a in enumerate(alloc):
                nname = a.get("name")
                if not nname:
                    continue
                g = gres[i] if i < len(gres) else ""
                gp = _parse_gres_gpus(g)
                occ.setdefault(nname, []).append({
                    "job_id": _num(j.get("job_id")),
                    "user": j.get("user_name") or "",
                    "name": j.get("name") or "",
                    "gpus": sum(gp.values()),
                    "gpu_idxs": _parse_idxs(g),
                    "cpus": (a.get("cpus") or {}).get("count")
                            if isinstance(a.get("cpus"), dict) else a.get("cpus"),
                    "start_time": _num(j.get("start_time")),
                    "time_limit_min": _num(j.get("time_limit")),
                    "partition": j.get("partition") or "",
                })
        for v in occ.values():
            v.sort(key=lambda x: (-x["gpus"], x["user"]))
        return occ

    return _cached("occ", ttl, fn)


def _expand_hostlist(expr: str) -> list[str]:
    try:
        return [x.strip() for x in _run(["scontrol", "show", "hostnames", expr]).splitlines()
                if x.strip()]
    except SlurmError:
        return []


def sprio(ttl: float = 8.0) -> dict:
    """job_id(int) -> priority factor breakdown from `sprio` (weighted values).

    On this cluster the meaningful factors are age, fairshare, qos and NICE
    (negative nice = a manual priority boost); assoc/jobsize/site are 0."""

    def fn():
        try:
            out = _run(["sprio", "-h", "-o", "%i|%Y|%A|%F|%Q|%P|%N"], timeout=20)
        except SlurmError:
            return {}

        def iv(x):
            try:
                return int(float(x))
            except (ValueError, TypeError):
                return 0

        m = {}
        for line in out.splitlines():
            c = line.split("|")
            if len(c) < 7:
                continue
            try:
                jid = int(re.split(r"[_\[]", c[0].strip())[0])
            except ValueError:
                continue
            nice = iv(c[6])
            m[jid] = {
                "total": iv(c[1]), "age": iv(c[2]), "fairshare": iv(c[3]),
                "qos": iv(c[4]), "partition": iv(c[5]), "nice": nice,
                "nice_boost": -nice if nice < 0 else 0,
            }
        return m

    return _cached("sprio", ttl, fn)


def pending_jobs(ttl: float = 8.0) -> list[dict]:
    """All PENDING jobs cluster-wide, parsed for node-queue matching.

    The scheduler here does not expose planned node placement, so a job's link to
    a node is via an explicit --nodelist (required_nodes) or resource match
    (same partition + GPU type). `required`/`excluded` are expanded node sets."""

    def fn():
        data = json.loads(_run(["squeue", "-t", "PENDING", "--json"], timeout=30))
        prio = sprio()
        hl: dict[str, set] = {}

        def expand(expr):
            if not expr or expr == "(null)":
                return None
            if expr not in hl:
                hl[expr] = set(_expand_hostlist(expr))
            return hl[expr]

        out = []
        for j in data.get("jobs", []):
            gpus, gtype = _parse_tres_gpu(j.get("tres_req_str"))
            if not gpus:
                gpus, gtype = _parse_tres_gpu(j.get("tres_per_node"))
            out.append({
                "id": _job_id_str(j),
                "job_id": _num(j.get("job_id")),
                "user": j.get("user_name") or "",
                "name": j.get("name") or "",
                "partition": j.get("partition") or "",
                "priority": _num(j.get("priority"), 0),
                "qos": j.get("qos") or "",
                "reason": j.get("state_reason") or "",
                "gpus": gpus,
                "gpu_type": gtype,
                "required": expand(j.get("required_nodes")),
                "excluded": expand(j.get("excluded_nodes")) or set(),
                "submit_time": _num(j.get("submit_time")),
                "sprio": prio.get(_num(j.get("job_id"))),
            })
        return out

    return _cached("pending", ttl, fn)


def _node_queue(name: str, gtypes: list[str], nparts: set, pend: list[dict],
                cap: int = 20) -> tuple[list[dict], int, int]:
    """Pending jobs that could land on this node, in scheduling order (actively
    waiting first by priority; held/dependency jobs sink to the bottom)."""
    q = []
    for p in pend:
        if not p["gpus"]:
            continue  # only GPU jobs queue for a GPU node
        if p["partition"] and p["partition"] not in nparts:
            continue
        if p["required"] is not None and name not in p["required"]:
            continue
        if name in p["excluded"]:
            continue
        if p["gpu_type"] and p["gpu_type"] not in gtypes:
            continue
        reason = p["reason"]
        waiting = not (reason.startswith("JobHeld") or reason in ("Dependency", "BeginTime"))
        q.append({
            "id": p["id"], "user": p["user"], "name": p["name"],
            "gpus": p["gpus"], "gpu_type": p["gpu_type"], "priority": p["priority"],
            "reason": reason, "qos": p["qos"],
            "pinned": p["required"] is not None, "waiting": waiting,
            "sprio": p["sprio"],
        })
    q.sort(key=lambda x: (not x["waiting"], not x["pinned"], -(x["priority"] or 0)))
    active = sum(1 for x in q if x["waiting"])
    return q[:cap], len(q), active


def reservations(ttl: float = 60) -> dict:
    """node name -> {name, mine} for nodes under an ACTIVE reservation. `mine`
    is True if the user's account/username is allowed in that reservation."""

    def fn():
        try:
            text = _run(["scontrol", "show", "reservation"])
        except SlurmError:
            return {}
        if "No reservations" in text:
            return {}
        accts, _ = _my_accounts_qos()
        # also count accounts the user is actively running jobs under — if a job
        # runs under account X, the user can submit under X (and use X's reservations)
        try:
            accts = accts | {j["account"] for j in my_jobs() if j.get("account")}
        except SlurmError:
            pass
        node2 = {}
        for block in text.split("ReservationName=")[1:]:
            name = block.split()[0]

            def g(k):
                m = re.search(rf"\b{k}=(\S+)", block)
                return m.group(1) if m else ""

            if g("State").upper() != "ACTIVE":
                continue
            nodes_expr = g("Nodes")
            if not nodes_expr or nodes_expr == "(null)":
                continue
            acc = {a for a in g("Accounts").split(",") if a and a != "(null)"}
            usr = {u for u in g("Users").split(",") if u and u != "(null)"}
            mine = bool(acc & accts) or (USER in usr)
            for nm in _expand_hostlist(nodes_expr):
                # if any reservation includes me, treat node as mine-reservable
                cur = node2.get(nm)
                node2[nm] = {"name": name,
                             "mine": mine or (cur["mine"] if cur else False),
                             "accounts": ",".join(sorted(acc)),
                             "users": ",".join(sorted(usr))}
        return node2

    return _cached("resv", ttl, fn)


def nodes(ttl: float = 4.0) -> list[dict]:
    """Per-node GPU state. Only nodes that actually have GPUs are returned.

    Crucially distinguishes *idle* GPUs from *grabbable* ones: GPUs on DRAIN /
    DOWN nodes, or under a reservation you're not in, are idle but you cannot get
    them; PLANNED ones are idle but already earmarked by backfill."""

    def fn():
        data = json.loads(_run(["scontrol", "show", "nodes", "--json"]))
        mine = my_partitions()
        resv = reservations()
        occ = occupancy()
        pend = pending_jobs()
        out = []
        for n in data.get("nodes", []):
            total = _parse_gres_gpus(n.get("gres"))
            if not total:
                continue
            used = _parse_gres_gpus(n.get("gres_used"))
            state = n.get("state") or []
            if isinstance(state, str):
                state = [state]
            up = {s.upper() for s in state}
            parts = n.get("partitions") or []
            name = n.get("name")
            per_type = []
            tot = us = 0
            for typ, t in sorted(total.items()):
                u = min(used.get(typ, 0), t)
                per_type.append({"type": typ, "total": t, "used": u, "free": t - u})
                tot += t
                us += u
            drain = bool(up & {"DOWN", "DRAIN", "DRAINED", "DRAINING", "FAIL", "FAILING",
                               "MAINT", "NOT_RESPONDING", "POWERED_DOWN", "POWERING_DOWN"})
            planned = "PLANNED" in up
            r = resv.get(name)
            reserved = bool(r)
            reserved_for_me = bool(r and r["mine"])
            usable_by_me = bool(set(parts) & mine)
            free = tot - us
            # can the user actually obtain these idle GPUs right now?
            grabbable = (not drain and usable_by_me and (not reserved or reserved_for_me))
            gtypes = sorted(total.keys())
            queued, queued_count, queued_active = _node_queue(name, gtypes, set(parts), pend)
            out.append({
                "name": name,
                "gpu_types": sorted(total.keys()),
                "per_type": per_type,
                "total": tot,
                "used": us,
                "free": free,
                "free_grabbable": free if grabbable else 0,
                "state": state,
                "partitions": parts,
                "usable_by_me": usable_by_me,
                "available": not drain,
                "drain": drain,
                "planned": planned,
                "reserved": reserved,
                "reserved_for_me": reserved_for_me,
                "reservation": r["name"] if r else None,
                "resv_accounts": r.get("accounts") if r else "",
                "resv_users": r.get("users") if r else "",
                "occupants": occ.get(name, []),
                "queued": queued,
                "queued_count": queued_count,
                "queued_active": queued_active,
                "grabbable": grabbable,
                "cpu_total": _num(n.get("cpus"), 0),
                "cpu_alloc": _num(n.get("alloc_cpus"), 0),
                "cpu_load": (_num(n.get("cpu_load"), 0) or 0) / 100,
                "mem_total_mb": _num(n.get("real_memory"), 0),
                "mem_alloc_mb": _num(n.get("alloc_memory"), 0),
            })
        out.sort(key=lambda x: (x["gpu_types"][0] if x["gpu_types"] else "", x["name"]))
        return out

    return _cached("nodes", ttl, fn)


# ---------------------------------------------------------------------------
# the user's jobs
# ---------------------------------------------------------------------------


def _job_id_str(j: dict) -> str:
    arr = _num(j.get("array_job_id"), 0)
    task = j.get("array_task_string") or ""
    if arr and task:
        return f"{arr}_[{task}]"
    if arr:
        tid = _num(j.get("array_task_id"))
        return f"{arr}_{tid}" if tid is not None else str(arr)
    return str(_num(j.get("job_id")))


def _parse_job(j: dict) -> dict:
    state = j.get("job_state") or []
    if isinstance(state, str):
        state = [state]
    st = state[0] if state else "UNKNOWN"
    gpus, gtype = _parse_tres_gpu(j.get("tres_req_str"))
    if not gpus:
        gpus, gtype = _parse_tres_gpu(j.get("tres_per_node"))
    return {
        "id": _job_id_str(j),
        "job_id": _num(j.get("job_id")),
        "is_array": bool(_num(j.get("array_job_id"), 0)),
        "name": j.get("name"),
        "state": st,
        "state_full": state,
        "reason": j.get("state_reason") or "",
        "partition": j.get("partition"),
        "qos": j.get("qos"),
        "gpus": gpus,
        "gpu_type": gtype,
        "node_count": _num(j.get("node_count"), 1),
        "nodes": j.get("nodes") or "",
        "time_limit_min": _num(j.get("time_limit")),
        "start_time": _num(j.get("start_time")),
        "submit_time": _num(j.get("submit_time")),
        "command": j.get("command") or "",
        "workdir": j.get("current_working_directory") or "",
        "account": j.get("account") or "",
    }


def my_jobs(ttl: float = 4.0) -> list[dict]:
    def fn():
        data = json.loads(_run(["squeue", "--me", "--json"]))
        out = [_parse_job(j) for j in data.get("jobs", [])]
        order = {"RUNNING": 0, "PENDING": 1}
        out.sort(key=lambda x: (order.get(x["state"], 2), -(x["submit_time"] or 0)))
        return out

    return _cached("jobs", ttl, fn)


def job_detail(job_id) -> dict | None:
    """Full parsed record for a single job (used by the migration engine)."""
    try:
        data = json.loads(_run(["squeue", "-j", str(job_id), "--json"], timeout=15))
    except SlurmError:
        return None
    jobs = data.get("jobs", [])
    return _parse_job(jobs[0]) if jobs else None


# ---------------------------------------------------------------------------
# actions (explicit, never scheduled)
# ---------------------------------------------------------------------------


def submit(sbatch_path: str, extra_args=None, workdir: str | None = None) -> str:
    cmd = ["sbatch", "--parsable"]
    if extra_args:
        cmd += list(extra_args)
    cmd.append(sbatch_path)
    out = _run(cmd, timeout=30, cwd=workdir)
    invalidate()
    return out.strip().split(";")[0]  # "jobid;cluster" -> jobid


def cancel(job_id: str) -> None:
    _run(["scancel", str(job_id)])
    invalidate()


def hold(job_id: str) -> None:
    _run(["scontrol", "hold", str(job_id)])
    invalidate()


def release(job_id: str) -> None:
    _run(["scontrol", "release", str(job_id)])
    invalidate()


def update(job_id: str, **fields) -> None:
    args = ["scontrol", "update", f"jobid={job_id}"]
    for k, v in fields.items():
        args.append(f"{k}={v}")
    _run(args)
    invalidate()


def job_state(job_id: str) -> str | None:
    """Quick single-job state lookup (used by migration watchers)."""
    out = _run(["squeue", "-j", str(job_id), "-h", "-o", "%T"], timeout=15)
    s = out.strip().splitlines()
    return s[0].strip() if s else None


# ---------------------------------------------------------------------------
# site / qos / partition helpers (priority & schedulability hints)
# ---------------------------------------------------------------------------

import socket  # noqa: E402

_SITE_PREFIXES = ("gcp-us2", "msp3", "sof1", "hala", "spear", "stp")


def current_site() -> str:
    host = socket.gethostname()
    for p in _SITE_PREFIXES:
        if host.startswith(p):
            return p
    return host.split("-")[0]


def login_hosts() -> dict:
    """Map site -> a login node on that site (for cross-site rsync)."""

    def fn():
        try:
            out = _run(["sinfo", "-h", "-p", "login", "-N", "-o", "%N"], timeout=15)
        except SlurmError:
            return {}
        hosts = {}
        for name in sorted(set(n.strip() for n in out.splitlines() if n.strip())):
            for p in _SITE_PREFIXES:
                if name.startswith(p):
                    hosts.setdefault(p, name)
                    break
        return hosts

    return _cached("loginhosts", 300, fn)


def my_qos() -> list[dict]:
    """QOS the user may use, with priority + preemption — the dominant lever for
    how soon a job is scheduled on this cluster."""

    def fn():
        _, qset = _my_accounts_qos()
        try:
            out = _run(["sacctmgr", "-nP", "show", "qos",
                        "format=Name,Priority,Preempt,Flags,MaxWall"])
        except SlurmError:
            return []
        rows = []
        for line in out.splitlines():
            c = line.split("|")
            if len(c) < 2 or not c[0]:
                continue
            name = c[0].strip()
            try:
                prio = int(c[1]) if c[1] else 0
            except ValueError:
                prio = 0
            rows.append({
                "name": name,
                "priority": prio,
                "can_preempt": bool(c[2].strip()) if len(c) > 2 else False,
                "flags": c[3].strip() if len(c) > 3 else "",
                "max_wall": c[4].strip() if len(c) > 4 else "",
                "usable": (not qset) or name in qset,
            })
        usable = [r for r in rows if r["usable"]]
        # normalize the bar against the user's OWN best QOS, so the comparison
        # that matters (which of MY qos schedules soonest) is legible
        maxp = max([r["priority"] for r in usable] or [0]) or 1
        for r in usable:
            r["priority_pct"] = round(100 * r["priority"] / maxp)
        usable.sort(key=lambda r: -r["priority"])
        return usable

    return _cached("myqos", 300, fn)


def usable_gpu_partitions() -> list[str]:
    """Partitions the user can submit GPU jobs to (excludes login)."""

    def fn():
        mine = my_partitions()
        gpu_parts = set()
        for n in nodes():
            if n["usable_by_me"]:
                gpu_parts.update(n["partitions"])
        out = sorted((mine & gpu_parts) - {"login"})
        return out

    return _cached("gpuparts", 60, fn)
