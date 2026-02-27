"""
Internal watcher coroutine for polling Telegram updates.

This module is internal (_watcher.py) and not part of the public API.
TelegramClient owns the watcher lifecycle.
"""

import asyncio
import json
import logging
from typing import Dict, Any, Optional
from telegram import Bot
from telegram.error import TelegramError

from .store import EventStore

logger = logging.getLogger(__name__)


async def run_watcher(
    bot: Bot,
    store: EventStore,
    chat_id: int,
    poll_interval: float = 1.0,
    stop_event: Optional[asyncio.Event] = None,
):
    """
    Poll Telegram API for updates and store events in database.

    Args:
        bot: Telegram Bot instance
        store: EventStore for persisting events
        chat_id: Only process updates from this chat
        poll_interval: Seconds between polls
        stop_event: Event to signal graceful shutdown

    Notes:
        - Uses long polling (getUpdates with timeout)
        - Filters by chat_id early to reduce noise
        - Handles message, callback_query, message_reaction updates
        - Graceful shutdown via stop_event
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    offset = 0
    logger.info(f"Starting Telegram watcher (chat_id={chat_id}, poll_interval={poll_interval}s)")

    while not stop_event.is_set():
        updates = []
        try:
            # Long polling with timeout
            updates = await bot.get_updates(
                offset=offset,
                timeout=int(poll_interval),
                allowed_updates=["message", "callback_query", "message_reaction"],
            )

            if updates:
                logger.info(f"Watcher received {len(updates)} update(s)")
            for update in updates:
                # Parse update into event dict
                event = _parse_update(update, chat_id)
                if event:
                    await store.insert_event(event)
                    logger.info(f"Stored event: {event['event_type']} uid={event['update_id']} topic={event.get('topic_id')} text={event.get('text','')[:40]}")
                else:
                    logger.info(f"Watcher: skipped update {update.update_id} (chat_id mismatch or unparseable)")

                # Update offset to ack this update
                offset = update.update_id + 1

        except TelegramError as e:
            logger.warning(f"Telegram API error: {e}")
            await asyncio.sleep(poll_interval * 2)  # Back off on errors

        except Exception as e:
            logger.error(f"Unexpected error in watcher: {e}", exc_info=True)
            await asyncio.sleep(poll_interval * 2)

        # Small delay between polls
        if not updates:
            await asyncio.sleep(poll_interval)

    logger.info("Watcher stopped")


def _extract_media(msg) -> Optional[Dict[str, Any]]:
    """Extract media metadata from a Telegram message."""
    if msg.photo:
        p = msg.photo[-1]  # highest resolution
        return {"type": "photo", "file_id": p.file_id, "file_unique_id": p.file_unique_id,
                "mime_type": "image/jpeg", "file_size": p.file_size}
    if msg.document:
        d = msg.document
        return {"type": "document", "file_id": d.file_id, "file_unique_id": d.file_unique_id,
                "mime_type": d.mime_type, "file_size": d.file_size, "file_name": d.file_name}
    if msg.voice:
        v = msg.voice
        return {"type": "voice", "file_id": v.file_id, "file_unique_id": v.file_unique_id,
                "mime_type": v.mime_type, "file_size": v.file_size, "duration": v.duration}
    if msg.audio:
        a = msg.audio
        return {"type": "audio", "file_id": a.file_id, "file_unique_id": a.file_unique_id,
                "mime_type": a.mime_type, "file_size": a.file_size, "duration": a.duration}
    if msg.video_note:
        vn = msg.video_note
        return {"type": "video_note", "file_id": vn.file_id, "file_unique_id": vn.file_unique_id,
                "file_size": vn.file_size, "duration": vn.duration}
    return None


def _parse_update(update, chat_id: int) -> Optional[Dict[str, Any]]:
    """
    Parse a Telegram Update into an event dict.

    Args:
        update: telegram.Update object
        chat_id: Only process updates from this chat

    Returns:
        Event dict if relevant, None otherwise

    Event dict structure:
        {
            "update_id": int,
            "event_type": "message" | "callback_query" | "reaction",
            "message_id": int (optional),
            "callback_query_id": str (optional),
            "user_id": int (optional),
            "user_name": str (optional),
            "text": str (optional),
            "callback_data": str (optional),
            "reply_to_message_id": int (optional),
            "topic_id": int (optional),
            "reaction_emoji": str (optional),
            "timestamp": int (Unix timestamp),
        }
    """
    # Message event
    if update.message:
        msg = update.message
        if msg.chat_id != chat_id:
            return None

        media = _extract_media(msg)
        return {
            "update_id": update.update_id,
            "event_type": "message",
            "message_id": msg.message_id,
            "user_id": msg.from_user.id if msg.from_user else None,
            "user_name": msg.from_user.username if msg.from_user else None,
            "text": msg.text or msg.caption or "",
            "reply_to_message_id": msg.reply_to_message.message_id if msg.reply_to_message else None,
            "topic_id": msg.message_thread_id,
            "timestamp": int(msg.date.timestamp()),
            "media_json": json.dumps(media) if media else None,
        }

    # Callback query event (button press)
    if update.callback_query:
        query = update.callback_query
        if query.message and query.message.chat_id != chat_id:
            return None

        return {
            "update_id": update.update_id,
            "event_type": "callback_query",
            "callback_query_id": query.id,
            "message_id": query.message.message_id if query.message else None,
            "user_id": query.from_user.id,
            "user_name": query.from_user.username,
            "callback_data": query.data,
            "topic_id": query.message.message_thread_id if query.message else None,
            "timestamp": int(query.message.date.timestamp()) if query.message else 0,
        }

    # Message reaction event
    if update.message_reaction:
        reaction = update.message_reaction
        if reaction.chat.id != chat_id:
            return None

        # Get emoji from new_reaction (could be multiple, take first)
        emoji = None
        if reaction.new_reaction:
            emoji = reaction.new_reaction[0].emoji if reaction.new_reaction else None

        return {
            "update_id": update.update_id,
            "event_type": "reaction",
            "message_id": reaction.message_id,
            "user_id": reaction.user.id if reaction.user else None,
            "user_name": reaction.user.username if reaction.user else None,
            "reaction_emoji": emoji,
            "timestamp": int(reaction.date.timestamp()),
        }

    return None
