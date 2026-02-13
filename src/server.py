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
from mcp.server.sse import SseServerTransport
from starlette.responses import Response
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from src.tools import create_mcp_server
from src.config import get_settings
from src.middleware import GleanAuthMiddleware

# Configure logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> Starlette:
    """Create the ASGI application with MCP SSE transport."""

    mcp_server = create_mcp_server()
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        """Handle SSE connection from Glean's MCP Host."""
        logger.info(f"New SSE connection from {request.client.host}")
        # Use the raw ASGI send callable from scope
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send or request.scope.get("send")
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )
        # Return empty response — SSE already sent via transport
        return Response()

    async def handle_messages(request):
        """Handle incoming MCP messages over SSE."""
        await sse_transport.handle_post_message(
            request.scope, request.receive, request._send or request.scope.get("send")
        )
        return Response("Accepted", status_code=202)

    async def health_check(request):
        """Health check endpoint for load balancers."""
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "healthy"})

    allowed_origins = [
        o.strip() for o in settings.allowed_origins.split(",") if o.strip()
    ] or ["*"]

    app = Starlette(
        routes=[
            Route("/health", health_check),
            Route("/sse", handle_sse),
            Route("/messages/", handle_messages, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=allowed_origins,
                allow_methods=["GET", "POST"],
                allow_headers=["*"],
            ),
            Middleware(GleanAuthMiddleware),
        ],
    )
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "src.server:app",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
        log_level=settings.log_level.lower(),
    )
