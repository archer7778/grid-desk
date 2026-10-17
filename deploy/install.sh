#!/usr/bin/env bash
# Grid Desk installer for Debian, Ubuntu and Raspberry Pi OS. Run as your normal user:
#   ./deploy/install.sh
# Safe to run again; it keeps your config.toml and data.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$ROOT"
if [ "$(id -u)" -eq 0 ]; then echo "Run this as your normal user (it asks for sudo when needed): ./deploy/install.sh"; exit 1; fi
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/3  Checking Python (3.11 or newer; no other packages needed)"
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || {
  echo "Python 3.11+ is needed. On older systems: sudo apt install python3.11"; exit 1; }

say "2/3  Configuration"
mkdir -p data
if [ ! -f config.toml ]; then cp config.example.toml config.toml; echo "  Wrote config.toml (edit any time, then: sudo systemctl restart griddesk-web)"; else echo "  Keeping your config.toml"; fi
chmod 600 config.toml   # can hold your private alert topic

say "3/3  Background services (fetch every 5 minutes, console at boot)"
for unit in griddesk-web.service griddesk-fetch.service griddesk-fetch.timer; do
  sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" "deploy/${unit}" | sudo tee "/etc/systemd/system/${unit}" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now griddesk-web.service griddesk-fetch.timer >/dev/null
sudo systemctl restart griddesk-web.service
PORT=$(awk -F= '$1 ~ /^port[ ]*$/ {gsub(/[ "]/,"",$2); print $2; exit}' config.toml); PORT=${PORT:-8090}
echo "  Console: http://$(hostname).local:${PORT}"
echo "  The first fetch runs within a minute. Four weeks of history fill in over the next day,"
echo "  one day per fetch; or all at once: python3 -m griddesk.fetch --history 28"
echo "  Logs: journalctl -u griddesk-web -u griddesk-fetch -f"
