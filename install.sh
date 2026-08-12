#!/usr/bin/env bash
# Install herdr-codex-capacity-retry into ~/.local/bin via symlink.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/herdr-codex-capacity-retry.py"
TARGET_DIR="${HOME}/.local/bin"
TARGET="${TARGET_DIR}/herdr-codex-capacity-retry"

if [[ ! -f "${SOURCE}" ]]; then
  echo "error: source script not found: ${SOURCE}" >&2
  exit 1
fi

chmod +x "${SOURCE}"
mkdir -p "${TARGET_DIR}"

if [[ -L "${TARGET}" ]]; then
  current="$(readlink "${TARGET}")"
  if [[ "${current}" == "${SOURCE}" ]]; then
    echo "already installed: ${TARGET} -> ${SOURCE}"
    exit 0
  fi
  echo "replacing existing symlink: ${TARGET} -> ${current}"
  rm -f "${TARGET}"
elif [[ -e "${TARGET}" ]]; then
  echo "replacing existing file with symlink: ${TARGET}"
  rm -f "${TARGET}"
fi

ln -s "${SOURCE}" "${TARGET}"
# Ensure the linked name is executable for shells that check the link itself.
chmod -h +x "${TARGET}" 2>/dev/null || true

echo "installed: ${TARGET} -> ${SOURCE}"
echo "make sure ${TARGET_DIR} is on your PATH"
