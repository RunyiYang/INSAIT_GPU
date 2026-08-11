#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(cd "${script_dir}/../.." && pwd -P)"
if [[ -x /usr/bin/python3 ]]; then
  python_bin="/usr/bin/python3"
else
  python_bin="$(command -v python3)"
fi
if [[ -x "${HOME}/.local/bin/cloudflared" ]]; then
  cloudflared_bin="${HOME}/.local/bin/cloudflared"
else
  cloudflared_bin="$(command -v cloudflared || true)"
fi
config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
unit_dir="${config_root}/systemd/user"
gpuviz_config_dir="${config_root}/gpuviz"
auth_token_file="${gpuviz_config_dir}/local-auth.token"
named_token_file="${gpuviz_config_dir}/cloudflare-tunnel.token"
named_unit="cloudflared-gpuviz.service"
quick_unit="cloudflared-gpuviz-quick.service"
proxy_socket_unit="gpuviz-quick-proxy.socket"
proxy_service_unit="gpuviz-quick-proxy.service"
proxy_bin="/usr/lib/systemd/systemd-socket-proxyd"
listen_args="--socket=%t/gpuviz/gpuviz.sock"

install -d -m 0700 "${unit_dir}" "${gpuviz_config_dir}"
if [[ -L "${auth_token_file}" ]] ||
   [[ -e "${auth_token_file}" &&
      ( ! -f "${auth_token_file}" || ! -O "${auth_token_file}" ) ]]; then
  echo "Refusing unsafe app auth token path: ${auth_token_file}" >&2
  exit 1
fi
if [[ ! -s "${auth_token_file}" ]]; then
  auth_token="$("${python_bin}" -c 'import secrets; print(secrets.token_hex(32))')"
  (umask 077; printf '%s\n' "${auth_token}" > "${auth_token_file}")
  unset auth_token
  echo "Generated a private 256-bit app auth token at ${auth_token_file}."
fi
chmod 0600 "${auth_token_file}"
if ! "${python_bin}" -c \
  'import pathlib, sys; token = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").strip(); raise SystemExit(0 if len(token) >= 32 else 1)' \
  "${auth_token_file}"; then
  echo "App auth token must contain at least 32 UTF-8 characters: ${auth_token_file}" >&2
  exit 1
fi

if [[ -z "${cloudflared_bin}" ]]; then
  echo "cloudflared was not found; run ./deploy/cloudflare/install-cloudflared.sh first." >&2
  exit 1
fi
if [[ -L "${named_token_file}" ]] ||
   [[ -e "${named_token_file}" &&
      ( ! -f "${named_token_file}" || ! -O "${named_token_file}" ) ]]; then
  echo "Refusing unsafe named-tunnel token path: ${named_token_file}" >&2
  exit 1
fi

if [[ -s "${named_token_file}" ]]; then
  mode="named"
  connector_unit="${named_unit}"
  chmod 0600 "${named_token_file}"
  sed -e "s|@@CLOUDFLARED@@|${cloudflared_bin}|g" \
      -e "s|@@TOKEN_FILE@@|${named_token_file}|g" \
    "${script_dir}/cloudflared-gpuviz.service.in" > "${unit_dir}/${named_unit}"
  chmod 0600 "${unit_dir}/${named_unit}"
else
  mode="quick"
  connector_unit="${quick_unit}"
  if [[ ! -x "${proxy_bin}" ]]; then
    echo "Quick Tunnel requires ${proxy_bin}, but it is not installed." >&2
    exit 1
  fi
  sed -e "s|@@CLOUDFLARED@@|${cloudflared_bin}|g" \
    "${script_dir}/cloudflared-gpuviz-quick.service.in" > "${unit_dir}/${quick_unit}"
  chmod 0600 "${unit_dir}/${quick_unit}"
  install -m 0600 "${script_dir}/gpuviz-quick-proxy.socket.in" \
    "${unit_dir}/${proxy_socket_unit}"
  install -m 0600 "${script_dir}/gpuviz-quick-proxy.service.in" \
    "${unit_dir}/${proxy_service_unit}"
fi

sed -e "s|@@REPO_DIR@@|${repo_dir}|g" \
    -e "s|@@PYTHON@@|${python_bin}|g" \
    -e "s|@@LISTEN_ARGS@@|${listen_args}|g" \
    -e "s|@@AUTH_TOKEN_FILE@@|${auth_token_file}|g" \
  "${script_dir}/gpuviz.service.in" > "${unit_dir}/gpuviz.service"
chmod 0600 "${unit_dir}/gpuviz.service"

wait_for_health() {
  local transport="$1"
  local endpoint="$2"
  "${python_bin}" -c 'import base64
import pathlib
import socket
import sys
import time

transport, endpoint, token_path = sys.argv[1:]
token = pathlib.Path(token_path).read_text(encoding="utf-8").strip()
credential = base64.b64encode(f"gpuviz:{token}".encode()).decode()
request = ("GET /api/health HTTP/1.1\r\nHost: localhost\r\n"
           f"Authorization: Basic {credential}\r\nConnection: close\r\n\r\n").encode()
deadline = time.monotonic() + 10
last_error = None
while time.monotonic() < deadline:
    client = None
    try:
        if transport == "unix":
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(endpoint)
        else:
            host, port = endpoint.rsplit(":", 1)
            client = socket.create_connection((host, int(port)), timeout=1)
        client.sendall(request)
        if client.recv(4096).startswith(b"HTTP/1.1 200"):
            raise SystemExit(0)
    except OSError as error:
        last_error = error
    finally:
        if client is not None:
            client.close()
    time.sleep(0.1)
print(f"authenticated origin health check failed: {last_error!r}", file=sys.stderr)
raise SystemExit(1)' "${transport}" "${endpoint}" "${auth_token_file}"
}

require_active() {
  local unit="$1"
  local state=""
  for _ in {1..120}; do
    state="$(systemctl --user is-active "${unit}" 2>/dev/null || true)"
    if [[ "${state}" == "active" ]]; then
      return 0
    fi
    if [[ "${state}" == "failed" ]]; then
      break
    fi
    sleep 0.25
  done
  echo "Required user unit did not become active: ${unit} (${state:-unknown})" >&2
  systemctl --user --no-pager --full status "${unit}" >&2 || true
  return 1
}

runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
unix_socket="${runtime_dir}/gpuviz/gpuviz.sock"
systemctl --user daemon-reload
if [[ "${mode}" == "named" ]]; then
  systemctl --user disable --now "${quick_unit}" >/dev/null 2>&1 || true
  systemctl --user disable --now "${proxy_socket_unit}" >/dev/null 2>&1 || true
  systemctl --user stop "${proxy_service_unit}" >/dev/null 2>&1 || true
  systemctl --user enable gpuviz.service "${named_unit}"
  systemctl --user restart gpuviz.service
  wait_for_health unix "${unix_socket}"
  require_active gpuviz.service
  systemctl --user restart --no-block "${named_unit}"
  require_active "${named_unit}"
  echo "Selected the named Cloudflare Tunnel (production mode)."
else
  systemctl --user disable --now "${named_unit}" >/dev/null 2>&1 || true
  systemctl --user disable --now "${quick_unit}" >/dev/null 2>&1 || true
  systemctl --user disable --now "${proxy_socket_unit}" >/dev/null 2>&1 || true
  systemctl --user stop "${proxy_service_unit}" >/dev/null 2>&1 || true
  systemctl --user enable gpuviz.service "${proxy_socket_unit}" "${quick_unit}"
  systemctl --user restart gpuviz.service
  wait_for_health unix "${unix_socket}"
  require_active gpuviz.service
  if ! systemctl --user start "${proxy_socket_unit}"; then
    echo "Quick Tunnel cannot bind 127.0.0.1:18770; the port is occupied." >&2
    exit 1
  fi
  require_active "${proxy_socket_unit}"
  wait_for_health tcp 127.0.0.1:18770
  require_active "${proxy_service_unit}"
  systemctl --user restart --no-block "${quick_unit}"
  require_active "${quick_unit}"
  echo "Selected a session-scoped Cloudflare Quick Tunnel (development mode)."
fi

linger="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
if [[ "${mode}" == "named" && "${linger}" != "yes" ]]; then
  echo "WARNING: user lingering is disabled; named-tunnel services stop at logout."
  echo "Ask an administrator to run: loginctl enable-linger $(id -un)"
elif [[ "${mode}" == "quick" ]]; then
  echo "Quick Tunnel URL:"
  echo "  journalctl --user -u ${quick_unit} -o cat --no-pager | grep -Eo 'https://[a-z0-9-]+\\.trycloudflare\\.com' | tail -1"
  echo "Credentials: username gpuviz; password is in ${auth_token_file}"
  if [[ "${linger}" != "yes" ]]; then
    echo "This temporary tunnel stops at logout and receives a new URL at the next login."
  fi
fi

if [[ "${mode}" == "quick" ]]; then
  systemctl --user --no-pager --full status gpuviz.service "${proxy_socket_unit}" \
    "${proxy_service_unit}" "${connector_unit}" || true
else
  systemctl --user --no-pager --full status gpuviz.service "${connector_unit}" || true
fi
