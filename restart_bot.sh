#!/bin/bash
set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$BOT_DIR/bot.log"

pkill -f "python.*bot.py" || true

cd "$BOT_DIR"
source ~/tgvenv/bin/activate
nohup python bot.py > "$LOG_FILE" 2>&1 &
echo "Bot restarted."

