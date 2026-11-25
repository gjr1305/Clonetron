#!/bin/bash
# Make the Clonetron scanner app start by itself when the Pi boots.
# Run once on the Pi from the scanner folder:   bash install_service.sh
# Undo with:                                    bash install_service.sh --remove
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ME="$(whoami)"
UNIT=/etc/systemd/system/clonetron.service
POLKIT_RULE=/etc/polkit-1/rules.d/50-clonetron-usb.rules
POLKIT_PKLA=/etc/polkit-1/localauthority/50-local.d/50-clonetron-usb.pkla

if [ "$1" = "--remove" ]; then
    sudo systemctl disable --now clonetron.service 2>/dev/null || true
    sudo rm -f "$UNIT" "$POLKIT_RULE" "$POLKIT_PKLA"
    sudo systemctl daemon-reload
    echo "Removed. The app no longer starts at boot."
    echo "(If you want the old IP-only LCD back: sudo systemctl enable --now colonyscope-lcd.service)"
    exit 0
fi

echo "This will:"
echo "  1. install udisks2 (lets the app open and eject USB sticks), if it isn't already"
echo "  2. allow user '$ME' to mount/eject USB sticks and shut down the Pi (LCD menu) without a password"
echo "  3. create $UNIT so the app starts at boot as '$ME' from $APP_DIR"
if systemctl list-unit-files 2>/dev/null | grep -q '^colonyscope-lcd.service'; then
    echo "  4. turn OFF the old colonyscope-lcd.service (the app drives the LCD now;"
    echo "     the old service is kept, just disabled, so you can switch it back on)"
fi
read -r -p "Go ahead? [y/N] " ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "Nothing changed."; exit 0; }

if [ ! -f "$HOME/.clonetron_auth.json" ]; then
    echo; echo "First choose the app password:"
    python3 "$APP_DIR/clonetron_server.py" --set-password
fi

if ! command -v udisksctl >/dev/null; then
    sudo apt-get update && sudo apt-get install -y udisks2
fi

# USB sticks: allow this user to mount/unmount/power off without a password prompt
sudo mkdir -p "$(dirname "$POLKIT_RULE")"
sudo tee "$POLKIT_RULE" >/dev/null <<EOF
// Clonetron: let $ME open and eject USB sticks from the scanner app
polkit.addRule(function(action, subject) {
    if (subject.user == "$ME" &&
        (action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") == 0 ||
         action.id.indexOf("org.freedesktop.udisks2.filesystem-unmount") == 0 ||
         action.id.indexOf("org.freedesktop.udisks2.power-off-drive") == 0 ||
         action.id.indexOf("org.freedesktop.udisks2.eject-media") == 0 ||
         action.id.indexOf("org.freedesktop.login1.power-off") == 0 ||
         action.id.indexOf("org.freedesktop.login1.reboot") == 0)) {
        return polkit.Result.YES;
    }
});
EOF
# older Pi OS versions use this format instead; harmless where it isn't used
sudo mkdir -p "$(dirname "$POLKIT_PKLA")"
sudo tee "$POLKIT_PKLA" >/dev/null <<EOF
[Clonetron USB sticks]
Identity=unix-user:$ME
Action=org.freedesktop.udisks2.filesystem-mount*;org.freedesktop.udisks2.filesystem-unmount*;org.freedesktop.udisks2.power-off-drive*;org.freedesktop.udisks2.eject-media*;org.freedesktop.login1.power-off*;org.freedesktop.login1.reboot*
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Clonetron scanner app
After=network-online.target udisks2.service
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/clonetron_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

if systemctl list-unit-files 2>/dev/null | grep -q '^colonyscope-lcd.service'; then
    sudo systemctl disable --now colonyscope-lcd.service || true
fi
sudo systemctl daemon-reload
sudo systemctl enable --now clonetron.service
sleep 4
echo
systemctl --no-pager --lines=8 status clonetron.service || true
echo
echo "Done. Clonetron now starts at every boot. Open  http://$(hostname -I | awk '{print $1}'):8000"
echo "Useful commands:"
echo "  sudo systemctl restart clonetron     restart it (e.g. after git pull)"
echo "  sudo systemctl stop clonetron        stop it (e.g. to run a script by hand)"
echo "  journalctl -u clonetron -f           watch what it prints"
