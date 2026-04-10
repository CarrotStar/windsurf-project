#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Grid Bot Environment Setup ---"

# Ensure the correct python3-venv package is installed for creating virtual environments
echo "Updating package list..."
sudo apt-get update

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Detected Python version: $PY_VERSION. Ensuring python${PY_VERSION}-venv is installed..."
sudo apt-get install -y "python${PY_VERSION}-venv"

# Create a Python virtual environment named 'venv' if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate the virtual environment
source venv/bin/activate

echo "Installing Python dependencies from requirements.txt..."
pip install -r requirements.txt

echo ""
echo "✅ Setup complete!"
echo "To run the bot, first activate the environment with: source venv/bin/activate"
echo "Then run the bot with: python3 main.py"