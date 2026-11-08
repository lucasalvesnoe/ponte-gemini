#!/usr/bin/env bash
# Remove o servico do launchd. Nao apaga o codigo nem o venv.
set -euo pipefail
LABEL="${PONTE_LABEL:-com.ponte-gemini}"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
[ -f "$PLIST" ] && mv "$PLIST" "$PLIST.disabled"
echo "Servico $LABEL parado. Plist renomeado para $PLIST.disabled"
