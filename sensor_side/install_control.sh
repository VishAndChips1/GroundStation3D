#!/bin/bash
# Installs the always-on control daemon as a systemd --user unit, creates a
# control token if there is none, and starts it. Safe to re-run: an existing
# token is kept. No sudo needed (the optional shutdown rule is separate).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_DIR="$HOME/.config/pitfusion3d"
TOKEN_FILE="$TOKEN_DIR/control_token"

mkdir -p "$HOME/.config/systemd/user" "$TOKEN_DIR"
chmod 700 "$TOKEN_DIR"

if [ ! -s "$TOKEN_FILE" ]; then
    head -c 24 /dev/urandom | base64 | tr -d '=+/\n' > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "Created a new control token."
fi

UNIT_SRC="$SCRIPT_DIR/pitfusion3d-control.service"
[ -f "$UNIT_SRC" ] || UNIT_SRC="$SCRIPT_DIR/../systemd/pitfusion3d-control.service"

sed "s#@SCRIPT@#$SCRIPT_DIR/pitfusion_control.py#" "$UNIT_SRC" \
    > "$HOME/.config/systemd/user/pitfusion3d-control.service"

loginctl enable-linger "$(whoami)"
systemctl --user daemon-reload
systemctl --user enable --now pitfusion3d-control.service

echo
echo "Control daemon running on port 8081."
echo
echo "Paste this token into the base station (Rover tab > Sensor PC power):"
echo
echo "    $(cat "$TOKEN_FILE")"
echo
echo "Optional - allow the 'shut down Pi' command (run once, as root):"
echo "    echo '$(whoami) ALL=(root) NOPASSWD: $(command -v systemctl) poweroff' | sudo tee /etc/sudoers.d/pitfusion3d-poweroff"
echo
echo "Logs: journalctl --user -u pitfusion3d-control.service -f"
