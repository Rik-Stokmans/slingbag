#!/usr/bin/env bash
# Haalt de Piper-engine (Linux arm64) en de drie Nederlandse stemmen op in vendor/
# en controleert ze met vendor/SHA256SUMS. Bestanden die er al zijn, worden overgeslagen.
set -euo pipefail

VENDOR="${1:-$(cd "$(dirname "$0")/.." && pwd)/vendor}"
PIPER_URL="https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz"
VOICES_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/nl/nl_NL"

mkdir -p "$VENDOR/voices"
haal() {
  if [ ! -s "$VENDOR/$1" ]; then
    echo "    $1"
    curl -fL --retry 3 -sS -o "$VENDOR/$1.part" "$2"
    mv "$VENDOR/$1.part" "$VENDOR/$1"
  fi
}

echo "==> Piper en stemmen ophalen"
haal piper_linux_aarch64.tar.gz "$PIPER_URL"
for stem in pim ronnie mls; do
  haal "voices/nl_NL-$stem-medium.onnx" "$VOICES_URL/$stem/medium/nl_NL-$stem-medium.onnx"
  haal "voices/nl_NL-$stem-medium.onnx.json" "$VOICES_URL/$stem/medium/nl_NL-$stem-medium.onnx.json"
done

echo "==> Controlesommen"
SUMS="$(cd "$(dirname "$0")/.." && pwd)/vendor/SHA256SUMS"
(cd "$VENDOR" && shasum -a 256 -c "$SUMS" 2>/dev/null || sha256sum -c "$SUMS")
