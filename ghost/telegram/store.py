"""
EventStore - SQLite-backed storage for Telegram events and topics.

Provides:
- Persistent event storage with indexed queries
- Topic caching (name -> topic_id)
- WAL mode for concurrent reads
- Maintenance utilities (pruning)
"""

import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any


class EventStore:
    """Async SQLite store for Telegram events and topics."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Initialize database connection and schema."""
        self.conn = await aiosqlite.connect(self.db_path)

        # Enable WAL mode for concurrent reads
        await self.conn.execute("PRAGMA journal_mode=WAL")

        # Create events table
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                update_id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                message_id INTEGER,
                callback_query_id TEXT,
                user_id INTEGER,
                user_name TEXT,
                text TEXT,
                callback_data TEXT,
                reply_to_message_id INTEGER,
                topic_id INTEGER,
                reaction_emoji TEXT,
                timestamp INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        # Create indexes for common queries
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_message_id
            ON events(message_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_reply_to
            ON events(reply_to_message_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_topic
            ON events(topic_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_callback_data
            ON events(callback_data)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_created_at
            ON events(created_at)
        """)

        # Create topics table
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY,
                topic_name TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                last_used REAL NOT NULL
            )
        """)

        # Safe migration: add media_json column
        try:
            await self.conn.execute("ALTER TABLE events ADD COLUMN media_json TEXT")
        except Exception:
            pass  # column already exists

        await self.conn.commit()

    async def close(self):
        """Close database connection."""
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def insert_event(self, event: Dict[str, Any]):
        """
        Insert a Telegram event into the database.

        Args:
            event: Dict with keys:
                - update_id (required)
                - event_type (required): "message" | "callback_query" | "reaction"
                - message_id (optional)
                - callback_query_id (optional)
                - user_id (optional)
                - user_name (optional)
                - text (optional)
                - callback_data (optional)
                - reply_to_message_id (optional)
                - topic_id (optional)
                - reaction_emoji (optional)
                - timestamp (required): Unix timestamp
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        now = datetime.now().timestamp()

        await self.conn.execute("""
            INSERT OR REPLACE INTO events (
                update_id, event_type, message_id, callback_query_id,
                user_id, user_name, text, callback_data,
                reply_to_message_id, topic_id, reaction_emoji,
                timestamp, created_at, media_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event["update_id"],
            event["event_type"],
            event.get("message_id"),
            event.get("callback_query_id"),
            event.get("user_id"),
            event.get("user_name"),
            event.get("text"),
            event.get("callback_data"),
            event.get("reply_to_message_id"),
            event.get("topic_id"),
            event.get("reaction_emoji"),
            event["timestamp"],
            now,
            event.get("media_json"),
        ))
        await self.conn.commit()

    async def query_events(
        self,
        event_type: Optional[str] = None,
        message_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        callback_data_prefix: Optional[str] = None,
        since_update_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query events with filters.

        Args:
            event_type: Filter by event type
            message_id: Filter by message_id
            reply_to_message_id: Filter by reply_to_message_id
            topic_id: Filter by topic_id
            callback_data_prefix: Filter callback_data starting with prefix
            since_update_id: Only return events after this update_id
            limit: Max results to return

        Returns:
            List of event dicts
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        conditions = []
        params = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        if message_id is not None:
            conditions.append("message_id = ?")
            params.append(message_id)

        if reply_to_message_id is not None:
            conditions.append("reply_to_message_id = ?")
            params.append(reply_to_message_id)

        if topic_id is not None:
            conditions.append("topic_id = ?")
            params.append(topic_id)

        if callback_data_prefix:
            conditions.append("callback_data LIKE ?")
            params.append(f"{callback_data_prefix}%")

        if since_update_id is not None:
            conditions.append("update_id > ?")
            params.append(since_update_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT * FROM events
            WHERE {where_clause}
            ORDER BY update_id ASC
            LIMIT ?
        """
        params.append(limit)

        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_thread_messages(
        self,
        root_message_id: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get all messages in a reply thread (recursive).

        Uses recursive CTE to follow reply_to_message_id chain.

        Args:
            root_message_id: Starting message ID
            limit: Max results

        Returns:
            List of message events in thread
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        query = """
            WITH RECURSIVE thread AS (
                -- Base case: root message
                SELECT * FROM events WHERE message_id = ?
                UNION ALL
                -- Recursive case: replies to thread messages
                SELECT e.*
                FROM events e
                INNER JOIN thread t ON e.reply_to_message_id = t.message_id
            )
            SELECT * FROM thread
            WHERE event_type = 'message'
            ORDER BY update_id ASC
            LIMIT ?
        """

        async with self.conn.execute(query, (root_message_id, limit)) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def save_topic(self, topic_name: str, topic_id: int):
        """
        Save or update a topic mapping.

        Args:
            topic_name: Topic name (unique)
            topic_id: Telegram topic ID
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        now = datetime.now().timestamp()

        await self.conn.execute("""
            INSERT INTO topics (topic_id, topic_name, created_at, last_used)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(topic_name) DO UPDATE SET
                topic_id = excluded.topic_id,
                last_used = excluded.last_used
        """, (topic_id, topic_name, now, now))
        await self.conn.commit()

    async def get_topic_id(self, topic_name: str) -> Optional[int]:
        """
        Get topic_id for a topic name.

        Args:
            topic_name: Topic name to lookup

        Returns:
            topic_id if found, None otherwise
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self.conn.execute(
            "SELECT topic_id FROM topics WHERE topic_name = ?",
            (topic_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                # Update last_used
                now = datetime.now().timestamp()
                await self.conn.execute(
                    "UPDATE topics SET last_used = ? WHERE topic_name = ?",
                    (now, topic_name)
                )
                await self.conn.commit()
                return row[0]
            return None

    async def prune_old_events(self, max_age_days: int = 7):
        """
        Delete events older than max_age_days.

        Args:
            max_age_days: Delete events older than this

        Returns:
            Number of events deleted
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        cutoff = (datetime.now() - timedelta(days=max_age_days)).timestamp()

        cursor = await self.conn.execute(
            "DELETE FROM events WHERE created_at < ?",
            (cutoff,)
        )
        await self.conn.commit()
        return cursor.rowcount

    async def prune_stale_topics(self, max_age_days: int = 30):
        """
        Delete topics not used in max_age_days.

        Args:
            max_age_days: Delete topics not used in this many days

        Returns:
            Number of topics deleted
        """
        if not self.conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        cutoff = (datetime.now() - timedelta(days=max_age_days)).timestamp()

        cursor = await self.conn.execute(
            "DELETE FROM topics WHERE last_used < ?",
            (cutoff,)
        )
        await self.conn.commit()
        return cursor.rowcount
