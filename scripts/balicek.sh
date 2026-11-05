#!/usr/bin/env bash
# ZIP pro netechnického uživatele: rozbalit → pravým na Instalovat.command → Otevřít.
# Použití: scripts/balicek.sh <IČO> <výstup.zip> [další volby pro ucetka init, např. --skutecne-vydaje --zamestnanec]
set -euo pipefail

ICO="$1"; OUT="$2"; shift 2
INIT_FLAGS="$*"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
GIT="${GIT:-git}"
TMP="$(mktemp -d)"
PKG="$TMP/ucetka"

mkdir -p "$PKG"
"$GIT" -C "$REPO" archive HEAD | tar -x -C "$PKG"
# Vývojářské soubory do balíčku nepatří.
rm -rf "$PKG/tests" "$PKG/examples" "$PKG/Makefile" "$PKG/scripts" "$PKG/.github" "$PKG/docs"

cat > "$PKG/Instalovat.command" <<EOF
#!/bin/bash
set -e
SRC="\$(cd "\$(dirname "\$0")" && pwd)"
DEST="\$HOME/ucetka"
echo "Instaluji ucetka do \$DEST …"
mkdir -p "\$DEST"
rsync -a --delete --exclude ".venv" "\$SRC/" "\$DEST/"
cd "\$DEST"
if ! command -v brew >/dev/null && [ ! -x /opt/homebrew/bin/brew ] && [ ! -x /usr/local/bin/brew ]; then
  echo
  echo "Nejdřív se nainstaluje Homebrew (správce programů pro Mac). Zeptá se na heslo k Macu."
  /bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
eval "\$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
./install.sh
if [ ! -f "\$HOME/.ucetka/supplier.yaml" ]; then
  echo
  echo "Teď pár otázek k tvému podnikání (předvyplněné odpovědi potvrdíš klávesou Enter):"
  "\$HOME/.local/bin/ucetka" init --ico $ICO $INIT_FLAGS
fi
open "http://127.0.0.1:8765/prehled" || true
echo
echo "Hotovo. Toto okno můžeš zavřít."
read -r -p "Stiskni Enter…" _
EOF
chmod +x "$PKG/Instalovat.command"

cp "$REPO/scripts/NAVOD.txt" "$PKG/NAVOD.txt"

rm -f "$OUT"
(cd "$TMP" && zip -qrX "$OUT" ucetka)
rm -rf "$TMP"
echo "$OUT"
