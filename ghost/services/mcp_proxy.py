"""
MCP reverse proxy — sits between agent sessions and the daemon's MCP server.

Listens on port 7865 (what clients connect to). Forwards to daemon on 7866.
Survives daemon restarts: holds client connections stable, re-establishes
backend sessions transparently.

Run standalone:
    python -m ghost.services.mcp_proxy

Or as a launchd service (com.ghost.mcp-proxy.plist).
"""

import asyncio
import json
import logging
import signal
import uuid

from aiohttp import web, ClientSession, ClientTimeout, ClientConnectionError

PROXY_PORT = 7865
BACKEND_PORT = 7866
BACKEND_BASE = f"http://[::1]:{BACKEND_PORT}"

log = logging.getLogger("mcp-proxy")


class MCPProxy:
    """Transparent reverse proxy with MCP session ID remapping."""

    def __init__(self):
        # client_session_id → backend_session_id
        self._session_map: dict[str, str | None] = {}
        self._http: ClientSession | None = None

    async def start(self):
        self._http = ClientSession(timeout=ClientTimeout(total=None))

    async def stop(self):
        if self._http:
            await self._http.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Forward any request to the backend MCP server."""
        path = request.path_qs  # includes query string
        backend_url = f"{BACKEND_BASE}{path}"

        # Read client session ID
        client_sid = request.headers.get("mcp-session-id")

        # Map to backend session ID
        backend_sid = None
        if client_sid and client_sid in self._session_map:
            backend_sid = self._session_map[client_sid]

        # Build forwarded headers (skip hop-by-hop, remap session)
        skip = {"host", "mcp-session-id", "transfer-encoding"}
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in skip
        }
        if backend_sid:
            headers["mcp-session-id"] = backend_sid

        # Read body (and extract JSON-RPC request ID for error recovery)
        body = await request.read()
        req_id = None
        try:
            req_json = json.loads(body) if body else None
            if isinstance(req_json, dict):
                req_id = req_json.get("id")
        except Exception:
            pass

        try:
            resp = await self._http.request(
                request.method,
                backend_url,
                headers=headers,
                data=body if body else None,
            )
        except (ClientConnectionError, OSError, asyncio.TimeoutError) as e:
            log.warning("Backend unreachable: %s", e)
            # If backend died mid-session, invalidate mapping so next
            # request re-establishes
            if client_sid and client_sid in self._session_map:
                self._session_map[client_sid] = None
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": "MCP backend temporarily unavailable, retrying will reconnect",
                    },
                    "id": req_id,
                },
                status=502,
            )

        # Capture backend session ID from response
        resp_sid = resp.headers.get("mcp-session-id")
        if resp_sid:
            if not client_sid:
                # First request — generate stable proxy session ID
                client_sid = str(uuid.uuid4())
            self._session_map[client_sid] = resp_sid

        content_type = resp.content_type or ""

        # SSE — stream through
        if "text/event-stream" in content_type:
            stream = web.StreamResponse(
                status=resp.status,
                headers=self._response_headers(resp, client_sid),
            )
            stream.content_type = "text/event-stream"
            await stream.prepare(request)

            try:
                async for chunk in resp.content.iter_any():
                    await stream.write(chunk)
            except (ClientConnectionError, ConnectionResetError, asyncio.CancelledError):
                # Backend disconnected mid-stream (e.g. daemon restart).
                # Send a proper JSON-RPC error event so the MCP client
                # gets a response instead of hanging on EOF.
                if req_id is not None:
                    error_data = json.dumps({
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32001,
                            "message": "MCP server restarted — call wait_for_message again to resume",
                        },
                        "id": req_id,
                    })
                    try:
                        await stream.write(
                            f"event: message\ndata: {error_data}\n\n".encode()
                        )
                    except Exception:
                        pass
                # Invalidate backend session — next request reconnects
                if client_sid and client_sid in self._session_map:
                    self._session_map[client_sid] = None
            finally:
                resp.close()

            return stream

        # Regular response — read and forward
        resp_body = await resp.read()
        return web.Response(
            status=resp.status,
            headers=self._response_headers(resp, client_sid),
            body=resp_body,
        )

    def _response_headers(self, resp, client_sid: str | None) -> dict:
        """Build response headers, remapping session ID to proxy's stable ID."""
        skip = {"transfer-encoding", "content-encoding", "content-length", "mcp-session-id"}
        headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in skip
        }
        if client_sid:
            headers["mcp-session-id"] = client_sid
        return headers


async def run_proxy():
    proxy = MCPProxy()
    await proxy.start()

    app = web.Application()
    # Catch-all: forward everything
    app.router.add_route("*", "/{path:.*}", proxy.handle)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "::1", PROXY_PORT)
    await site.start()

    log.info("MCP proxy listening on [::1]:%d → backend :%d", PROXY_PORT, BACKEND_PORT)

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Shutting down proxy")
    await runner.cleanup()
    await proxy.stop()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    asyncio.run(run_proxy())


if __name__ == "__main__":
    main()
