# KJD RX Bridge (Mac)
Lets KJD Edit on the iPad send clips to iZotope RX 12 on this Mac, shows the Mac screen inside KJD Edit, and runs the Piano Excerpts engine (the Mac app's own code) for the iPad's Piano Excerpts page.
Standard-library Python only. Install with `curl -fsSL https://kjdombroski.github.io/kjd-edit/bridge/install.sh | bash`, then double-click **KJD RX Bridge.app**; it starts the bridge, publishes it to your
devices with `tailscale serve`, keeps the Mac awake, and shows the pairing code.
Needs: Tailscale (Mac + iPad, same account, MagicDNS + HTTPS on), Screen Sharing on, RX 12 installed.
Work files: ~/Music/KJD Edit RX/<Project>/ · Log: ~/Library/Logs/KJD RX Bridge/bridge.log
