#!/usr/bin/env python3
"""gpu-visualizer — Slurm GPU dashboard + per-repo job manager (zero deps).

Run on a login node:   python3 server.py --socket=/run/user/$UID/gpuviz.sock
Tunnel from laptop:    ssh -L 8770:/run/user/<uid>/gpuviz.sock <login-node>
Open:                  http://localhost:8770

The WebUI separates *job definition* (declared in each project repo's
gpuviz.toml) from *job management* (submit / monitor / migrate / read logs),
done here. Pure stdlib; the frontend polls the JSON API.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import math
import os
import re
import signal
import socket
import stat
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import parse_qs, unquote, urlparse

import catalog
import deps
import drafts
import logs
import migrate
import monitors
import projects
import sbatch
import slurm
import submitter
import transfer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.realpath(os.path.join(HERE, "static"))
MAX_BODY_BYTES = 1024 * 1024
AUTH_USER = b"gpuviz"
AUTH_MIN_TOKEN_CHARS = 32


def load_auth_token(path: str) -> bytes:
    """Load a private Basic-auth token without following symlinks."""
    path = os.path.abspath(os.path.expanduser(path))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise ValueError(f"cannot open auth token file {path}: {e.strerror or e}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("auth token file must be a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("auth token file must be owned by the current user")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            raise ValueError("auth token file must have mode 0600")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read()
    finally:
        if fd >= 0:
            os.close(fd)
    token = raw.strip()
    try:
        token_text = token.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("auth token file must contain UTF-8 text") from e
    if len(token_text) < AUTH_MIN_TOKEN_CHARS:
        raise ValueError(f"auth token must contain at least {AUTH_MIN_TOKEN_CHARS} characters")
    return token


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GpuvizHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64
    auth_token: bytes | None = None


class GpuvizUnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    request_queue_size = 64
    auth_token: bytes | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "gpuviz/0.2"
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return self.server_version

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    # -- io helpers -------------------------------------------------------
    @staticmethod
    def _sane(o):
        """Strip inf/nan (e.g. TimeLimit=UNLIMITED) — not valid JSON."""
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, dict):
            return {k: Handler._sane(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [Handler._sane(x) for x in o]
        return o

    def _json(self, obj, status=200):
        body = json.dumps(self._sane(obj)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path, head_only=False):
        path = os.path.realpath(path)
        try:
            inside_static = os.path.commonpath((STATIC, path)) == STATIC
        except ValueError:
            inside_static = False
        if not inside_static or not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(
            os.path.splitext(path)[1], "application/octet-stream")
        info = os.stat(path)
        etag = f'"{info.st_mtime_ns:x}-{info.st_size:x}"'
        # Assets are not content-hashed, so every file must be revalidated.
        # This prevents fresh HTML loading a stale, incompatible JS/CSS bundle.
        cache = "no-cache, max-age=0, must-revalidate"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", cache)
            self.send_header("ETag", etag)
            self._security_headers()
            self.end_headers()
            return None
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(info.st_size))
        self.send_header("Cache-Control", cache)
        self.send_header("ETag", etag)
        self._security_headers()
        self.end_headers()
        if not head_only:
            with open(path, "rb") as f:
                self.wfile.write(f.read())

    def _security_headers(self):
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; base-uri 'none'; object-src 'none'; "
                         "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
                         "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self._forwarded_proto() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _forwarded_proto(self) -> str | None:
        value = self.headers.get("X-Forwarded-Proto")
        return None if value is None else value.split(",", 1)[0].strip().lower()

    def _auth_error(self, status: int, message: str, challenge: bool = False) -> bool:
        body = json.dumps({"error": message}, separators=(",", ":")).encode()
        self.close_connection = True
        self.send_response(status)
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="GPU Visualizer", charset="UTF-8"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def _require_auth(self) -> bool:
        expected = self.server.auth_token
        if expected is None:
            return True
        forwarded_proto = self._forwarded_proto()
        if forwarded_proto is not None and forwarded_proto != "https":
            return self._auth_error(403, "HTTPS is required for Basic authentication")
        supplied_user = b""
        supplied_token = b""
        header = self.headers.get("Authorization", "")
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "basic":
            try:
                decoded = base64.b64decode(parts[1], validate=True)
            except (binascii.Error, ValueError):
                decoded = b""
            supplied_user, separator, supplied_token = decoded.partition(b":")
        else:
            separator = b""
        user_matches = hmac.compare_digest(supplied_user, AUTH_USER)
        token_matches = hmac.compare_digest(supplied_token, expected)
        if separator and user_matches and token_matches:
            return True
        return self._auth_error(401, "authentication required", challenge=True)

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise RequestError("Transfer-Encoding is not supported")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            self.close_connection = True
            raise RequestError("invalid Content-Length") from e
        if n < 0 or n > MAX_BODY_BYTES:
            self.close_connection = True
            raise RequestError(f"request body must be <= {MAX_BODY_BYTES} bytes", 413)
        if not n:
            return {}
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            # Do not let an unread body become the next HTTP/1.1 request.
            self.close_connection = True
            raise RequestError("Content-Type must be application/json", 415)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as e:
            raise RequestError("invalid JSON body") from e
        if not isinstance(body, dict):
            raise RequestError("JSON body must be an object")
        return body

    def _check_origin(self):
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise RequestError("cross-site request rejected", 403)
        origin = self.headers.get("Origin")
        if not origin:
            return
        origin_host = urlparse(origin).netloc.lower()
        forwarded = self.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
        request_host = (forwarded or self.headers.get("Host", "")).lower()
        if not origin_host or origin_host != request_host:
            raise RequestError("request origin does not match host", 403)

    def _mutate(self, path, q=None):
        self._check_origin()
        result = (self._delete(path, q or {}) if self.command == "DELETE"
                  else self._post(path, self._body()))
        if path not in ("/api/preview", "/api/monitors") and not path.endswith("/preview"):
            actor = ("basic:gpuviz" if self.server.auth_token is not None else
                     (self.headers.get("Cf-Access-Authenticated-User-Email") or "local")[:200])
            print(json.dumps({"event": "mutation", "actor": actor,
                              "method": self.command, "path": path}), flush=True)
        return result

    def log_message(self, *a):
        pass

    def _wrap(self, fn):
        try:
            return fn()
        except RequestError as e:
            return self._json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return None
        except slurm.SlurmError as e:
            return self._json({"error": str(e)}, 502)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": repr(e)}, 500)

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        if not self._require_auth():
            return None
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if path.startswith("/static/"):
            safe = os.path.normpath(path[len("/static/"):]).lstrip("/")
            return self._file(os.path.join(STATIC, safe))
        return self._wrap(lambda: self._get(path, q))

    def do_HEAD(self):
        if not self._require_auth():
            return None
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"), head_only=True)
        if path.startswith("/static/"):
            safe = os.path.normpath(path[len("/static/"):]).lstrip("/")
            return self._file(os.path.join(STATIC, safe), head_only=True)
        if path == "/api/health":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            return None
        return self._json({"error": "not found"}, 404)

    def _get(self, path, q):
        if path == "/api/health":
            return self._json({"ok": True, "service": "gpuviz", "version": self.server_version})
        if path == "/api/state":
            # Do not annotate the shared TTL-cache objects in place.
            jobs = [dict(job) for job in slurm.my_jobs()]
            origin = submitter.by_job_id()
            for j in jobs:
                o = origin.get(str(j["job_id"]).split("_")[0])
                if o:
                    j["origin"] = {"project": o.get("project"), "job_key": o.get("job_key"),
                                   "submission_id": o.get("id")}
            return self._json({
                "me": slurm.USER, "site": slurm.current_site(),
                "nodes": slurm.nodes(), "jobs": jobs,
                "partitions": slurm.usable_gpu_partitions(),
            })
        if path == "/api/qos":
            return self._json({"qos": slurm.my_qos()})
        if path == "/api/queue":
            return self._queue()
        if path == "/api/projects":
            return self._json({"projects": [projects.status(p) for p in projects.list_projects()]})
        m = re.fullmatch(r"/api/projects/(\d+)/catalog", path)
        if m:
            return self._project_catalog(int(m.group(1)))
        if path == "/api/submissions":
            return self._json({"submissions": submitter.list_submissions()})
        if path == "/api/drafts":
            return self._json({"drafts": drafts.list_drafts()})
        if path == "/api/drafts/defaults":
            kind = (q.get("kind") or ["normal"])[0]
            draft = sbatch.holder_defaults() if kind == "holder" else sbatch.new_draft_defaults()
            draft.setdefault("kind", kind)
            return self._json({"draft": draft})
        if path == "/api/migrations":
            return self._json({"migrations": migrate.list_migrations()})
        if path == "/api/transfers":
            return self._json({"transfers": transfer.list_transfers()})
        if path == "/api/monitors":
            return self._json({"monitors": monitors.list_monitors()})
        if path == "/api/sbatch/help":
            return self._json({"help": sbatch.FIELD_HELP})
        m = re.fullmatch(r"/api/jobs/([\w\[\]%_-]+)/log", path)
        if m:
            try:
                tail = int((q.get("tail") or [str(logs.MAX_TAIL)])[0])
            except ValueError as e:
                raise RequestError("tail must be an integer") from e
            tail = max(1, min(logs.MAX_TAIL, tail))
            paths = logs.resolve_paths(m.group(1))
            same = paths["out"] and paths["out"] == paths["err"]
            return self._json({
                "out": logs.read_tail(paths["out"], tail),
                "err": None if same else logs.read_tail(paths["err"], tail),
                "workdir": paths["workdir"],
            })
        return self._json({"error": "not found"}, 404)

    def _queue(self):
        """Cluster-wide PENDING queue, sorted by scheduling order, for the Queue
        tab. Projects out the non-serializable required/excluded node sets."""
        pend = slurm.pending_jobs()
        rows = []
        summary = {}
        for p in pend:
            reason = p["reason"]
            waiting = not (reason.startswith("JobHeld") or reason in ("Dependency", "BeginTime"))
            req = p.get("required")
            rows.append({
                "id": p["id"], "job_id": p["job_id"], "user": p["user"],
                "name": p["name"], "partition": p["partition"], "qos": p["qos"],
                "priority": p["priority"], "reason": reason, "gpus": p["gpus"],
                "gpu_type": p["gpu_type"], "submit_time": p["submit_time"],
                "sprio": p["sprio"], "waiting": waiting,
                "pinned": req is not None,
                "nodelist": sorted(req) if req else None,
                "mine": p["user"] == slurm.USER,
            })
            if p["gpus"]:
                t = p["gpu_type"] or "gpu"
                s = summary.setdefault(t, {"jobs": 0, "gpus": 0, "waiting": 0})
                s["jobs"] += 1
                s["gpus"] += p["gpus"]
                if waiting:
                    s["waiting"] += 1
        rows.sort(key=lambda x: (not x["waiting"], -(x["priority"] or 0)))
        for i, r in enumerate(rows):
            r["rank"] = i + 1 if r["waiting"] else None
        return self._json({"queue": rows, "summary": summary, "me": slurm.USER})

    def _project_catalog(self, pid):
        p = projects.get(pid)
        if not p:
            return self._json({"error": "no such project"}, 404)
        cat = catalog.load(p["path"])
        for spec in cat["jobs"]:
            spec["deps"] = deps.check(spec.get("needs"))
        return self._json({"project": p, "catalog": cat})

    # -- DELETE -----------------------------------------------------------
    def do_DELETE(self):
        if not self._require_auth():
            return None
        u = urlparse(self.path)
        return self._wrap(lambda: self._mutate(unquote(u.path), parse_qs(u.query)))

    def _delete(self, path, q):
        m = re.fullmatch(r"/api/drafts/(\d+)", path)
        if m:
            ok = drafts.delete(int(m.group(1)))
            return self._json({"ok": ok}, 200 if ok else 404)
        m = re.fullmatch(r"/api/projects/(\d+)", path)
        if m:
            ok = projects.remove(int(m.group(1)))
            return self._json({"ok": ok}, 200 if ok else 404)
        m = re.fullmatch(r"/api/submissions/(\d+)", path)
        if m:
            keep = (q.get("keep_snapshot") or ["0"])[0] in ("1", "true")
            ok = submitter.delete_submission(int(m.group(1)), remove_snapshot=not keep)
            return self._json({"ok": ok}, 200 if ok else 404)
        m = re.fullmatch(r"/api/monitors/([\w.:-]+)", path)
        if m:
            return self._json({"ok": monitors.delete(m.group(1))})
        return self._json({"error": "not found"}, 404)

    # -- POST / PUT -------------------------------------------------------
    def do_POST(self):
        if not self._require_auth():
            return None
        path = unquote(urlparse(self.path).path)
        return self._wrap(lambda: self._mutate(path))

    def do_PUT(self):
        return self.do_POST()

    def _post(self, path, body):
        # ---- projects ---------------------------------------------------
        if path == "/api/projects":
            if not body.get("path"):
                return self._json({"error": "path required"}, 400)
            return self._json({"project": projects.add(body["path"], body.get("name"))})
        m = re.fullmatch(r"/api/projects/(\d+)/submit", path)
        if m:
            return self._submit_catalog(int(m.group(1)), body)
        m = re.fullmatch(r"/api/projects/(\d+)/preview", path)
        if m:
            return self._preview_catalog(int(m.group(1)), body)

        # ---- migration --------------------------------------------------
        if path == "/api/migrate":
            return self._migrate(body)
        m = re.fullmatch(r"/api/migrations/(\d+)/abort", path)
        if m:
            ok = migrate.abort(int(m.group(1)))
            if not ok:
                return self._json({"error": "migration can no longer be aborted"}, 409)
            return self._json({"ok": True, "state": "aborting"})
        if path == "/api/migrations/clear":
            migrate.clear_finished()
            return self._json({"ok": True})

        # ---- cross-site data staging ------------------------------------
        if path == "/api/stage":
            host = (body.get("src_host") or "").strip()
            src = (body.get("src_path") or "").strip()
            if not host or not src:
                return self._json({"error": "src_host and src_path required"}, 400)
            t = transfer.create(host, src, body.get("dst_path") or src)
            return self._json({"transfer": t})
        m = re.fullmatch(r"/api/transfers/(\d+)/abort", path)
        if m:
            return self._json({"ok": transfer.abort(int(m.group(1)))})
        if path == "/api/transfers/clear":
            transfer.clear_finished()
            return self._json({"ok": True})

        # ---- external progress monitors ---------------------------------
        if path == "/api/monitors":
            try:
                return self._json({"monitor": monitors.upsert(body)})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/monitors/clear":
            return self._json({"cleared": monitors.clear_finished()})

        # ---- ad-hoc drafts (secondary) ----------------------------------
        if path == "/api/drafts":
            kind = body.pop("kind", "normal")
            return self._json({"draft": drafts.create(body, kind=kind)})
        m = re.fullmatch(r"/api/drafts/(\d+)", path)
        if m:
            d = drafts.update(int(m.group(1)), body)
            return self._json({"draft": d}, 200 if d else 404)
        m = re.fullmatch(r"/api/drafts/(\d+)/submit", path)
        if m:
            return self._submit_draft(int(m.group(1)))
        if path == "/api/preview":
            return self._json({"sbatch": sbatch.render(body)})

        # ---- real job actions -------------------------------------------
        m = re.fullmatch(r"/api/jobs/([\w\[\]%_-]+)/(cancel|hold|release)", path)
        if m:
            getattr(slurm, m.group(2))(m.group(1))
            return self._json({"ok": True, "job": m.group(1), "action": m.group(2)})
        m = re.fullmatch(r"/api/jobs/([\w\[\]%_-]+)/update", path)
        if m:
            fields = {k: v for k, v in body.items() if v not in (None, "")}
            slurm.update(m.group(1), **fields)
            return self._json({"ok": True, "job": m.group(1)})

        return self._json({"error": "not found"}, 404)

    # -- catalog submit / preview ----------------------------------------
    def _find_spec(self, pid, job_key):
        p = projects.get(pid)
        if not p:
            raise FileNotFoundError("no such project")
        cat = catalog.load(p["path"])
        for s in cat["jobs"]:
            if s["key"] == job_key:
                return p, s
        raise FileNotFoundError(f"no job '{job_key}' in catalog")

    def _submit_catalog(self, pid, body):
        p, spec = self._find_spec(pid, body.get("job_key"))
        rec = submitter.submit_spec(p, spec, body.get("overrides") or {})
        return self._json({"ok": True, "submission": rec})

    def _preview_catalog(self, pid, body):
        p, spec = self._find_spec(pid, body.get("job_key"))
        merged = {**spec, **{k: v for k, v in (body.get("overrides") or {}).items()
                             if v not in (None, "")}}
        if spec.get("script_file"):
            src = spec["script_file"]
            if not os.path.isabs(src):
                src = os.path.join(p["path"], src)
            try:
                with open(src) as f:
                    text = f.read()
                note = f"# (script_file: {src}; overrides applied as sbatch CLI flags)\n"
                return self._json({"sbatch": note + text,
                                   "extra": submitter._override_args(merged)})
            except OSError as e:
                return self._json({"error": str(e)}, 400)
        return self._json({"sbatch": sbatch.render(merged)})

    # -- migration dispatch ----------------------------------------------
    def _migrate(self, body):
        src = str(body.get("src_job_id") or "").strip()
        node = (body.get("target_node") or "").strip()
        if not src or not node:
            return self._json({"error": "src_job_id and target_node required"}, 400)
        mode = body.get("mode", "clone")

        if mode == "handoff":
            p, spec = self._find_spec(int(body["project_id"]), body.get("job_key"))
            ov = {**(body.get("overrides") or {}), "nodelist": node}

            def submit_fn():
                return submitter.submit_spec(p, spec, ov)["job_id"]

            label = f"handoff {spec['key']} → {node} (replace {src})"
        else:  # clone
            d = slurm.job_detail(src)
            if not d:
                return self._json({"error": f"job {src} not found"}, 404)
            script, workdir = d.get("command"), d.get("workdir") or os.path.expanduser("~")
            if not script or not os.path.isfile(script):
                return self._json({"error": "原任务脚本不可读,无法克隆迁移;可用 handoff 模式指定一个 repo 任务"}, 400)
            extra = ["--nodelist=" + node]
            if body.get("partition"):
                extra.append("--partition=" + body["partition"])

            def submit_fn():
                return slurm.submit(script, extra_args=extra, workdir=workdir)

            label = f"clone {d.get('name') or src} → {node}"

        m = migrate.create(src, node, submit_fn, label=label)
        return self._json({"migration": m})

    def _submit_draft(self, did):
        d = drafts.get(did)
        if not d:
            return self._json({"error": "no such draft"}, 404)
        outdir = sbatch.required_output_dir(d)
        workdir = d.get("workdir") or os.path.expanduser("~")
        if outdir:
            tgt = outdir if os.path.isabs(outdir) else os.path.join(workdir, outdir)
            os.makedirs(tgt, exist_ok=True)
        path = drafts.write_sbatch_file(d)
        job_id = slurm.submit(path, workdir=workdir)
        drafts.mark_submitted(did, job_id)
        return self._json({"ok": True, "job_id": job_id})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument(
        "--auth-token-file",
        help="enable HTTP Basic auth with a current-user-owned mode-0600 token file",
    )
    listen = ap.add_mutually_exclusive_group(required=True)
    listen.add_argument(
        "--host",
        help="explicitly serve TCP (unsafe on a shared host without another auth layer)",
    )
    listen.add_argument("--socket", help="serve on a mode-0600 Unix socket (recommended)")
    args = ap.parse_args()
    try:
        auth_token = load_auth_token(args.auth_token_file) if args.auth_token_file else None
    except ValueError as e:
        ap.error(str(e))
    socket_path = None
    if args.socket:
        socket_path = os.path.abspath(os.path.expanduser(args.socket))
        os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
        if os.path.lexists(socket_path):
            info = os.lstat(socket_path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                ap.error(f"refusing to replace non-owned socket path: {socket_path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(socket_path)
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            except OSError as e:
                ap.error(f"cannot safely inspect existing socket {socket_path}: {e}")
            else:
                ap.error(f"socket is already served by another process: {socket_path}")
            finally:
                probe.close()
            os.unlink(socket_path)
        httpd = GpuvizUnixHTTPServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
    else:
        httpd = GpuvizHTTPServer((args.host, args.port), Handler)
    httpd.auth_token = auth_token
    print(f"gpu-visualizer — user '{slurm.USER}' @ site '{slurm.current_site()}'")
    if socket_path:
        print(f"  socket: {socket_path} (mode 0600)")
    else:
        print(f"  local:  http://{args.host}:{args.port}")
        print(f"  tunnel: ssh -L {args.port}:localhost:{args.port} <this-login-node>")
    def stop_on_signal(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
        if socket_path and os.path.lexists(socket_path):
            info = os.lstat(socket_path)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                os.unlink(socket_path)


if __name__ == "__main__":
    main()
