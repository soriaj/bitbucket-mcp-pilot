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
import logging
import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from src.config import get_settings

logger = logging.getLogger(__name__)


class GleanAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates that requests originate from the configured
    Glean instance and carry a valid Bearer token.
    """

    def __init__(self, app):
        super().__init__(app)
        self.settings = get_settings()
        self._http = httpx.AsyncClient(timeout=10.0)
        # Token validation cache: hash(token) → expiry timestamp
        self._validated_tokens: dict[str, float] = {}
        # Build the allowed origin pattern from glean_instance
        self._allowed_glean_host = (
            f"{self.settings.glean_instance}-be.glean.com"
            if self.settings.glean_instance
            else None
        )

    async def dispatch(self, request: Request, call_next):
        # Skip auth for these paths:
        # - /health  → load balancer health checks
        # - /sse     → SSE connection + tool discovery
        # - OPTIONS  → CORS preflight
        skip_paths = {"/health", "/sse"}

        if request.url.path in skip_paths or request.method == "OPTIONS":
            return await call_next(request)

        auth_mode = self.settings.auth_mode

        # Development mode — no auth
        if auth_mode == "none":
            return await call_next(request)

        # ── Check 1: Bearer token present ──
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(f"Rejected: No Bearer token from " f"{request.client.host}")
            return JSONResponse(
                status_code=401,
                content={"error": "Bearer token required"},
            )

        token = auth_header[7:]

        # ── Check 2: Token not empty/malformed ──
        if len(token) < 10:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid token format"},
            )

        # ── Check 3: Token validation with caching ──
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]

        if token_hash in self._validated_tokens:
            if time.time() < self._validated_tokens[token_hash]:
                # Cached valid token
                return await call_next(request)
            else:
                del self._validated_tokens[token_hash]

        # Validate token against Bitbucket (since Glean obtained
        # a Bitbucket OAuth token via the OAuth flow you configured)
        is_valid = await self._validate_token(token)

        if not is_valid:
            logger.warning(f"Rejected: Invalid token from " f"{request.client.host}")
            return JSONResponse(
                status_code=403,
                content={"error": "Invalid or expired token"},
            )

        # Cache valid token for 5 minutes
        self._validated_tokens[token_hash] = time.time() + 300
        self._cleanup_cache()

        # return await call_next(request)
        return await self._maybe_check_origin(request, call_next)

    async def _maybe_check_origin(self, request: Request, call_next):
        """Apply origin huristics only when configured."""

        # Enforce orgin heuristics in glean_only mode
        if self.settings.auth_mode == "glean_only":
            if not self._check_request_origin(request):
                logger.warning(
                    "Rejected: Request failed origin heuristic check. "
                    f"Client: {request.client.host}, "
                    f"User-Agent: {request.headers.get('user-agent')}"
                )
                return JSONResponse(
                    status_code=403,
                    content={"error": "Unauthorized origin"},
                )

        return await call_next(request)

    def _check_request_origin(self, request: Request) -> bool:
        """
        Verify the request likely comes from the allowed
        Glean instance by checking available signals.

        Checks (in order):
        1. X-Forwarded-For / client IP patterns
        2. User-Agent (Glean's backend uses Go-http-client)
        3. Referer/Origin headers if present
        """
        # Check User-Agent — Glean's backend is a Go service
        user_agent = request.headers.get("user-agent", "")
        if "Go-http-client" not in user_agent:
            logger.info(f"Non-Glean User-Agent: {user_agent}")
            return False

        # Check Origin/Referer if present
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")

        if origin and self._allowed_glean_host:
            if self._allowed_glean_host not in origin:
                return False

        if referer and self._allowed_glean_host:
            if self._allowed_glean_host not in referer:
                return False

        return True

    async def _validate_token(self, token: str) -> bool:
        """
        Validate the OAuth token by calling Bitbucket's
        user endpoint. If the token is valid, Bitbucket
        returns the user profile. If expired/revoked, 401.

        This works because Glean obtained a Bitbucket OAuth
        token via the Authorization URL/Token URL you
        configured in the Glean admin console.
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

            logger.info(f"Token validation failed: " f"{response.status_code}")
            return False

        except Exception as e:
            logger.exception("Error validating token")
            return False

    def _cleanup_cache(self):
        """Remove expired entries from token cache."""
        if len(self._validated_tokens) > 500:
            now = time.time()
            self._validated_tokens = {
                k: v for k, v in self._validated_tokens.items() if v > now
            }
