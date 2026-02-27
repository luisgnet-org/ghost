"""
Stateful menu utilities for interactive workflows.

Provides:
- run_stateful_menu: State machine pattern for interactive UIs
- Helper functions for button creation
"""

from typing import Callable, Dict, Any, List, Tuple, Optional


async def run_stateful_menu(
    client,
    initial_state: Dict[str, Any],
    render: Callable[[Dict[str, Any]], str],
    update: Callable[[Dict[str, Any], str], Dict[str, Any]],
    buttons: List[Tuple[str, str]],
    topic: Optional[str] = None,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """
    Run an interactive stateful menu.

    Args:
        client: TelegramClient instance
        initial_state: Initial state dict
        render: Function (state) -> message_text
        update: Function (state, button_data) -> new_state
        buttons: List of (label, callback_data) tuples
        topic: Optional topic name/id
        timeout: Max seconds to wait for interactions

    Returns:
        Final state dict

    Example:
        final = await run_stateful_menu(
            client,
            initial_state={"count": 0, "done": False},
            render=lambda s: f"Count: {s['count']}",
            update=lambda s, btn: {
                "count": s["count"] + 1 if btn == "inc" else s["count"],
                "done": btn == "done"
            },
            buttons=[("Increment", "inc"), ("Done", "done")],
        )
    """
    state = initial_state.copy()

    # Send initial message
    text = render(state)
    keyboard = [buttons]  # Single row for simplicity
    msg_id = await client.send_message(text, topic=topic, keyboard=keyboard)

    # Track cursor to prevent processing duplicate events
    cursor = 0

    while True:
        # Wait for button press (only events after cursor)
        event = await client.wait_for_callback(msg_id, timeout=timeout, since_update_id=cursor)
        if not event:
            # Timeout reached
            await client.edit_message(msg_id, render(state) + "\n\n⏱ Timeout", keyboard=None)
            return state

        callback_data = event["callback_data"]
        callback_query_id = event["callback_query_id"]

        # Update cursor to prevent re-processing this event
        cursor = event["update_id"]

        # Acknowledge callback
        await client.answer_callback(callback_query_id)

        # Update state
        new_state = update(state, callback_data)
        state = new_state

        # Re-render
        new_text = render(state)

        # Check if done (caller's responsibility to signal via state)
        # For simplicity, we'll let caller decide when to stop by returning state
        # In practice, this would need a termination condition
        # Let's use a convention: if state has "done": True, stop
        if state.get("done"):
            await client.edit_message(msg_id, new_text, keyboard=None)
            return state

        # Update message with new state
        await client.edit_message(msg_id, new_text, keyboard=keyboard)


def button(label: str, callback_data: str) -> Tuple[str, str]:
    """
    Helper to create a button tuple.

    Args:
        label: Button text
        callback_data: Callback data

    Returns:
        (label, callback_data) tuple
    """
    return (label, callback_data)


def button_row(*buttons: Tuple[str, str]) -> List[Tuple[str, str]]:
    """
    Helper to create a button row.

    Args:
        *buttons: Variable number of (label, data) tuples

    Returns:
        List of button tuples (for a single row)
    """
    return list(buttons)
