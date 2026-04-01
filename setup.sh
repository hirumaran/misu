#!/usr/bin/env bash
set -euo pipefail

# M.I.S.U. — Setup Script (idempotent)
# Installs all required dependencies via brew and pip.

echo ""
echo "  M.I.S.U. — Dependency Installer"
echo "  ================================"
echo ""

# Check for Homebrew
if ! command -v brew &>/dev/null; then
    echo "  [!] Homebrew not found. Install it first:"
    echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
fi

echo "  [*] Installing/upgrading brew packages: yt-dlp, ffmpeg ..."
brew install yt-dlp ffmpeg 2>/dev/null || brew upgrade yt-dlp ffmpeg 2>/dev/null || true

echo "  [*] Installing Python packages: sounddevice, numpy ..."
pip3 install --quiet --upgrade sounddevice numpy

echo ""
echo "  [✓] All dependencies installed."
echo "  Run M.I.S.U. with:"
echo "      python3 $(dirname "$0")/misu.py"
echo ""
