# GPU Visualizer

A zero-dependency Slurm WebUI that **separates job definition from job
management**: you declare jobs in each project repo's `gpuviz.toml`, register the
repos here, and then submit / monitor / migrate / read logs for all of them from
one page — alongside a live map of free GPUs across the cluster.

Pure Python **stdlib** backend + vanilla JS frontend. No pip installs, no
node/npm, no build step.

## Run

On the Slurm **login node**:

```bash
gpuviz_runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
python3 server.py --socket="${gpuviz_runtime}/gpuviz-manual.sock"
```

From your laptop:

```bash
ssh -L 8770:/run/user/<UID>/gpuviz-manual.sock <login-node>
# open http://localhost:8770
```

Find `<UID>` with `id -u` on the login node. The Unix socket is mode `0600`, so
other users on the shared host cannot call the unauthenticated control API.
TCP mode requires an explicit `--host`; do not use it on a shared login node
without a separate authentication boundary.

## Cloudflare production deployment

This app cannot run on Pages or Workers: the backend must stay beside Slurm so
it can execute cluster commands and read project/log files. The supported online
shape is a named Cloudflare Tunnel protected by Cloudflare Access, pointing to a
mode-`0600` Unix socket owned by the Slurm user.

See [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md) for the pinned
connector install, systemd user units, Access-first rollout, and verification
steps. Never expose this app without Access; it can submit and cancel jobs.

## Concept

```
  repo A ─┐   each repo's gpuviz.toml          WebUI (here)         Slurm
  repo B ─┼─► declares its jobs (resources,  ─►  · free-GPU map  ─►  sbatch
  repo C ─┘   command, data deps)                · submit + overrides  scancel
                                                 · make-before-break   scontrol
                                                 · logs / migrations
```

Define jobs **once, in the repo** (version-controlled, next to the code). Manage
them **here**. GUI tweaks at submit time don't rewrite your `gpuviz.toml`; the
exact sbatch that ran is snapshotted into `<repo>/.gpuviz/submissions/`.

## Features

**Cluster GPU map** — every GPU node grouped by type (h200 / a6000 / …); one cell
per GPU, green = free. Nodes you can submit to are highlighted, others dimmed 🔒.
Header shows free-by-type. Filters + adjustable refresh interval.

**Projects** — register a repo → its `gpuviz.toml` jobs appear as blocks (sized by
GPU count). Each shows its **data-dependency status** (`needs`): ✓ present /
⚠ too small / ✗ missing / ⇄ on another site. *Preview / submit* opens an override
form (live sbatch preview, `?` explains every flag, **QOS picker shows priority**,
**⚡ max-schedulability** button clears nodelist + any-GPU + multi-partition).

**Jobs** — your running/pending jobs. Per job: **logs** (drill-in stdout/stderr
viewer with live tail), hold/release, edit (`scontrol update`), cancel. Jobs
submitted from a repo are tagged with their origin.

**Make-before-break migration** — drag a running job onto a free node: a clone is
submitted there and the original is cancelled **only once the clone is RUNNING**
(no allocation gap). A background thread watches it; abort anytime (keeps the
original). `swap→` on a running job does the same but hands the node to a chosen
catalog job (e.g. holder → real eval).

**Ad-hoc** — one-off jobs not tied to a repo (full sbatch editor).

## Why QOS matters on this cluster (and "spread across nodes" doesn't)

Scheduling is `sched/backfill` with `PriorityWeightQOS` ≈ 1e9, dwarfing age /
fairshare. So **which QOS you submit under dominates start time** far more than
how many nodes you target. Submitting many node-pinned copies is *worse* than one
unconstrained job (pinning defeats backfill, risks double-starts). The right
"queue everywhere" is one job: no nodelist, any GPU type, multi-partition,
**tight `--time`** — that's the ⚡ max-schedulability button.

## gpuviz.toml

See [`examples/gpuviz.toml`](examples/gpuviz.toml). Supports `[defaults]` +
`[[job]]` with inline `command`, reused `script_file`, `kind="holder"`, and data
`needs` (with `min_gb` / `site`).

## Files

| file | role |
|------|------|
| `server.py`    | stdlib HTTP server + JSON API |
| `slurm.py`     | nodes / jobs / qos / partitions / actions |
| `catalog.py`   | read a repo's `gpuviz.toml` |
| `projects.py`  | repo registry (`~/.gpuviz/projects.json`) |
| `submitter.py` | render + snapshot + submit + provenance index |
| `migrate.py`   | durable make-before-break background engine |
| `deps.py`      | data-dependency presence checks |
| `logs.py`      | resolve + tail stdout/stderr |
| `sbatch.py`    | render spec → sbatch; field help; holder template |
| `drafts.py`    | ad-hoc (non-repo) jobs |
| `static/`      | `index.html`, `style.css`, `app.js` (no build) |

## Operational notes

- **Cross-site data transfer is experimental.** The current implementation uses
  rsync over SSH and reports progress, but it still depends on the confirmed
  storage topology, host keys, and site-to-site login policy.
- Live submit/migrate were left **untested against real jobs** by design; do a
  guarded end-to-end run with a tiny holder job first.

State lives in `~/.gpuviz/` (projects, drafts, submission index, and migration
journal). Waiting make-before-break migrations are reconciled after a server
restart.

## Verification

```bash
python3 -m unittest discover -s tests -v
deno check static/app.js  # optional JS syntax check when Deno is available
```
