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
token_file="${gpuviz_config_dir}/cloudflare-tunnel.token"

install -d -m 0700 "${unit_dir}" "${gpuviz_config_dir}"
sed -e "s|@@REPO_DIR@@|${repo_dir}|g" -e "s|@@PYTHON@@|${python_bin}|g" \
  "${script_dir}/gpuviz.service.in" > "${unit_dir}/gpuviz.service"
chmod 0600 "${unit_dir}/gpuviz.service"

systemctl --user daemon-reload
systemctl --user enable gpuviz.service
systemctl --user restart gpuviz.service

if [[ -n "${cloudflared_bin}" && -s "${token_file}" ]]; then
  chmod 0600 "${token_file}"
  sed -e "s|@@CLOUDFLARED@@|${cloudflared_bin}|g" -e "s|@@TOKEN_FILE@@|${token_file}|g" \
    "${script_dir}/cloudflared-gpuviz.service.in" > "${unit_dir}/cloudflared-gpuviz.service"
  chmod 0600 "${unit_dir}/cloudflared-gpuviz.service"
  systemctl --user daemon-reload
  systemctl --user enable cloudflared-gpuviz.service
  systemctl --user restart cloudflared-gpuviz.service
else
  systemctl --user disable --now cloudflared-gpuviz.service >/dev/null 2>&1 || true
  echo "GPU Visualizer is running, but the tunnel was not installed."
  echo "Install cloudflared and place the named-tunnel token at: ${token_file}"
fi

if [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
  echo "WARNING: user lingering is disabled; ask an administrator to run:"
  echo "  loginctl enable-linger $(id -un)"
fi

systemctl --user --no-pager --full status gpuviz.service || true
if systemctl --user is-enabled cloudflared-gpuviz.service >/dev/null 2>&1; then
  systemctl --user --no-pager --full status cloudflared-gpuviz.service || true
fi
