#!/bin/bash
# KJD RX Bridge installer / updater — paste into Terminal:
#   curl -fsSL https://kjdombroski.github.io/kjd-edit/bridge/install.sh | bash
set -e
WEB="https://kjdombroski.github.io/kjd-edit/bridge"
BASE="$WEB/KJD%20RX%20Bridge.app/Contents"
DEST="$HOME/Desktop/KJD RX Bridge"; APP="$DEST/KJD RX Bridge.app"
AGENT="$HOME/Library/LaunchAgents/com.kjdproductions.rxbridge.plist"
echo "Installing KJD RX Bridge into $DEST …"
launchctl bootout "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 || true
pkill -f kjd_rx_bridge.py 2>/dev/null || true; pkill -f "KJD RX Bridge.app/Contents/Resources/kjd-audio" 2>/dev/null || true
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
curl -fsSL "$BASE/Info.plist" -o "$APP/Contents/Info.plist"
curl -fsSL "$BASE/MacOS/KJD%20RX%20Bridge" -o "$APP/Contents/MacOS/KJD RX Bridge"
curl -fsSL "$BASE/Resources/kjd_rx_bridge.py" -o "$APP/Contents/Resources/kjd_rx_bridge.py"
chmod +x "$APP/Contents/MacOS/KJD RX Bridge"
# live RX audio helper (ScreenCaptureKit) — compiled here with Apple's Command Line Tools
if xcrun --find swiftc >/dev/null 2>&1; then
  TMP="$(mktemp -d)"; curl -fsSL "$WEB/kjd_audio.swift" -o "$TMP/kjd_audio.swift"
  if xcrun swiftc -O "$TMP/kjd_audio.swift" -o "$APP/Contents/Resources/kjd-audio" 2>"$TMP/build.log"; then echo "Live audio helper built."
  else echo "Live audio helper didn't build (details: $TMP/build.log). Everything else works."; fi
else
  echo "Live audio needs Apple's Command Line Tools: run  xcode-select --install  and then run this installer again."
fi
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
# start automatically at login (quietly, no window)
mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.kjdproductions.rxbridge</string>
<key>ProgramArguments</key><array><string>$APP/Contents/MacOS/KJD RX Bridge</string><string>--background</string></array>
<key>RunAtLoad</key><true/>
<key>AbandonProcessGroup</key><true/>
<key>StandardOutPath</key><string>$HOME/Library/Logs/KJD RX Bridge/launch.log</string>
<key>StandardErrorPath</key><string>$HOME/Library/Logs/KJD RX Bridge/launch.log</string>
</dict></plist>
PLIST
mkdir -p "$HOME/Library/Logs/KJD RX Bridge"
launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>/dev/null || launchctl load "$AGENT" 2>/dev/null || true
echo "Installed: $APP  (starts automatically when you log in)"
sleep 2; open "$APP"
