#!/usr/bin/env bash
# Instalace ucetka na macOS: závislosti, webový přehled po přihlášení, Faktury.app, MCP pro Antigravity a Claude Desktop.
# Spouštěj z kořene naklonovaného repa. Opakované spuštění je bezpečné.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LABEL="cz.ucetka.dashboard"
PORT=8765

command -v brew >/dev/null || { echo "Chybí Homebrew — nainstaluj ho z https://brew.sh a spusť znovu."; exit 1; }
BREW_PREFIX="$(brew --prefix)"

echo "→ Závislosti (uv, pango pro PDF)"
brew list uv >/dev/null 2>&1 || brew install uv
brew list pango >/dev/null 2>&1 || brew install pango
UV="$(command -v uv)"

echo "→ Python prostředí"
(cd "$REPO" && "$UV" sync --quiet)

echo "→ Příkaz ucetka do ~/.local/bin"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/ucetka" <<EOF
#!/usr/bin/env bash
# WeasyPrint hledá knihovny z Homebrew; DYLD_* musí být nastavené před startem procesu (SIP).
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/lib"
exec "$UV" --directory "$REPO" run python -m ucetka.cli "\$@"
EOF
chmod +x "$HOME/.local/bin/ucetka"
# Claude Desktop spouští MCP přes hardened helper, který DYLD_* z "env" v configu zahodí — proto vlastní wrapper.
# „python -m“ místo konzolového skriptu: v dlouhé cestě nebo cestě s mezerou ho uv spouští přes /bin/sh, který (SIP)
# DYLD_* zahodí a WeasyPrint pak nenajde knihovny.
cat > "$HOME/.local/bin/ucetka-mcp" <<EOF
#!/bin/zsh
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/lib"
exec "$UV" --directory "$REPO" run python -m ucetka.mcp_server
EOF
chmod +x "$HOME/.local/bin/ucetka-mcp"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  (přidej ~/.local/bin do PATH, např. do ~/.zshrc)";; esac

echo "→ Webový přehled po přihlášení (LaunchAgent $LABEL)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string><string>--directory</string><string>$REPO</string>
        <string>run</string><string>python</string><string>-m</string><string>ucetka.cli</string>
        <string>dashboard</string><string>--no-open</string><string>--port</string><string>$PORT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>DYLD_FALLBACK_LIBRARY_PATH</key><string>$BREW_PREFIX/lib</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/ucetka.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/ucetka.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "→ ~/Applications/Faktury.app"
mkdir -p "$HOME/Applications"
osacompile -o "$HOME/Applications/Faktury.app" -e "open location \"http://127.0.0.1:$PORT/prehled\""

register_mcp() {
  local config="$1"
  mkdir -p "$(dirname "$config")"
  [ -f "$config" ] && cp "$config" "$config.bak-$(date +%Y%m%d-%H%M%S)"
  "$UV" --directory "$REPO" run python - "$config" "$HOME/.local/bin/ucetka-mcp" <<'PY'
import json, sys
from pathlib import Path
config, wrapper = Path(sys.argv[1]), sys.argv[2]
data = json.loads(config.read_text()) if config.exists() and config.read_text().strip() else {}
data.setdefault("mcpServers", {})["ucetka"] = {"command": wrapper, "args": []}
config.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
}

echo "→ MCP server pro AI asistenty"
# Antigravity (Gemini) čte ~/.gemini/config/mcp_config.json; starší verze ~/.gemini/antigravity/.
register_mcp "$HOME/.gemini/config/mcp_config.json"
[ -d "$HOME/.gemini/antigravity" ] && register_mcp "$HOME/.gemini/antigravity/mcp_config.json"
[ -d "/Applications/Claude.app" ] && register_mcp "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
cat > "$REPO/mcp_config_pro_AI.json" <<EOF
{
  "mcpServers": {
    "ucetka": {
      "command": "$HOME/.local/bin/ucetka-mcp",
      "args": []
    }
  }
}
EOF

echo
echo "Hotovo. Další kroky:"
echo "  1. ucetka init                    (profil: IČO, účet, DPH, daně)"
echo "  2. restartuj Antigravity / Claude (nástroje ucetka v chatu)"
echo "  3. otevři Faktury.app             (přehled na http://127.0.0.1:$PORT/prehled)"
