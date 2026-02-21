"""
Inbound authentication middleware.

Ensures only your Glean instance can invoke MCP tools.
Three validation checks:
1. Bearer token must be present
2. Token must be valid (not expired/revoked)
3. Request must come from the allowed Glean instance
"""

import time
import hashlib
import json
import logging
import httpx
from starlette.requests import Request
from starlette.types import ASGIApp, Scope, Receive, Send
from src.config import get_settings

logger = logging.getLogger(__name__)


class GleanAuthMiddleware:
    """
    Pure ASGI middleware that validates requests from the configured
    Glean instance. Does NOT subclass BaseHTTPMiddleware — avoids
    response buffering that breaks SSE streaming connections.
    """

    # Paths that bypass auth entirely:
    #   /health  → Cloud Run / load balancer health checks
    #   /sse     → SSE handshake (tool discovery ListToolsRequest comes here)
    #   OPTIONS  → CORS preflight
    SKIP_PATHS = {"/health", "/sse"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.settings = get_settings()
        self._http = httpx.AsyncClient(timeout=10.0)

        # Token validation cache: sha256(token)[:16] → expiry timestamp
        # NOTE: per-instance cache. Acceptable for Cloud Run single-instance
        # dev/demo deployments. For multi-instance prod, use Redis or similar.
        self._validated_tokens: dict[str, float] = {}

        # Allowed Glean backend host for origin heuristic checks
        self._allowed_glean_host = (
            f"{self.settings.glean_instance}-be.glean.com"
            if self.settings.glean_instance
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Pure ASGI entry point. Only intercepts HTTP requests.
        WebSocket and lifespan scopes are passed straight through
        without any response wrapping.
        """
        if scope["type"] != "http":
            # Pass WebSocket / lifespan scopes through untouched
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        # ── Skip auth for health checks, SSE, and CORS preflight ──────────
        if request.url.path in self.SKIP_PATHS or request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # ── Development mode — bypass all auth ────────────────────────────
        if self.settings.auth_mode == "none":
            await self.app(scope, receive, send)
            return

        # ── Check 1: Bearer token present ─────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                f"Rejected (no Bearer token): {request.client.host} "
                f"→ {request.method} {request.url.path}"
            )
            await self._send_json_response(
                send, status=401, body={"error": "Bearer token required"}
            )
            return

        token = auth_header[7:]

        # ── Check 2: Token not empty/malformed ────────────────────────────
        if len(token) < 10:
            await self._send_json_response(
                send, status=401, body={"error": "Invalid token format"}
            )
            return

        # ── Check 3: Token validation (with cache) ────────────────────────
        token_hash = hashlib.sha256(token.encode()).hexdigest()[
            :32
        ]  # 32 chars for safety
        now = time.time()

        if token_hash in self._validated_tokens:
            if now < self._validated_tokens[token_hash]:
                # Cache hit — skip Bitbucket API call
                pass
            else:
                # Expired — remove and re-validate
                del self._validated_tokens[token_hash]
                if not await self._validate_token(token):
                    logger.warning(f"Rejected (expired token): {request.client.host}")
                    await self._send_json_response(
                        send, status=403, body={"error": "Invalid or expired token"}
                    )
                    return
                self._validated_tokens[token_hash] = now + 300
        else:
            # Cache miss — validate live against Bitbucket
            if not await self._validate_token(token):
                logger.warning(f"Rejected (invalid token): {request.client.host}")
                await self._send_json_response(
                    send, status=403, body={"error": "Invalid or expired token"}
                )
                return
            self._validated_tokens[token_hash] = now + 300

        self._cleanup_cache()

        # ── Check 4: Origin heuristics (glean_only mode) ──────────────────
        if self.settings.auth_mode == "glean_only":
            if not self._check_request_origin(request):
                logger.warning(
                    f"Rejected (origin check failed): "
                    f"client={request.client.host}, "
                    f"user-agent={request.headers.get('user-agent')}"
                )
                await self._send_json_response(
                    send, status=403, body={"error": "Unauthorized origin"}
                )
                return

        # ── All checks passed — forward to application ─────────────────────
        await self.app(scope, receive, send)

    # ── Origin Heuristics ──────────────────────────────────────────────────

    def _check_request_origin(self, request: Request) -> bool:
        """
        Verify the request likely comes from the allowed Glean instance.
        Checks (in order):
          1. User-Agent — Glean's backend uses Go-http-client
          2. Origin / Referer headers (if present)

        Two Glean hosts must be allowed:
        - support-lab-be.glean.com  → Go backend (tool execution)
        - support-lab.glean.com     → Browser frontend (Agent Builder UI
                                        tool saving/validation)
        """
        user_agent = request.headers.get("user-agent", "")
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")

        # ── User-Agent: advisory signal only ─────────────────────────────────
        # Log unexpected UAs for visibility but do NOT block on them.
        if "Go-http-client" not in user_agent:
            logger.info(
                f"Non-Go-http-client User-Agent observed (allowed): '{user_agent}' "
                f"from {request.client.host} → {request.method} {request.url.path}"
            )

            # ── Build allowed host list: both frontend and backend ────────────────
            # self._allowed_glean_host = "support-lab-be.glean.com"  (backend)
            # frontend host            = "support-lab.glean.com"     (Agent Builder UI)
            allowed_hosts = []
            if self._allowed_glean_host:
                allowed_hosts.append(
                    self._allowed_glean_host
                )  # support-lab-be.glean.com
            if self.settings.glean_instance:
                allowed_hosts.append(
                    f"{self.settings.glean_instance}.glean.com"
                )  # support-lab.glean.com

            # ── Origin header: hard check only if present ─────────────────────────
            if origin and allowed_hosts:
                if not any(host in origin for host in allowed_hosts):
                    logger.warning(
                        f"Origin mismatch: got '{origin}', "
                        f"expected one of: {allowed_hosts}"
                    )
                    return False

            # ── Referer header: hard check only if present ────────────────────────
            if referer and allowed_hosts:
                if not any(host in referer for host in allowed_hosts):
                    logger.warning(
                        f"Referer mismatch: got '{referer}', "
                        f"expected one of: {allowed_hosts}"
                    )
                    return False

            return True

    # ── Token Validation ───────────────────────────────────────────────────

    async def _validate_token(self, token: str) -> bool:
        """
        Validate the OAuth token by calling Bitbucket's /user endpoint.
        Returns True if the token is valid, False otherwise.
        """
        try:
            response = await self._http.get(
                "https://api.bitbucket.org/2.0/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            if response.status_code == 200:
                user_data = response.json()
                logger.info(
                    f"Token validated for Bitbucket user: "
                    f"{user_data.get('display_name', 'unknown')}"
                )
                return True
            logger.info(f"Token validation failed: HTTP {response.status_code}")
            return False
        except Exception:
            logger.exception("Error during token validation against Bitbucket")
            return False

    # ── ASGI Response Helper ───────────────────────────────────────────────

    @staticmethod
    async def _send_json_response(send: Send, status: int, body: dict) -> None:
        """
        Send a minimal JSON HTTP response directly via the ASGI send callable.
        Used to reject requests before they reach the application.
        Safe to use in pure ASGI middleware — no response buffering.
        """
        body_bytes = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body_bytes)).encode()],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
                "more_body": False,
            }
        )

    # ── Cache Maintenance ──────────────────────────────────────────────────

    def _cleanup_cache(self) -> None:
        """Evict expired entries when cache grows large."""
        if len(self._validated_tokens) > 500:
            now = time.time()
            self._validated_tokens = {
                k: v for k, v in self._validated_tokens.items() if v > now
            }
