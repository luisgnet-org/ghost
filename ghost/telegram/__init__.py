"""
ghost.telegram - SQLite-backed Telegram client with event storage.

Public API:
    - TelegramClient: Main client class
    - run_stateful_menu: Interactive menu helper
    - button, button_row: Button creation helpers

Usage:
    from ghost.telegram import TelegramClient

    client = TelegramClient(bot_token, chat_id, db_path="/path/to/telegram.db")
    await client.start()
    try:
        msg_id = await client.send_message("Hello!", topic="general")
        reply = await client.wait_for_reply(msg_id, timeout=30)
    finally:
        await client.stop()
"""

from .client import TelegramClient
from .menus import run_stateful_menu, button, button_row

__all__ = [
    "TelegramClient",
    "run_stateful_menu",
    "button",
    "button_row",
]

__version__ = "1.0.0"
