"""
Event filtering and waiting utilities.

Provides SQL-based filtering with wildcard support for callback patterns.
"""

import asyncio
import fnmatch
from typing import Dict, Any, Optional, Tuple
from .store import EventStore


async def wait_for_event(
    store: EventStore,
    event_type: Optional[str] = None,
    message_id: Optional[int] = None,
    reply_to: Optional[int] = None,
    in_thread: Optional[int] = None,
    in_topic: Optional[int] = None,
    callback_data: Optional[str] = None,
    since_update_id: Optional[int] = None,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    Wait for a Telegram event matching filters.

    Args:
        store: EventStore to query
        event_type: Filter by event type ("message", "callback_query", "reaction")
        message_id: Filter by message_id (for callbacks, this is the message the button was on)
        reply_to: Direct reply to message_id
        in_thread: Any message in reply chain starting from message_id
        in_topic: Filter by topic_id
        callback_data: Filter callback_data (supports wildcards like "approve:*")
        since_update_id: Only check events after this update_id (cursor)
        timeout: Max seconds to wait
        poll_interval: Seconds between polls

    Returns:
        (event_dict, cursor) if found within timeout
        None if timeout reached

    Examples:
        # Wait for any reply to message 123
        event, cursor = await wait_for_event(store, reply_to=123, timeout=30)

        # Wait for callback with pattern
        event, cursor = await wait_for_event(
            store,
            event_type="callback_query",
            callback_data="approve:*",
            timeout=60
        )

        # Wait for message in topic
        event, cursor = await wait_for_event(
            store,
            event_type="message",
            in_topic=456,
            timeout=120
        )
    """
    start_time = asyncio.get_event_loop().time()
    cursor = since_update_id or 0

    while True:
        # Check if timeout reached
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= timeout:
            return None

        # Handle in_thread filter (uses recursive CTE)
        if in_thread is not None:
            events = await store.get_thread_messages(in_thread, limit=100)
            # Filter by cursor
            events = [e for e in events if e["update_id"] > cursor]
        else:
            # Standard query with SQL filters
            callback_prefix = None
            if callback_data and "*" not in callback_data:
                # Exact match, use SQL
                callback_prefix = callback_data
            elif callback_data and callback_data.endswith("*"):
                # Prefix match, use SQL LIKE
                callback_prefix = callback_data[:-1]

            events = await store.query_events(
                event_type=event_type,
                message_id=message_id,
                reply_to_message_id=reply_to,
                topic_id=in_topic,
                callback_data_prefix=callback_prefix if callback_prefix else None,
                since_update_id=cursor,
                limit=100,
            )

        # Apply wildcard filtering in Python (for complex patterns)
        if callback_data and "*" in callback_data:
            events = [
                e for e in events
                if e.get("callback_data") and fnmatch.fnmatch(e["callback_data"], callback_data)
            ]

        # Return first match
        if events:
            event = events[0]
            new_cursor = event["update_id"]
            return (event, new_cursor)

        # Update cursor for next iteration
        if events:
            cursor = max(e["update_id"] for e in events)

        # Wait before next poll
        await asyncio.sleep(poll_interval)
