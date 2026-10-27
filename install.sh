#!/usr/bin/env bash
# Installeert of werkt de slingbag-software bij met de nieuwste release. Uitvoeren op de Pi:
#   curl -fsSL https://github.com/Rik-Stokmans/slingbag/releases/latest/download/install.sh | bash
set -euo pipefail

REPO="${SLINGBAG_REPO:-Rik-Stokmans/slingbag}"
APP_DIR="$HOME/slingbag"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Nieuwste release van $REPO downloaden"
curl -fL --retry 3 -sS -o "$TMP/slingbag.zip" "https://github.com/$REPO/releases/latest/download/slingbag.zip"
python3 -m zipfile -e "$TMP/slingbag.zip" "$TMP"

echo "==> Bestanden plaatsen in $APP_DIR"
mkdir -p "$APP_DIR"
# Oude bestanden weghalen, maar vendor/ (gedownloade stemmen) bewaren.
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name vendor -exec rm -rf {} +
cp -a "$TMP/slingbag/." "$APP_DIR/"

bash "$APP_DIR/deploy/install-pi.sh"
