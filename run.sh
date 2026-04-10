#!/bin/bash

# This script simplifies running the bot.
# It activates the Python virtual environment and either attaches to an
# existing 'gridbot' tmux session or creates a new one if it doesn't exist.

set -e

ๅ# Check if already inside a tmux session to prevent nesting.
if [ -n "$TMUX" ]; then
  echo "❌ ERROR: You are already inside a tmux session."
  echo "Please exit this session first (type 'exit' or press Ctrl+b, d) before running this script."
  exit 1
fi

# Activate virtual environment
source venv/bin/activate

if tmux has-session -t gridbot 2>/dev/null; then
  echo "Attaching to existing 'gridbot' tmux session..."
  tmux attach -t gridbot
else
  echo "No existing session found. Creating new 'gridbot' session and starting the bot..."
  # สร้าง session ใหม่ในเบื้องหลัง (-d), ตั้งชื่อว่า 'gridbot' (-s),
  # และรันบอทข้างใน จากนั้นจึง attach เข้าไปที่ session นั้น
  tmux new-session -d -s gridbot "python3 main.py"
  tmux attach -t gridbot
fi