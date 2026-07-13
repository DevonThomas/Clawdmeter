#!/bin/bash
# Build and install Clawdmeter.app — a macOS menu bar front-end for the BLE
# usage daemon. Replaces the silent LaunchAgent (install-mac.sh) with a visible
# app you launch manually and see in the menu bar / Applications.
#
# The .app is a thin launcher: it runs the daemon's venv Python on
# daemon/clawdmeter_menubar.py, so it stays in sync with the repo. Moving the
# repo means re-running this script.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/daemon/.venv"
APP_NAME="Clawdmeter"
OLD_LABEL="com.user.claude-usage-daemon"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"

echo "=== Clawdmeter menu bar app install ==="

# 1. Prerequisites: an arm64-NATIVE venv + rumps/Pillow/bleak/httpx.
# On Apple Silicon we insist on an interpreter with NO x86_64 slice, so no
# Intel/Rosetta code is involved — future-proof once Rosetta is removed, and it
# silences the macOS "Intel-based app support ending" notification. (The
# python.org framework build is universal2 and still carries an x86_64 slice,
# which trips that warning even though it runs arm64.)
pick_python() {
    local cand real archs
    for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                /opt/homebrew/bin/python3 "$(command -v python3.13)" \
                "$(command -v python3.12)" "$(command -v python3)"; do
        [ -n "$cand" ] && [ -x "$cand" ] || continue
        if [ "$(uname -m)" = "arm64" ]; then
            real="$(readlink -f "$cand")"; archs="$(lipo -archs "$real" 2>/dev/null)"
            case " $archs " in *" x86_64 "*) continue ;; esac  # skip Intel-carrying builds
            case " $archs " in *" arm64 "*) : ;; *) continue ;; esac
        fi
        echo "$cand"; return 0
    done
    return 1
}
venv_is_clean_native() {
    [ -x "$VENV/bin/python" ] || return 1
    [ "$(uname -m)" != "arm64" ] && return 0
    local real archs
    real="$(readlink -f "$VENV/bin/python")"; archs="$(lipo -archs "$real" 2>/dev/null)"
    case " $archs " in *" x86_64 "*) return 1 ;; esac
    case " $archs " in *" arm64 "*) return 0 ;; esac
    return 1
}

echo "[1/5] Ensuring an arm64-native Python venv + deps..."
if ! venv_is_clean_native; then
    PY="$(pick_python)" || { echo "Error: no arm64 python3 found (try: brew install python)" >&2; exit 1; }
    echo "  (Re)building venv with $("$PY" --version 2>&1) [$PY]"
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "rumps>=0.4" "Pillow>=10" "bleak>=0.22" \
    "httpx>=0.27" "pyobjc-framework-WebKit>=10"

# 2. Build the app icon (.icns) + menu-bar PNG from the Clawd mascot asset.
echo "[2/5] Generating icon from assets/logo_80.png..."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/Clawdmeter.iconset"
mkdir -p "$ICONSET"
"$VENV/bin/python" - "$REPO_DIR/assets/logo_80.png" "$ICONSET" "$WORK/menubar.png" <<'PY'
import sys
from PIL import Image

src_path, iconset, menubar = sys.argv[1], sys.argv[2], sys.argv[3]
src = Image.open(src_path).convert("RGBA")

def render(size, margin=0.82):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = max(1, int(round(size * margin)))
    resample = Image.NEAREST if inner >= src.width else Image.LANCZOS
    art = src.resize((inner, inner), resample)
    off = (size - inner) // 2
    canvas.paste(art, (off, off), art)
    return canvas

# .icns iconset (Apple's required names/sizes)
for base in (16, 32, 128, 256, 512):
    render(base).save(f"{iconset}/icon_{base}x{base}.png")
    render(base * 2).save(f"{iconset}/icon_{base}x{base}@2x.png")

# Menu-bar icon: no margin, crisp small pixel art.
menu = src.resize((44, 44), Image.LANCZOS)
menu.save(menubar)
print("icon sizes rendered")
PY
iconutil -c icns "$ICONSET" -o "$WORK/$APP_NAME.icns"

# 3. Assemble the .app bundle in the repo (macos/Clawdmeter.app is the source).
echo "[3/5] Assembling $APP_NAME.app..."
SRC_APP="$REPO_DIR/macos/$APP_NAME.app"
rm -rf "$SRC_APP"
mkdir -p "$SRC_APP/Contents/MacOS" "$SRC_APP/Contents/Resources"
cp "$WORK/$APP_NAME.icns" "$SRC_APP/Contents/Resources/$APP_NAME.icns"
cp "$WORK/menubar.png" "$SRC_APP/Contents/Resources/menubar.png"

# Decide the final install location up front so the launcher stub can point the
# Launch-at-Login item at the right place.
if [ -w /Applications ]; then
    DEST_DIR="/Applications"
else
    DEST_DIR="$HOME/Applications"
    mkdir -p "$DEST_DIR"
fi
DEST_APP="$DEST_DIR/$APP_NAME.app"

cat > "$SRC_APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>com.devonthomas.clawdmeter</string>
    <key>CFBundleExecutable</key><string>$APP_NAME</string>
    <key>CFBundleIconFile</key><string>$APP_NAME</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>LSUIElement</key><true/>
    <key>NSBluetoothAlwaysUsageDescription</key>
    <string>Clawdmeter connects to your ESP32 usage display over Bluetooth.</string>
    <key>NSBluetoothPeripheralUsageDescription</key>
    <string>Clawdmeter connects to your ESP32 usage display over Bluetooth.</string>
</dict>
</plist>
PLIST

# Launcher stub. Two modes, both running from *inside* the bundle so they carry
# the Info.plist's NSBluetoothAlwaysUsageDescription (required — CoreBluetooth
# aborts a process without it):
#   (no args)   -> the rumps menu bar app
#   --daemon    -> the headless BLE daemon child (stdout inherited so the menu
#                  bar app can read it; NOT redirected to the log here)
cat > "$SRC_APP/Contents/MacOS/$APP_NAME" <<STUB
#!/bin/bash
# Generated by install-mac-app.sh.
REPO="$REPO_DIR"
VENV_PY="\$REPO/daemon/.venv/bin/python"
if [ "\$1" = "--daemon" ]; then
    exec "\$VENV_PY" "\$REPO/daemon/claude_usage_daemon.py"
fi
export CLAWDMETER_ICON="$DEST_APP/Contents/Resources/menubar.png"
export CLAWDMETER_APP="$DEST_APP"
export CLAWDMETER_EXEC="$DEST_APP/Contents/MacOS/$APP_NAME"
LOG="\$HOME/Library/Logs/clawdmeter-app.log"
cd "\$REPO/daemon"
# Launch DETACHED, not exec: a bundle-exec'd framework Python doesn't get a
# status-bar item (its NSBundle.mainBundle is Python.framework, not this .app,
# so LaunchServices never attaches the item). An orphaned process does. The
# app sets its own accessory activation policy so there's still no Dock icon.
nohup "\$VENV_PY" "\$REPO/daemon/clawdmeter_menubar.py" >>"\$LOG" 2>&1 &
exit 0
STUB
chmod +x "$SRC_APP/Contents/MacOS/$APP_NAME"

# 4. Retire the silent LaunchAgent so two BLE centrals don't fight the board.
echo "[4/5] Retiring the background LaunchAgent (if present)..."
if [ -f "$OLD_PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null \
        || launchctl unload "$OLD_PLIST" 2>/dev/null || true
    mv "$OLD_PLIST" "$OLD_PLIST.disabled"
    echo "  Unloaded and disabled $OLD_LABEL (backed up to $OLD_PLIST.disabled)."
else
    echo "  No active LaunchAgent found — nothing to retire."
fi

# 5. Install into Applications and register with LaunchServices.
echo "[5/5] Installing to $DEST_DIR ..."
rm -rf "$DEST_APP"
cp -R "$SRC_APP" "$DEST_APP"
touch "$DEST_APP"   # nudge LaunchServices to re-read the bundle
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$DEST_APP" 2>/dev/null || true

echo ""
echo "=== Done ==="
echo "Installed: $DEST_APP"
echo "Launch it from Applications/Spotlight, or run:  open -a \"$APP_NAME\""
echo "It appears in the menu bar (top-right). Menu → Quit to stop it."
echo "Log: ~/Library/Logs/clawdmeter-app.log"
