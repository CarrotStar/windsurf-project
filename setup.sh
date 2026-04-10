#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Grid Bot Environment Setup ---"

# Ensure python3-venv is installed for creating virtual environments
echo "Updating package list and installing python3-venv..."
sudo apt-get update
sudo apt-get install -y python3-venv

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