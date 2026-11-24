#!/usr/bin/env bash
# Installs the desktop part of Uber Watch for the current user (no sudo): the watchdog, its service and the tray icon.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.config/systemd/user ~/.config/autostart ~/.local/share/uber-watch
install -m 755 "$here/uber-watch" ~/.local/bin/uber-watch
install -m 644 "$here/strata-watchdog.service" ~/.config/systemd/user/strata-watchdog.service
install -m 755 "$here/uber-watch-tray.py" ~/.local/share/uber-watch/uber-watch-tray.py
cat > ~/.config/autostart/uber-watch-tray.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Uber Watch
Exec=python3 $HOME/.local/share/uber-watch/uber-watch-tray.py
DESK
systemctl --user daemon-reload
echo "Installed. Next: create the settings drop-in described in the README (preset, model name, address), then:"
echo "  systemctl --user enable --now strata-watchdog"
