"""
Agent MCP + REST server.

Daemon-lifetime MCP server on a fixed port. Hosts both:
- MCP tools via streamable HTTP at /mcp (agent sessions connect here)
- REST API routes at /api/* (session launchers, external tools)

The session launcher communicates with the daemon through the REST API.
Agent sessions (e.g. Claude Code) connect via MCP streamable HTTP.
Streamable HTTP is stateless per-request, so daemon restarts don't
break the MCP connection (especially with mcp_proxy in front).

Runs uvicorn in a dedicated thread with its own event loop to avoid
contention with the daemon's main event loop.

Plugin tools: workflows can register additional MCP tools by providing
a `register_mcp_tools(mcp, run_on_daemon, get_topic)` function.
The daemon calls these during server initialization.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ghost.telegram.markdown_v2 import escape as _escape_markdown_v2

logger = logging.getLogger("ghost")

MCP_PORT = int(os.environ.get("MCP_BACKEND_PORT", "7866"))  # clients connect to proxy on MCP_PROXY_PORT

# Egress filter — blocks leaked secrets in outbound messages
BLOCKLIST_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),               # Anthropic/OpenAI API keys
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                 # GitHub tokens
    re.compile(r"xoxb-[a-zA-Z0-9-]+"),                  # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]+-----"),              # PEM/SSH keys
    re.compile(r"/Users/\w+/\.\w+"),                     # Hidden dirs in home
    re.compile(r"TELEGRAM_BOT_TOKEN\s*="),               # Env var assignments
    re.compile(r"LLM_API_KEY\s*="),
    re.compile(r"ANTHROPIC_API_KEY\s*="),
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE), # Password assignments
    re.compile(r"Bearer [a-zA-Z0-9._-]{20,}"),           # Bearer tokens
]


def _check_egress(text: str) -> str | None:
    """Check text against egress blocklist. Returns match description or None."""
    for pattern in BLOCKLIST_PATTERNS:
        if pattern.search(text):
            return f"Egress filter blocked: matched pattern '{pattern.pattern}'"
    return None


def _sanitize_telegram_markdown(text: str) -> str:
    """Escape text for Telegram MarkdownV2.

    Delegates to ghost.telegram.markdown_v2.escape(), which handles:
    - All MarkdownV2 special characters escaped in plain text
    - Inline backtick code spans (content preserved, not escaped)
    - Triple-backtick code blocks (content preserved, not escaped)
    """
    return _escape_markdown_v2(text)


class AgentMCPServer:
    """FastMCP-based MCP server for agent Telegram tools + REST API.

    Daemon-lifetime server on a fixed port. Hosts:
    - MCP streamable HTTP at /mcp (agent sessions connect here)
    - REST API at /api/* (session launcher, external tools)

    Personality plugins and data pipeline workflows can register
    additional tools via register_plugin_tools().
    """

    def __init__(self, tg_client, port: int = MCP_PORT, name: str = "ghost-agent"):
        self.tg_client = tg_client
        self.topic_id: int | None = None
        self._topic_ids: dict[str, int] = {}  # {topic_name: topic_id}
        self._active_topic: str | None = None  # currently active topic name
        self.port = port
        self._mcp = FastMCP(name=name)
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._uvicorn_server: uvicorn.Server | None = None
        # Capture the daemon's event loop for cross-thread async calls
        self._daemon_loop = asyncio.get_running_loop()
        # Generation counter for wait_for_message — incremented on each new
        # call so older concurrent calls can detect they've been superseded.
        self._wfm_generation = 0
        # Inbox path — set by the agent workflow (e.g. claw sets this to its inbox dir)
        self._inbox_path: Path | None = None
        # Plugin tool registration functions (called during _register_tools)
        self._plugin_registrations: list = []
        self._register_tools(tg_client)

    def set_topic_id(self, topic_id: int):
        """Set the topic ID lazily (daemon calls this once topic is resolved).
        Kept for backward compatibility."""
        self.topic_id = topic_id

    def set_topic_ids(self, topic_ids: dict[str, int]):
        """Set the full topic_name → topic_id map. Called by agent workflow."""
        self._topic_ids = dict(topic_ids)
        if topic_ids:
            self.topic_id = next(iter(topic_ids.values()))

    def set_inbox_path(self, path: Path):
        """Set the inbox directory path. Called by agent workflow."""
        self._inbox_path = path

    def register_plugin_tools(self, register_fn):
        """Register a plugin's tool registration function.

        The function will be called with (mcp, run_on_daemon, get_topic)
        and should use @mcp.tool() to register its tools.

        Call this BEFORE start(). Plugins registered after start() won't
        have their tools available.
        """
        self._plugin_registrations.append(register_fn)

    def _get_topic_id(self, topic_name: str | None = None) -> int:
        """Get topic ID by name. Raises if ambiguous."""
        if topic_name and topic_name in self._topic_ids:
            return self._topic_ids[topic_name]
        if topic_name:
            raise ToolError(
                f"Unknown topic '{topic_name}'. "
                f"Subscribed: {', '.join(self._topic_ids.keys())}"
            )
        if self._active_topic and self._active_topic in self._topic_ids:
            return self._topic_ids[self._active_topic]
        if len(self._topic_ids) > 1:
            raise ToolError(
                "Multiple topics subscribed — specify topic= or call "
                "set_active_topic() first. "
                f"Topics: {', '.join(self._topic_ids.keys())}"
            )
        if self.topic_id is None:
            raise ToolError("Topic ID not yet resolved — daemon still starting")
        return self.topic_id

    def _run_on_daemon_loop(self, coro, timeout=30):
        """Run an async coroutine on the daemon's event loop from the server thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._daemon_loop)
        return future.result(timeout=timeout)

    def _register_tools(self, tg):
        run_on_daemon = self._run_on_daemon_loop
        get_topic = self._get_topic_id
        server_ref = self

        # ------------------------------------------------------------------
        # Telegram tools
        # ------------------------------------------------------------------

        @self._mcp.tool()
        async def send_message(text: str, topic: str = "") -> str:
            """Send a text message to the user via Telegram.

            Args:
                text: Message text to send.
                topic: Optional topic name. Defaults to active topic.
            """
            block = _check_egress(text)
            if block:
                raise ToolError(block)
            topic_id = get_topic(topic or None)
            safe_text = _sanitize_telegram_markdown(text)
            msg_id = run_on_daemon(tg.send_message(
                safe_text, topic=topic_id, parse_mode="MarkdownV2",
            ))
            return f"Message sent (id: {msg_id})"

        @self._mcp.tool()
        async def send_image(image_path: str, caption: str = "", topic: str = "") -> str:
            """Send an image file to the user via Telegram.

            Args:
                image_path: Path to the image file.
                caption: Optional caption.
                topic: Optional topic name. Defaults to active topic.
            """
            if caption:
                block = _check_egress(caption)
                if block:
                    raise ToolError(block)
            topic_id = get_topic(topic or None)
            msg_id = run_on_daemon(
                tg.send_photo(Path(image_path), caption=caption or None, topic=topic_id)
            )
            return f"Image sent (id: {msg_id})"

        @self._mcp.tool()
        async def send_document(document_path: str, caption: str = "", topic: str = "") -> str:
            """Send a document file to the user via Telegram.

            Args:
                document_path: Path to the document.
                caption: Optional caption.
                topic: Optional topic name. Defaults to active topic.
            """
            if caption:
                block = _check_egress(caption)
                if block:
                    raise ToolError(block)
            topic_id = get_topic(topic or None)
            msg_id = run_on_daemon(
                tg.send_document(
                    document=Path(document_path),
                    caption=caption or None,
                    topic=topic_id,
                )
            )
            return f"Document sent (id: {msg_id})"

        @self._mcp.tool()
        async def ask_approval(action: str, details: str, timeout: int = 300, topic: str = "") -> str:
            """Ask the user to approve or deny an action. Blocks until response or timeout.

            Args:
                action: Short description of the action.
                details: Detailed explanation.
                timeout: Seconds to wait for response.
                topic: Optional topic name. Defaults to active topic.
            """
            text = f"*Approval needed:* {action}\n\n{details}"
            block = _check_egress(text)
            if block:
                raise ToolError(block)

            topic_id = get_topic(topic or None)
            msg_id = run_on_daemon(tg.send_message(
                text,
                topic=topic_id,
                keyboard=[[
                    ("Approve", "agent_approve"),
                    ("Deny", "agent_deny"),
                ]],
            ))

            event = run_on_daemon(tg.wait_for_callback(
                message_id=msg_id,
                timeout=timeout,
                callback_data="agent_*",
            ), timeout=timeout + 10)

            if event is None:
                return "TIMEOUT — no response received"

            approved = event.get("callback_data") == "agent_approve"
            status = "APPROVED" if approved else "DENIED"

            cb_id = event.get("callback_query_id")
            if cb_id:
                run_on_daemon(tg.answer_callback(cb_id))

            run_on_daemon(tg.edit_message(
                msg_id,
                text=f"{text}\n\n*{status}*",
            ))

            return status

        @self._mcp.tool()
        async def react_to_message(message_id: int, emoji: str) -> str:
            """React to a Telegram message with an emoji.

            Call list_reaction_emojis() first if you need the full list of
            available emojis — Telegram only supports a specific set.
            """
            run_on_daemon(tg.set_reaction(message_id, emoji))
            return json.dumps({"ok": True, "message_id": message_id, "emoji": emoji})

        @self._mcp.tool()
        async def list_reaction_emojis() -> str:
            """List all emojis available for Telegram message reactions."""
            emojis = [
                "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔",
                "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩",
                "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
                "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆",
                "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
                "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
                "😇", "😨", "🤝", "✍️", "🤗", "🫡", "🎅", "🎄",
                "☃️", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄",
                "😘", "💊", "🙊", "😎", "👾", "🤷", "🤷‍♂️", "🤷‍♀️",
                "😡",
            ]
            return json.dumps({"emojis": emojis, "count": len(emojis)})

        # ------------------------------------------------------------------
        # Topic management tools
        # ------------------------------------------------------------------

        @self._mcp.tool()
        async def list_topics() -> str:
            """List all Telegram topics the agent is subscribed to."""
            result = []
            for name, tid in server_ref._topic_ids.items():
                result.append({"name": name, "topic_id": tid})
            return json.dumps({"topics": result, "count": len(result)})

        @self._mcp.tool()
        async def set_active_topic(topic: str) -> str:
            """Set which topic the agent is actively working on.

            Updates topic icons and sets the default topic for
            send_message/send_image/ask_approval.

            Args:
                topic: Topic name. Must be subscribed.
            """
            if topic not in server_ref._topic_ids:
                raise ToolError(
                    f"Topic '{topic}' not subscribed. "
                    f"Subscribed: {', '.join(server_ref._topic_ids.keys())}"
                )

            server_ref._active_topic = topic

            # Update topic icons if available
            try:
                from ghost.services.telegram_topic_icons import TOPIC_ICONS
                icon_fire = TOPIC_ICONS.get("🔥")
                icon_eyes = TOPIC_ICONS.get("👀")
                if icon_fire and icon_eyes:
                    for name, tid in server_ref._topic_ids.items():
                        icon = icon_fire if name == topic else icon_eyes
                        try:
                            run_on_daemon(
                                tg.bot.edit_forum_topic(
                                    chat_id=tg.chat_id,
                                    message_thread_id=tid,
                                    icon_custom_emoji_id=icon,
                                )
                            )
                        except Exception as e:
                            logger.warning(f"Failed to set icon for topic '{name}': {e}")
            except ImportError:
                pass

            watching = [n for n in server_ref._topic_ids if n != topic]
            return json.dumps({
                "ok": True,
                "active": topic,
                "watching": watching,
            })

        # ------------------------------------------------------------------
        # Session keepalive tool
        # ------------------------------------------------------------------

        @self._mcp.tool()
        async def wait_for_message(timeout: int = 300) -> str:
            """Block until a new message arrives in the inbox, or timeout.

            Use this to keep your session alive while waiting for the user
            to respond. Preferred approach: do productive background work
            instead. Only call this when you have no productive work left.

            If this call returns an MCP error (e.g. server restarted), call
            wait_for_message again immediately — the daemon restarted and your
            wait was interrupted, but you are still in session and should keep
            listening.

            When new_messages > 0, the response includes a "messages" array
            with the message content. Messages are marked as read when
            delivered here — this is the sole consumer. The io-bridge hook
            only nudges you that messages are waiting; it does not deliver
            or consume them.

            If cancelled=true is returned, a newer wait_for_message call is
            already running — do NOT call wait_for_message again.

            Args:
                timeout: Max seconds to wait (default 300 = 5 minutes).
            """
            inbox = server_ref._inbox_path
            if inbox is None:
                raise ToolError("Inbox path not configured — agent workflow must call set_inbox_path()")

            # Bump generation so any concurrent older call exits
            self._wfm_generation += 1
            my_generation = self._wfm_generation

            def _unread_messages():
                if not inbox.exists():
                    return set()
                return {
                    f.name for f in inbox.iterdir()
                    if f.name.startswith("msg_") and f.name.endswith(".json")
                }

            def _consume_messages(filenames):
                """Read and mark as read all given inbox files."""
                messages = []
                for name in sorted(filenames):
                    try:
                        path = inbox / name
                        data = json.loads(path.read_text())
                        msg = {
                            "from": data.get("from", "unknown"),
                            "text": data.get("text", ""),
                        }
                        if data.get("topic"):
                            msg["topic"] = data["topic"]
                        if data.get("message_id"):
                            msg["message_id"] = data["message_id"]
                        if data.get("media"):
                            msg["media"] = data["media"]
                        messages.append(msg)
                        path.rename(path.with_suffix(".json.read"))
                    except Exception:
                        pass
                return messages

            existing = _unread_messages()
            if existing:
                return json.dumps({
                    "new_messages": len(existing),
                    "waited_seconds": 0,
                    "messages": _consume_messages(existing),
                })

            poll_interval = 1
            elapsed = 0

            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                if self._wfm_generation != my_generation:
                    return json.dumps({
                        "cancelled": True,
                        "reason": "superseded by a newer wait_for_message call — do NOT call wait_for_message again",
                    })
                current = _unread_messages()
                if current:
                    return json.dumps({
                        "new_messages": len(current),
                        "waited_seconds": elapsed,
                        "messages": _consume_messages(current),
                    })

            return json.dumps({
                "new_messages": 0,
                "waited_seconds": elapsed,
                "timeout": True,
            })

        # ------------------------------------------------------------------
        # Register plugin tools
        # ------------------------------------------------------------------
        for register_fn in self._plugin_registrations:
            try:
                register_fn(self._mcp, run_on_daemon, get_topic)
            except Exception as e:
                logger.warning(f"Plugin tool registration failed: {e}")

    def _build_app(self) -> Starlette:
        """Build combined Starlette app with MCP streamable HTTP + REST API routes."""
        mcp_app = self._mcp.streamable_http_app()
        session_mgr = self._mcp.session_manager

        async def api_health(request: Request) -> JSONResponse:
            return JSONResponse({
                "status": "ok",
                "topic_id": self.topic_id,
                "topic_ids": self._topic_ids,
            })

        async def api_notify(request: Request) -> JSONResponse:
            """Send a text notification to the agent's Telegram topic."""
            body = await request.json()
            text = body.get("text", "")
            if not text:
                return JSONResponse({"error": "missing text"}, status_code=400)
            topic_name = body.get("topic_name")
            try:
                if topic_name:
                    topic = self._run_on_daemon_loop(
                        self.tg_client.resolve_topic(topic_name)
                    )
                else:
                    topic = self._get_topic_id(None)
            except Exception:
                topic = next(iter(self._topic_ids.values()), self.topic_id)
            msg_id = self._run_on_daemon_loop(
                self.tg_client.send_message(text, topic=topic, parse_mode=None)
            )
            return JSONResponse({"ok": True, "message_id": msg_id})

        async def api_edit_notify(request: Request) -> JSONResponse:
            """Edit a previously sent notification message."""
            body = await request.json()
            message_id = body.get("message_id")
            text = body.get("text", "")
            if not message_id:
                return JSONResponse({"error": "missing message_id"}, status_code=400)
            if not text:
                return JSONResponse({"error": "missing text"}, status_code=400)
            try:
                self._run_on_daemon_loop(
                    self.tg_client.edit_message(message_id, text, parse_mode=None)
                )
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True})

        async def api_delete_notify(request: Request) -> JSONResponse:
            """Delete a previously sent notification message."""
            body = await request.json()
            message_id = body.get("message_id")
            if not message_id:
                return JSONResponse({"error": "missing message_id"}, status_code=400)
            try:
                self._run_on_daemon_loop(
                    self.tg_client.delete_message(message_id)
                )
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True})

        async def api_topic_icon(request: Request) -> JSONResponse:
            """Change a topic's icon emoji."""
            body = await request.json()
            emoji_id = body.get("emoji_id", "")
            if not emoji_id:
                return JSONResponse({"error": "missing emoji_id"}, status_code=400)
            topic = self._get_topic_id()
            try:
                self._run_on_daemon_loop(
                    self.tg_client.bot.edit_forum_topic(
                        chat_id=self.tg_client.chat_id,
                        message_thread_id=topic,
                        icon_custom_emoji_id=emoji_id,
                    )
                )
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True})

        async def api_set_all_sleeping(request: Request) -> JSONResponse:
            """Set sleeping icon on all subscribed topics and clear active topic."""
            self._active_topic = None
            try:
                from ghost.services.telegram_topic_icons import TOPIC_ICONS
                icon_robot = TOPIC_ICONS.get("🤖")
            except ImportError:
                icon_robot = None

            if not icon_robot:
                return JSONResponse({"ok": True, "note": "no icon available"})

            errors = []
            for name, tid in self._topic_ids.items():
                try:
                    self._run_on_daemon_loop(
                        self.tg_client.bot.edit_forum_topic(
                            chat_id=self.tg_client.chat_id,
                            message_thread_id=tid,
                            icon_custom_emoji_id=icon_robot,
                        )
                    )
                except Exception as e:
                    errors.append(f"{name}: {e}")
            if errors:
                return JSONResponse({"ok": False, "errors": errors})
            return JSONResponse({"ok": True, "topics": list(self._topic_ids.keys())})

        api_routes = [
            Route("/api/health", api_health, methods=["GET"]),
            Route("/api/notify", api_notify, methods=["POST"]),
            Route("/api/edit-notify", api_edit_notify, methods=["POST"]),
            Route("/api/delete-notify", api_delete_notify, methods=["POST"]),
            Route("/api/topic-icon", api_topic_icon, methods=["POST"]),
            Route("/api/set-all-sleeping", api_set_all_sleeping, methods=["POST"]),
        ]

        @contextlib.asynccontextmanager
        async def lifespan(app):
            async with session_mgr.run():
                yield

        app = Starlette(
            routes=[
                *api_routes,
                Mount("/", app=mcp_app),
            ],
            lifespan=lifespan,
        )
        return app

    def _server_thread_target(self):
        """Run uvicorn in a dedicated thread with its own event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._server_loop = loop
        try:
            loop.run_until_complete(self._uvicorn_server.serve())
        except Exception as e:
            logger.error(f"MCP server thread error: {e}")
        finally:
            loop.close()

    async def start(self) -> bool:
        """Start the MCP server in a dedicated thread. Returns True if server is up."""
        app = self._build_app()

        config = uvicorn.Config(
            app, host="::", port=self.port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)

        self._server_thread = threading.Thread(
            target=self._server_thread_target,
            daemon=True,
            name="ghost-mcp",
        )
        self._server_thread.start()

        # Wait for port to accept connections (up to 10s).
        # Must use ::1 (IPv6 loopback) since server binds to :: (IPv6).
        connected = False
        for i in range(100):
            await asyncio.sleep(0.1)
            if not self._server_thread.is_alive():
                logger.error("MCP server thread died during startup")
                return False
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("::1", self.port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()
                connected = True
                break
            except (OSError, asyncio.TimeoutError):
                continue

        if connected:
            logger.info(f"MCP server listening on port {self.port}")
        else:
            logger.error(f"MCP server FAILED to start on port {self.port} after 10s")
        return connected

    async def stop(self):
        """Stop the MCP server."""
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
        logger.info("MCP server stopped")
