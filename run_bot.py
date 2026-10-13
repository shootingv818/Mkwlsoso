#!/usr/bin/env python3
"""Entry point for the Telegram control panel.

Usage:
    python run_bot.py

Requires API_ID, API_HASH, BOT_TOKEN, OWNER_ID in .env (see .env.example).
"""

from bot.app import main

if __name__ == "__main__":
    main()
