#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Grid Bot Environment Setup ---"

# Ensure the correct python3-venv package is installed for creating virtual environments
echo "Updating package list and installing system dependencies (venv, tmux)..."
sudo apt-get update

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Detected Python version: $PY_VERSION."
sudo apt-get install -y "python${PY_VERSION}-venv" tmux

# If the virtual environment is not set up correctly, (re)create it.
if [ ! -f "venv/bin/activate" ]; then
    echo "Virtual environment is incomplete or missing. Recreating..."
    # Remove potentially broken venv directory before recreating
    rm -rf venv
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Verify that the virtual environment was created successfully
if [ ! -f "venv/bin/activate" ]; then
    echo "❌ ERROR: Virtual environment creation failed even after retry. Please check Python installation and permissions."
    exit 1
fi

# Activate the virtual environment
source venv/bin/activate

echo "Installing Python dependencies from requirements.txt..."
pip install -r requirements.txt

echo ""
echo "✅ Setup complete!"
echo "To run the bot, first activate the environment with: source venv/bin/activate"
echo "Then, for persistent execution, run inside a tmux session:"
echo "  1. tmux new -s gridbot"
echo "  2. python3 main.py (then press Ctrl+b, d to detach)"