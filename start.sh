#!/usr/bin/env bash
set -e

echo "Starting Media Gateway (Flask) on port 8080..."
python3 media_gateway.py &

echo "Starting Giant Chat Bot..."
python3 bot.py
