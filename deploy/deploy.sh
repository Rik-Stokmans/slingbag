#!/usr/bin/env bash
# Zet de lokale werkkopie vanaf een computer op de Pi en installeert de service (voor ontwikkelen).
#   ./deploy/deploy.sh                      Pi rechtstreeks (standaard rik@slingbag.local)
#   PI=rik@169.254.12.34 ./deploy/deploy.sh ander adres
#   JUMP=rik@laptop ./deploy/deploy.sh      via de laptop als de Pi daar aan hangt
set -euo pipefail

PI="${PI:-rik@slingbag.local}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ -n "${JUMP:-}" ] && SSH_OPTS+=(-J "$JUMP")
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Verbinding met $PI controleren"
ssh "${SSH_OPTS[@]}" "$PI" 'echo "verbonden met $(hostname), $(uname -m), $(. /etc/os-release; echo $PRETTY_NAME)"'

echo "==> Bestanden kopiëren"
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude 'vendor/piper/' \
  "$HERE/" "$PI:slingbag/"

echo "==> Installeren op de Pi"
ssh "${SSH_OPTS[@]}" "$PI" 'bash ~/slingbag/deploy/install-pi.sh'
