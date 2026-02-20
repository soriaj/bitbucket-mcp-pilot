"""
MCP Server entry point.

Exposes the Bitbucket PR Review tools via SSE transport,
which Glean's Agent Builder connects to as an MCP Host.

Transport options:
- SSE (Server-Sent Events) — recommended, widely supported
- Streamable HTTP — newer MCP spec, use if Glean supports it
"""

import logging
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.routing import Route, Mount
from starlette.types import Scope, Receive, Send
from mcp.server.sse import SseServerTransport

from src.tools import create_mcp_server
from src.config import get_settings
from src.middleware import GleanAuthMiddleware

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _NoopResponse(Response):
    """
    A response that intentionally does nothing when Starlette calls it.

    WHY THIS EXISTS:
    Starlette's route machinery always calls `await response(scope, receive, send)`
    on whatever the handler returns. For SSE and POST /messages/ endpoints,
    the MCP transport (SseServerTransport) has ALREADY sent the full HTTP response
    via the `send` callable before the handler returns. If Starlette then tries
    to send another `http.response.start`, Uvicorn raises:

      RuntimeError: Unexpected ASGI message 'http.response.start'
                    sent, after response already completed.

    This class suppresses that second send, letting the SSE transport
    fully own the response lifecycle.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pass  # SSE/message transport already completed the response — do nothing


def create_app() -> Starlette:
    """
    Build the inner Starlette ASGI app (routes + CORS only).
    GleanAuthMiddleware is applied OUTSIDE this in create_asgi_app().
    """
    mcp_server = create_mcp_server()
    sse_transport = SseServerTransport("/messages/")

    # ── Route Handlers ──────────────────────────────────────────────────────

    async def handle_sse(request: Request) -> _NoopResponse:
        """
        Establish the SSE connection for Glean's MCP host.

        The SSE transport fully owns the HTTP response lifecycle:
          1. Sends 'http.response.start' (SSE headers + 200)
          2. Streams SSE events for the duration of the session
          3. Closes cleanly when the connection drops

        We return _NoopResponse to prevent Starlette from sending a
        second 'http.response.start' after connect_sse() exits.
        """
        logger.info(f"New SSE connection from {request.client.host}")
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,  # The raw Uvicorn send callable — not scope["send"]
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )
        logger.info(f"SSE connection closed for {request.client.host}")
        return _NoopResponse()

    async def handle_messages(request: Request) -> _NoopResponse:
        """
        Accept an incoming MCP message from Glean over the SSE session.

        SseServerTransport.handle_post_message() sends the 202 Accepted
        response itself via the send callable. We return _NoopResponse
        to prevent Starlette from sending a duplicate response.

        NOTE: If you are on an older MCP SDK version where handle_post_message
        does NOT send a response, swap _NoopResponse for:
          return Response("Accepted", status_code=202)
        """
        await sse_transport.handle_post_message(
            request.scope,
            request.receive,
            request._send,
        )
        return _NoopResponse()

    async def health_check(request: Request) -> JSONResponse:
        """Health check endpoint for Cloud Run / load balancers."""
        return JSONResponse({"status": "healthy"})

    # ── Starlette App (CORS only — auth is applied outside) ────────────────

    allowed_origins = [
        o.strip() for o in settings.allowed_origins.split(",") if o.strip()
    ] or ["*"]

    starlette_app = Starlette(
        routes=[
            Route("/health", health_check),
            Route("/sse", handle_sse),
            Route("/messages/", handle_messages, methods=["POST"]),
        ],
        middleware=[
            # CORS only — GleanAuthMiddleware is applied outside Starlette
            # so it doesn't go through Starlette's middleware wrapping machinery.
            Middleware(
                CORSMiddleware,
                allow_origins=allowed_origins,
                allow_methods=["GET", "POST"],
                allow_headers=["*"],
            ),
        ],
    )

    return starlette_app


def create_asgi_app():
    """
    Compose the full ASGI app:
      GleanAuthMiddleware (pure ASGI, outermost)
        └── CORSMiddleware (Starlette)
              └── Starlette routes (SSE, messages, health)

    GleanAuthMiddleware is applied HERE, outside Starlette's middleware
    stack, to guarantee it uses pure ASGI __call__(scope, receive, send)
    without any response buffering that BaseHTTPMiddleware would add.
    """
    inner_app = create_app()
    return GleanAuthMiddleware(inner_app)


# ── ASGI entrypoint ─────────────────────────────────────────────────────────
app = create_asgi_app()

if __name__ == "__main__":
    uvicorn.run(
        "src.server:app",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
        log_level=settings.log_level.lower(),
        # Never enable reload in production — it restarts the process and
        # drops all active SSE sessions.
        reload=False,
    )
