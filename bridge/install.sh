#!/bin/bash
# KJD RX Bridge installer — paste into Terminal:
#   curl -fsSL https://kjdombroski.github.io/kjd-edit/bridge/install.sh | bash
set -e
BASE="https://kjdombroski.github.io/kjd-edit/bridge/KJD%20RX%20Bridge.app/Contents"
DEST="$HOME/Desktop/KJD RX Bridge"; APP="$DEST/KJD RX Bridge.app"
pkill -f kjd_rx_bridge.py 2>/dev/null || true
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
curl -fsSL "$BASE/Info.plist" -o "$APP/Contents/Info.plist"
curl -fsSL "$BASE/MacOS/KJD%20RX%20Bridge" -o "$APP/Contents/MacOS/KJD RX Bridge"
curl -fsSL "$BASE/Resources/kjd_rx_bridge.py" -o "$APP/Contents/Resources/kjd_rx_bridge.py"
chmod +x "$APP/Contents/MacOS/KJD RX Bridge"
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
echo "Installed: $APP"
open "$APP"
