# Cloudflare deployment

GPU Visualizer must remain on the Slurm login-side host. It executes Slurm,
reads mounted repositories and logs, writes state under `~/.gpuviz`, and runs
background watchers. Cloudflare Pages and Workers cannot provide that runtime.

The preferred production path is:

```text
browser -> Cloudflare Access -> named Tunnel -> 0600 Unix socket -> app Basic auth -> Slurm
```

A no-account fallback is also available for short development sessions:

```text
browser -> random Quick URL -> Basic auth -> loopback socket proxy -> 0600 Unix socket -> Slurm
```

Named Tunnels are strongly preferred. Quick Tunnels have no SLA, stable URL, or
Cloudflare Access policy and are not a production deployment. In both modes the
installer adds private application credentials because this application can
submit, update, migrate, and cancel jobs.

## 1. Install the connector and user services

From the repository root:

```bash
./deploy/cloudflare/install-cloudflared.sh
./deploy/cloudflare/install-user-services.sh
```

The connector installer pins and verifies `cloudflared` 2026.7.3. The service
installer resolves the current repository path, prefers the system Python, and
uses the connector installed at `~/.local/bin/cloudflared`. It creates the
private username file `~/.config/gpuviz/local-auth.user`, defaulting to the
output of `id -un`, and a 256-bit application password at
`~/.config/gpuviz/local-auth.token`. Both files have mode `0600`; the username
must match `[A-Za-z0-9._-]{1,64}`. The service receives the validated username
and the password-file path. To choose a different username, replace the first
file's contents, keep it mode `0600`, and rerun the installer.

The service installer selects exactly one connector:

- A nonempty `~/.config/gpuviz/cloudflare-tunnel.token` selects the persistent
  named-tunnel unit, `cloudflared-gpuviz.service`. The origin is a mode-`0600`
  Unix socket.
- With no named-tunnel token, it selects
  `cloudflared-gpuviz-quick.service` plus the
  `gpuviz-quick-proxy.socket`/`.service` socket-activation pair. The proxy owns
  fixed loopback port `127.0.0.1:18770` and forwards to the same private Unix
  socket; installation fails closed if the port is occupied.

The installer stops and disables the inactive connector before starting the
selected one, so named and Quick Tunnels never proxy the app simultaneously.

For named-tunnel services to survive logout and restart after boot, an
administrator must enable lingering:

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
The service receives only the token-file path. Rerunning the installer switches
from a Quick Tunnel to the named Tunnel and disables the Quick unit.

`cloudflared` 2025.4.0 and newer supports `--token-file`; the pinned version
meets that requirement.

## 4. Verify production

```bash
systemctl --user --no-pager --full status gpuviz.service cloudflared-gpuviz.service
journalctl --user -u gpuviz.service -u cloudflared-gpuviz.service --since=-10m
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
gpuviz_user="$(<"${XDG_CONFIG_HOME:-$HOME/.config}/gpuviz/local-auth.user")"
curl --user "${gpuviz_user}" --unix-socket "${runtime_dir}/gpuviz/gpuviz.sock" \
  http://localhost/api/health
curl -I https://gpuviz.example.com/
```

When `curl` prompts for the password, paste the contents of
`~/.config/gpuviz/local-auth.token`. Do not put that password directly on the
command line, where other local users could briefly see it in the process list.

The public request should redirect unauthenticated callers to Cloudflare Access.
After Access authentication, the browser should show an HTTP Basic prompt. Use
the username from `~/.config/gpuviz/local-auth.user` and the local auth token as the
password. Then verify the Cluster, Projects, Jobs, Queue, Activity, and Ad-hoc
views before exercising a write action. Start with a guarded, tiny holder job;
live submit/migrate against real allocations is operationally sensitive.

Strong passwords are the default and are strongly recommended for every public
deployment. To use an existing password between 8 and 31 UTF-8 characters, an
operator must explicitly create this exact, private sentinel and rerun the
installer:

```bash
allow_weak_file="${XDG_CONFIG_HOME:-$HOME/.config}/gpuviz/local-auth.allow-weak"
(umask 077; printf %s 'I accept weak public authentication' > "${allow_weak_file}")
./deploy/cloudflare/install-user-services.sh
```

The installer accepts the weaker password only when that path is an owned,
regular, mode-`0600` file whose bytes exactly equal
`I accept weak public authentication` (with no trailing newline). A missing,
unsafe, or malformed opt-in never lowers the 32-character minimum; unsafe and
malformed files make installation fail closed. The service receives the
acknowledgement-file path and independently revalidates it at startup. Remove
the sentinel, replace the password with at least 32 characters, and rerun the
installer to restore the recommended policy.

## Temporary no-sudo Quick Tunnel

When `cloudflare-tunnel.token` is absent or empty, running
`install-user-services.sh` starts a Quick Tunnel without a Cloudflare account,
domain, Access application, or sudo. `cloudflared` does not support a Quick
Tunnel with `--unix-socket`, so a systemd socket-activation proxy connects its
loopback-only `127.0.0.1:18770` origin to the app's mode-`0600` Unix socket.
Other users on the login host can reach loopback, but they cannot replace the
systemd-owned listener or reach the app without the configured password.
The installer verifies authenticated health over both the Unix socket and the
proxy before it starts cloudflared.

Retrieve the current random URL and credentials with:

```bash
journalctl --user -u cloudflared-gpuviz-quick.service -o cat --no-pager \
  | grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
gpuviz_user="$(<"${XDG_CONFIG_HOME:-$HOME/.config}/gpuviz/local-auth.user")"
printf 'username: %s\npassword: ' "${gpuviz_user}"
cat "${XDG_CONFIG_HOME:-$HOME/.config}/gpuviz/local-auth.token"
printf '\n'
```

Open the URL and enter the configured username plus that password in the
browser's Basic-auth prompt. Treat both values as credentials; anyone with them
can control your Slurm jobs.

Quick Tunnel limitations are intentional:

- It is a development/testing facility with no SLA or uptime guarantee.
- Its random `*.trycloudflare.com` URL changes on every connector start or
  restart, including an automatic restart after a failure.
- With user lingering disabled, it ends when the last login session ends. The
  enabled user units start again at the next login and receive a new URL. If an
  administrator enables lingering, they can also survive logout and boot, but
  the URL still changes whenever cloudflared restarts.
- It cannot use your Cloudflare Access policy or a custom hostname. The app's
  Basic authentication is the only access boundary.
- Cloudflare currently limits Quick Tunnels to 200 in-flight requests and does
  not support Server-Sent Events.
- Stock `cloudflared` also exposes unauthenticated diagnostics on a random
  loopback port. Other login-host users can discover the temporary hostname or
  induce profiling load, although info-level logging does not emit HTTP
  authorization headers. This is another reason to keep Quick mode temporary.

Stop a temporary session explicitly with:

```bash
systemctl --user stop cloudflared-gpuviz-quick.service \
  gpuviz-quick-proxy.socket gpuviz.service
```

The Quick unit passes `--config=/dev/null` deliberately. Cloudflare documents
that Quick Tunnels are incompatible with a discovered `config.yml` or
`config.yaml`; this prevents a personal or system cloudflared configuration
from silently changing the selected tunnel. Add a named-tunnel token and rerun
the installer for a stable, Access-protected deployment.

Cross-site staging invokes noninteractive SSH. A lingered service has no
interactive `SSH_AUTH_SOCK`, so leave staging unused unless `BatchMode` keys and
`known_hosts` have been provisioned and tested for this service account.

Cloudflare references:

- [Protect a self-hosted application with Access](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Quick Tunnel behavior and limitations](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [Run Cloudflare Tunnel as a service](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/)
- [Tunnel run parameters and token files](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)
