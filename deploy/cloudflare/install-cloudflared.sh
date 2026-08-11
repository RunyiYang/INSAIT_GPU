#!/usr/bin/env bash
set -euo pipefail

cloudflared_version="2026.7.3"

case "$(uname -m)" in
  x86_64|amd64)
    artifact_name="cloudflared-linux-amd64"
    cloudflared_sha256="9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17"
    ;;
  aarch64|arm64)
    artifact_name="cloudflared-linux-arm64"
    cloudflared_sha256="65259e652a7bea08bf5df603233ab22b8bf3116af8df9f9206209af6a1b955c0"
    ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

download_url="https://github.com/cloudflare/cloudflared/releases/download/${cloudflared_version}/${artifact_name}"

user_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
if [[ -z "${user_home}" || ! -d "${user_home}" ]]; then
  echo "Could not resolve the current user's home directory" >&2
  exit 1
fi

bin_dir="${user_home}/.local/bin"
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT

artifact="${tmp_dir}/cloudflared"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  "${download_url}" --output "${artifact}"
printf '%s  %s\n' "${cloudflared_sha256}" "${artifact}" | sha256sum --check --status

install -d -m 0755 "${bin_dir}"
install -m 0755 "${artifact}" "${bin_dir}/cloudflared"

"${bin_dir}/cloudflared" --version
echo "Installed the pinned connector at ${bin_dir}/cloudflared."
echo "Next run: ./deploy/cloudflare/install-user-services.sh"
