#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/aquaguard"
CONFIG_DIR="/etc/aquaguard"
DATA_DIR="/var/lib/aquaguard"

echo "=== AquaGuard Installer ==="

# Create directories
echo "Creating directories..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR"

# Copy application
echo "Installing application..."
cp -r aquaguard/ "$INSTALL_DIR/"
cp pyproject.toml "$INSTALL_DIR/"

# Copy config if not exists
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    echo "Installing default config..."
    cp config.yaml "$CONFIG_DIR/config.yaml"
else
    echo "Config already exists, skipping..."
fi

# Create virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -e "$INSTALL_DIR"

# Install systemd service
echo "Installing systemd service..."
cp deploy/aquaguard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable aquaguard.service

echo ""
echo "=== Installation complete ==="
echo ""
echo "Edit config:    nano $CONFIG_DIR/config.yaml"
echo "Start service:  systemctl start aquaguard"
echo "View logs:      journalctl -u aquaguard -f"
echo "Dashboard:      http://$(hostname -I | awk '{print $1}'):8080"
