#!/usr/bin/env bash
# Installeert of werkt de slingbag-software bij met de nieuwste release. Uitvoeren op de Pi:
#   curl -fsSL https://github.com/Rik-Stokmans/slingbag/releases/latest/download/install.sh | bash
# Nieuwste beta of een vaste versie:
#   ... | SLINGBAG_VERSIE=beta bash
#   ... | SLINGBAG_VERSIE=v0.2.0 bash
set -euo pipefail

REPO="${SLINGBAG_REPO:-Rik-Stokmans/slingbag}"
VERSIE="${SLINGBAG_VERSIE:-latest}"
APP_DIR="$HOME/slingbag"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

case "$VERSIE" in
  latest) URL="https://github.com/$REPO/releases/latest/download/slingbag.zip" ;;
  beta)
    # De nieuwste release, ook als dat een pre-release (beta) is.
    TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=20" \
      | python3 -c 'import json, sys; print(next(r["tag_name"] for r in json.load(sys.stdin) if not r["draft"]))')
    URL="https://github.com/$REPO/releases/download/$TAG/slingbag.zip" ;;
  *) URL="https://github.com/$REPO/releases/download/$VERSIE/slingbag.zip" ;;
esac

echo "==> Release ($VERSIE) van $REPO downloaden"
curl -fL --retry 3 -sS -o "$TMP/slingbag.zip" "$URL"
python3 -m zipfile -e "$TMP/slingbag.zip" "$TMP"

echo "==> Bestanden plaatsen in $APP_DIR"
mkdir -p "$APP_DIR"
# Oude bestanden weghalen, maar vendor/ (gedownloade stemmen) bewaren.
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name vendor -exec rm -rf {} +
cp -a "$TMP/slingbag/." "$APP_DIR/"

bash "$APP_DIR/deploy/install-pi.sh"
