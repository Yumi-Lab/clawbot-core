#!/usr/bin/env bash
# install.sh — runs once during module installation
# Working directory is the module repo root
set -e

# Install dependencies
# apt-get install -y your-deps
# pip3 install your-pip-deps

# Copy service file
cp clawbot-my-module.service /etc/systemd/system/
systemctl daemon-reload

echo "my-module installed successfully"
