# Cloudflare production deployment

GPU Visualizer must remain on the Slurm login-side host. It executes Slurm,
reads mounted repositories and logs, writes state under `~/.gpuviz`, and runs
background watchers. Cloudflare Pages and Workers cannot provide that runtime.

The production path is:

```text
browser -> Cloudflare Access -> named Cloudflare Tunnel -> 0600 Unix socket -> server.py -> Slurm
```

Do not use a Quick Tunnel. Do not publish a hostname until its Access policy is
active: this application can submit, update, migrate, and cancel jobs.

## 1. Install the origin and connector services

From the repository root:

```bash
./deploy/cloudflare/install-cloudflared.sh
./deploy/cloudflare/install-user-services.sh
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
curl --unix-socket "${runtime_dir}/gpuviz/gpuviz.sock" http://localhost/api/health
```

The connector installer pins and verifies `cloudflared` 2026.7.3. The service
installer resolves the current repository path, prefers the system Python, and
uses the connector installed at `~/.local/bin/cloudflared` before installing the
`gpuviz.service` and `cloudflared-gpuviz.service` user units. The origin listens
only on `$XDG_RUNTIME_DIR/gpuviz/gpuviz.sock` with mode `0600`; it does not open
a TCP port to other users on the shared login node.

For the services to survive logout and restart after boot, an administrator
must enable lingering:

```bash
id -un
sudo loginctl enable-linger <USER>
```

Confirm that a persistent outbound tunnel is allowed by cluster policy before
enabling it on a login node.

## 2. Create Access before the public route

In Cloudflare Zero Trust, create a self-hosted Access application for the final
hostname (for example, `gpuviz.example.com`). Use deny-by-default access, an
`Allow` rule for the owner's exact email address, and an MFA `Require` rule.
Do not add `Everyone` or `Bypass` policies, and use a short session duration.
Enable "Protect with Access" on every Tunnel route to this origin so Access
tokens are checked before traffic reaches the application.

## 3. Create a named, remotely managed Tunnel

Create a named Tunnel and configure its public hostname with this service URL:

```text
unix:/run/user/<UID>/gpuviz/gpuviz.sock
```

Run `id -u` on the login host and replace `<UID>` with that numeric result.

Store the Tunnel token as raw text in the service's private config directory,
never in the repository or a process argument:

```bash
gpuviz_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/gpuviz"
install -d -m 0700 "${gpuviz_config_dir}"
read -rsp "Paste the Tunnel token: " tunnel_token; printf '\n'
(umask 077; printf '%s' "${tunnel_token}" > "${gpuviz_config_dir}/cloudflare-tunnel.token")
unset tunnel_token
chmod 0600 "${gpuviz_config_dir}/cloudflare-tunnel.token"
./deploy/cloudflare/install-user-services.sh
```

The hidden `read` keeps the token off the terminal and out of shell history.
The service receives only the token-file path.

`cloudflared` 2025.4.0 and newer supports `--token-file`; the pinned version
meets that requirement.

## 4. Verify production

```bash
systemctl --user --no-pager --full status gpuviz.service cloudflared-gpuviz.service
journalctl --user -u gpuviz.service -u cloudflared-gpuviz.service --since=-10m
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
curl --unix-socket "${runtime_dir}/gpuviz/gpuviz.sock" http://localhost/api/health
curl -I https://gpuviz.example.com/
```

The public request should redirect unauthenticated callers to Cloudflare Access.
After authentication, verify the Cluster, Projects, Jobs, Queue, Activity, and
Ad-hoc views before exercising a write action. Start with a guarded, tiny holder
job; live submit/migrate against real allocations is operationally sensitive.

Cross-site staging invokes noninteractive SSH. A lingered service has no
interactive `SSH_AUTH_SOCK`, so leave staging unused unless `BatchMode` keys and
`known_hosts` have been provisioned and tested for this service account.

Cloudflare references:

- [Protect a self-hosted application with Access](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Run Cloudflare Tunnel as a service](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/)
- [Tunnel run parameters and token files](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)
