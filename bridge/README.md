# KJD RX Bridge (Mac)
Lets KJD Edit on the iPad send clips to iZotope RX 12 on this Mac and shows the Mac screen inside KJD Edit.
Standard-library Python only. Double-click **KJD RX Bridge.app**; it starts the bridge, publishes it to your
devices with `tailscale serve`, keeps the Mac awake, and shows the pairing code.
Needs: Tailscale (Mac + iPad, same account, MagicDNS + HTTPS on), Screen Sharing on, RX 12 installed.
Work files: ~/Music/KJD Edit RX/<Project>/ · Log: ~/Library/Logs/KJD RX Bridge/bridge.log
