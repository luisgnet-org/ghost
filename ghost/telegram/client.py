"""
TelegramClient - Main API for Telegram bot interactions.

Provides:
- Unified sending API (messages, documents, photos)
- Event waiting with filters
- Topic management with caching
- Integrated watcher lifecycle
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Union, List, Dict, Any

from telegram import Bot, BotCommand, BotCommandScopeChat, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode

from .store import EventStore
from ._watcher import run_watcher
from .wait import wait_for_event

logger = logging.getLogger(__name__)


class TelegramClient:
    """
    Async Telegram client with persistent event storage.

    Usage:
        client = TelegramClient(bot_token, chat_id, db_path)
        await client.start()
        try:
            await client.send_message("Hello!", topic="general")
            event = await client.wait_for_reply(msg_id, timeout=30)
        finally:
            await client.stop()
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        db_path: Union[str, Path],
        poll_interval: float = 1.0,
    ):
        """
        Initialize TelegramClient.

        Args:
            bot_token: Telegram bot token
            chat_id: Chat/group ID to monitor
            db_path: Path to SQLite database
            poll_interval: Seconds between update polls
        """
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.db_path = Path(db_path)
        self.poll_interval = poll_interval

        self.store = EventStore(db_path)
        self._watcher_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self):
        """Start the client (connect DB, start watcher)."""
        await self.store.connect()
        logger.info(f"Connected to database: {self.db_path}")

        # Start watcher coroutine
        self._stop_event.clear()
        self._watcher_task = asyncio.create_task(
            run_watcher(
                self.bot,
                self.store,
                self.chat_id,
                self.poll_interval,
                self._stop_event,
            )
        )
        logger.info("Telegram watcher started")

    async def stop(self):
        """Stop the client (stop watcher, close DB)."""
        if self._watcher_task:
            self._stop_event.set()
            await self._watcher_task
            self._watcher_task = None

        await self.store.close()
        logger.info("TelegramClient stopped")

    # === File Download ===

    async def download_file(self, file_id: str, dest_path: Path) -> Path:
        """Download a Telegram file by file_id to local path."""
        tg_file = await self.bot.get_file(file_id)
        await tg_file.download_to_drive(dest_path)
        return dest_path

    # === Sending Methods ===

    async def send_message(
        self,
        text: str,
        topic: Optional[Union[int, str]] = None,
        reply_to: Optional[int] = None,
        keyboard: Optional[List[List[tuple]]] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN,
        silent: bool = False,
    ) -> int:
        """
        Send a text message.

        Args:
            text: Message text
            topic: Topic ID (int) or name (str)
            reply_to: Message ID to reply to
            keyboard: List of button rows, each row is [(label, data), ...]
            parse_mode: "Markdown" or "HTML" or None
            silent: Disable notification

        Returns:
            message_id of sent message

        Example:
            msg_id = await client.send_message(
                "Choose an option:",
                topic="support",
                keyboard=[[("Yes", "yes"), ("No", "no")]]
            )
        """
        topic_id = await self.resolve_topic(topic) if topic else None

        # Build inline keyboard if provided
        reply_markup = None
        if keyboard:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in keyboard
            ])

        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            message_thread_id=topic_id,
            reply_to_message_id=reply_to,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=silent,
        )
        return message.message_id

    async def send_document(
        self,
        document: Union[str, Path, bytes],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        topic: Optional[Union[int, str]] = None,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN,
    ) -> int:
        """
        Send a document/file.

        Args:
            document: File path, Path object, or bytes
            filename: Filename to display (required if document is bytes)
            caption: Caption text
            topic: Topic ID (int) or name (str)
            reply_to: Message ID to reply to
            parse_mode: "Markdown" or "HTML" or None

        Returns:
            message_id of sent message
        """
        topic_id = await self.resolve_topic(topic) if topic else None

        # Handle Path objects
        if isinstance(document, Path):
            document = str(document)

        message = await self.bot.send_document(
            chat_id=self.chat_id,
            document=document,
            filename=filename,
            caption=caption,
            message_thread_id=topic_id,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        return message.message_id

    async def send_photo(
        self,
        photo: Union[str, Path, bytes],
        caption: Optional[str] = None,
        topic: Optional[Union[int, str]] = None,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN,
    ) -> int:
        """
        Send a photo.

        Args:
            photo: File path, Path object, or bytes
            caption: Caption text
            topic: Topic ID (int) or name (str)
            reply_to: Message ID to reply to
            parse_mode: "Markdown" or "HTML" or None

        Returns:
            message_id of sent message
        """
        topic_id = await self.resolve_topic(topic) if topic else None

        # Handle Path objects
        if isinstance(photo, Path):
            photo = str(photo)

        message = await self.bot.send_photo(
            chat_id=self.chat_id,
            photo=photo,
            caption=caption,
            message_thread_id=topic_id,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        return message.message_id

    async def edit_message(
        self,
        message_id: int,
        text: str,
        keyboard: Optional[List[List[tuple]]] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN,
    ):
        """
        Edit a message's text and/or keyboard.

        Args:
            message_id: Message to edit
            text: New text
            keyboard: New keyboard (or None to remove)
            parse_mode: "Markdown" or "HTML" or None
        """
        reply_markup = None
        if keyboard:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in keyboard
            ])

        await self.bot.edit_message_text(
            chat_id=self.chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def delete_message(self, message_id: int):
        """Delete a message."""
        await self.bot.delete_message(
            chat_id=self.chat_id,
            message_id=message_id,
        )

    async def answer_callback(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ):
        """
        Answer a callback query (acknowledge button press).

        Args:
            callback_query_id: Query ID from callback event
            text: Optional text to show user
            show_alert: Show as alert popup (vs toast)
        """
        await self.bot.answer_callback_query(
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert,
        )

    async def set_reaction(self, message_id: int, emoji: str):
        """
        Set a reaction on a message.

        Args:
            message_id: Message to react to
            emoji: Emoji reaction (e.g., "👍", "❤️")
        """
        from telegram import ReactionTypeEmoji

        await self.bot.set_message_reaction(
            chat_id=self.chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )

    # === Waiting Methods ===

    async def wait_for_event(
        self,
        event_type: Optional[str] = None,
        message_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        in_thread: Optional[int] = None,
        in_topic: Optional[Union[int, str]] = None,
        callback_data: Optional[str] = None,
        since_update_id: Optional[int] = None,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> Optional[Dict[str, Any]]:
        """
        Wait for an event matching filters.

        Args:
            event_type: "message" | "callback_query" | "reaction"
            message_id: Filter by message_id (for callbacks, the message the button was on)
            reply_to: Direct reply to message_id
            in_thread: Any message in reply chain
            in_topic: Topic ID (int) or name (str)
            callback_data: Callback pattern (supports wildcards)
            since_update_id: Cursor for pagination
            timeout: Max seconds to wait
            poll_interval: Seconds between polls

        Returns:
            Event dict if found, None if timeout
        """
        # Resolve topic name to ID
        topic_id = None
        if in_topic is not None:
            topic_id = await self.resolve_topic(in_topic)

        result = await wait_for_event(
            self.store,
            event_type=event_type,
            message_id=message_id,
            reply_to=reply_to,
            in_thread=in_thread,
            in_topic=topic_id,
            callback_data=callback_data,
            since_update_id=since_update_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )

        if result:
            event, cursor = result
            return event
        return None

    async def wait_for_callback(
        self,
        message_id: int,
        timeout: float = 60.0,
        callback_data: Optional[str] = None,
        since_update_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Convenience wrapper: wait for callback on a specific message.

        Args:
            message_id: Wait for callback on this message
            timeout: Max seconds to wait
            callback_data: Optional pattern filter
            since_update_id: Only return callbacks after this update_id (prevents duplicates)

        Returns:
            Callback event dict if found, None if timeout
        """
        # Query events to find message and get topic_id
        events = await self.store.query_events(message_id=message_id, limit=1)
        topic_id = events[0].get("topic_id") if events else None

        return await self.wait_for_event(
            event_type="callback_query",
            message_id=message_id,  # Filter by the specific message!
            in_topic=topic_id,
            callback_data=callback_data,
            since_update_id=since_update_id,  # Cursor to prevent duplicates!
            timeout=timeout,
        )

    async def wait_for_reply(
        self,
        message_id: int,
        timeout: float = 60.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Convenience wrapper: wait for direct reply to a message.

        Args:
            message_id: Wait for reply to this message
            timeout: Max seconds to wait

        Returns:
            Message event dict if found, None if timeout
        """
        return await self.wait_for_event(
            event_type="message",
            reply_to=message_id,
            timeout=timeout,
        )

    # === Topic Methods ===

    async def get_or_create_topic(self, name: str) -> int:
        """
        Get or create a forum topic.

        Args:
            name: Topic name

        Returns:
            topic_id (cached in DB)
        """
        # Check cache first
        topic_id = await self.store.get_topic_id(name)
        if topic_id:
            return topic_id

        # Create new topic
        topic = await self.bot.create_forum_topic(
            chat_id=self.chat_id,
            name=name,
        )
        topic_id = topic.message_thread_id

        # Save to cache
        await self.store.save_topic(name, topic_id)
        logger.info(f"Created topic '{name}' (id={topic_id})")

        return topic_id

    async def resolve_topic(self, topic: Union[int, str]) -> int:
        """
        Resolve topic to ID.

        Args:
            topic: Topic ID (int) or name (str)

        Returns:
            topic_id

        Raises:
            ValueError: If topic name not found
        """
        if isinstance(topic, int):
            return topic

        # General is Telegram's built-in default topic (message_thread_id=1)
        if topic.lower() == "general":
            return 1

        # Lookup by name
        topic_id = await self.store.get_topic_id(topic)
        if topic_id:
            return topic_id

        # Not in cache, try to create
        return await self.get_or_create_topic(topic)

    # === Command Registration ===

    # Static commands always registered alongside dynamic trigger commands.
    # Override in subclass or config to add custom commands.
    STATIC_COMMANDS = [
        BotCommand("help", "Show help"),
        BotCommand("kill_session", "Kill active agent session"),
    ]

    async def register_commands(self, workflow_names: list[str]):
        """Register static + /trigger_<name> commands for Telegram autocomplete."""
        commands = list(self.STATIC_COMMANDS) + [
            BotCommand(f"trigger_{name}", f"Trigger {name}")
            for name in sorted(workflow_names)
        ]
        scope = BotCommandScopeChat(chat_id=self.chat_id)
        await self.bot.set_my_commands(commands, scope=scope)
        logger.info(f"Registered {len(commands)} bot commands (scoped to chat {self.chat_id})")

    # === Maintenance Methods ===

    async def prune_old_events(self, max_age_days: int = 7) -> int:
        """
        Delete events older than max_age_days.

        Args:
            max_age_days: Delete events older than this

        Returns:
            Number of events deleted
        """
        return await self.store.prune_old_events(max_age_days)

    async def prune_stale_topics(self, max_age_days: int = 30) -> int:
        """
        Delete topics not used in max_age_days.

        Args:
            max_age_days: Delete topics not used in this many days

        Returns:
            Number of topics deleted
        """
        return await self.store.prune_stale_topics(max_age_days)
