#!/usr/bin/env bash
# Snel bijwerken zonder internet op de Pi: kopieert alleen de code (en de Engelse stem) en herstart de service.
# Geen apt-get en geen downloads, dus dit werkt ook via een directe netwerkkabel tussen computer en Pi.
# Gebruik alleen als er geen nieuwe systeempakketten nodig zijn; anders deploy.sh.
#   ./deploy/snel-bijwerken.sh                      standaard rik@slingbag.local
#   PI=rik@169.254.12.34 ./deploy/snel-bijwerken.sh ander adres
#   JUMP=rik@laptop ./deploy/snel-bijwerken.sh      via een laptop waar de Pi aan hangt
set -euo pipefail

PI="${PI:-rik@slingbag.local}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ -n "${JUMP:-}" ] && SSH_OPTS+=(-J "$JUMP")
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Verbinding met $PI controleren"
ssh "${SSH_OPTS[@]}" "$PI" 'echo "verbonden met $(hostname)"'

echo "==> Code kopiëren"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  --exclude __pycache__ \
  "$HERE/slingbag" "$HERE/web" "$HERE/deploy" "$HERE/install.sh" "$HERE/README.md" "$PI:slingbag/"
rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/vendor/SHA256SUMS" "$PI:slingbag/vendor/"

echo "==> Engelse stem kopiëren (alleen als die nog ontbreekt)"
if ls "$HERE"/vendor/voices/en_US-*.onnx >/dev/null 2>&1; then
  rsync -az --ignore-existing -e "ssh ${SSH_OPTS[*]}" "$HERE"/vendor/voices/en_US-* "$PI:slingbag/vendor/voices/"
else
  echo "LET OP: geen Engelse stem in vendor/voices; haal hem op met ./deploy/download-vendor.sh"
fi

echo "==> Service herstarten"
ssh "${SSH_OPTS[@]}" "$PI" 'sudo systemctl restart slingbag && sleep 3 && systemctl is-active slingbag'
echo "Klaar. Open de app opnieuw en kies Instellingen → Taal."
