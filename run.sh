#!/bin/bash

# This script simplifies running the bot.
# It activates the Python virtual environment and either attaches to an
# existing 'gridbot' tmux session or creates a new one if it doesn't exist.

set -e

# Activate virtual environment
source venv/bin/activate

if tmux has-session -t gridbot 2>/dev/null; then
  echo "Attaching to existing 'gridbot' tmux session..."
  tmux attach -t gridbot
else
  echo "Creating new 'gridbot' tmux session."
  tmux new -s gridbot
fi