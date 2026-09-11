#!/usr/bin/env bash
# Bifrost installer. Idempotent: safe to run again after every git pull.
#
#   1. writes a default ~/.config/bifrost/config.json if none exists
#   2. links the daemon into ~/.local/bin
#   3. installs and enables the bifrost.service systemd user unit
#   4. links (or copies) the Fusion add-in into the Wine prefix AddIns folder
#
# Usage: ./install.sh [--copy] [--prefix <wineprefix>] [--no-service]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/bifrost"
CONFIG_FILE="$CONFIG_DIR/config.json"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
WINEPREFIX_DIR="${WINEPREFIX:-$HOME/.autodesk_fusion/wineprefixes/default}"
LINK_MODE="symlink"
INSTALL_SERVICE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --copy) LINK_MODE="copy"; shift ;;
        --prefix) WINEPREFIX_DIR="$2"; shift 2 ;;
        --no-service) INSTALL_SERVICE=0; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '  %s\n' "$*"; }

echo "Bifrost install"
echo "  repo:       $REPO_DIR"
echo "  wineprefix: $WINEPREFIX_DIR"

# 1. default config -----------------------------------------------------------
mkdir -p "$CONFIG_DIR"
if [[ -f "$CONFIG_FILE" ]]; then
    say "config kept:    $CONFIG_FILE"
else
    cp "$REPO_DIR/config.default.json" "$CONFIG_FILE"
    say "config written: $CONFIG_FILE"
fi
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CONFIG_FILE" \
    || { echo "config is not valid JSON: $CONFIG_FILE" >&2; exit 1; }

# 2. daemon on PATH -----------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/daemon/bifrost_daemon.py" "$BIN_DIR/bifrost-daemon"
chmod +x "$REPO_DIR/daemon/bifrost_daemon.py"
say "daemon linked:  $BIN_DIR/bifrost-daemon"

# 3. systemd user service -----------------------------------------------------
if [[ "$INSTALL_SERVICE" == "1" ]]; then
    mkdir -p "$UNIT_DIR"
    sed "s|@DAEMON@|$REPO_DIR/daemon/bifrost_daemon.py|g" \
        "$REPO_DIR/systemd/bifrost.service" > "$UNIT_DIR/bifrost.service"
    systemctl --user daemon-reload
    systemctl --user enable bifrost.service >/dev/null
    systemctl --user restart bifrost.service
    say "service:        bifrost.service enabled and restarted"
else
    say "service:        skipped (--no-service)"
fi

# 4. Fusion add-in ------------------------------------------------------------
ADDINS_DIR="$WINEPREFIX_DIR/drive_c/users/$USER/AppData/Roaming/Autodesk/Autodesk Fusion 360/API/AddIns"
if [[ ! -d "$(dirname "$ADDINS_DIR")" ]]; then
    echo "  WARNING: no Fusion API folder under $WINEPREFIX_DIR, add-in not installed" >&2
else
    mkdir -p "$ADDINS_DIR"
    TARGET="$ADDINS_DIR/Bifrost"
    if [[ "$LINK_MODE" == "copy" ]]; then
        rm -rf "$TARGET"
        cp -r "$REPO_DIR/fusion_addin/Bifrost" "$TARGET"
        say "add-in copied:  $TARGET"
    else
        rm -rf "$TARGET"
        ln -sfn "$REPO_DIR/fusion_addin/Bifrost" "$TARGET"
        say "add-in linked:  $TARGET"
    fi
fi

# 5. register the add-in for autostart ---------------------------------------
python3 "$REPO_DIR/tools/register_addin.py" --prefix "$WINEPREFIX_DIR" || \
    say "autostart:      could not update Fusion's script registry, tick Run on Startup by hand"

echo
echo "Done. Check the daemon with:"
echo "  systemctl --user status bifrost.service"
echo "  $REPO_DIR/daemon/bifrost_daemon.py --tail --seconds 5"
