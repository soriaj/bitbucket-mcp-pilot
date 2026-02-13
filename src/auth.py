"""
OAuth 2.0 token management for Bitbucket API access.

Supports flow: Client Credentials (service-to-service, workspace-level)

"""

import time
import httpx
from dataclasses import dataclass, field
from src.config import get_settings


@dataclass
class TokenInfo:
    """Stores OAuth token with expiry tracking."""

    access_token: str
    expires_at: float
    refresh_token: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        """Check if token is expired (with 60s buffer)."""
        return time.time() >= (self.expires_at - 60)


class BitbucketAuth:
    """Manages Bitbucket OAuth 2.0 authentication."""

    def __init__(self):
        self.settings = get_settings()
        self._token: TokenInfo | None = None
        self._http = httpx.AsyncClient(timeout=30.0)

    async def get_access_token(self) -> str:
        """
        Get a valid access token, refreshing if expired.

        Returns:
            str: A valid Bitbucket access token

        Raises:
            httpx.HTTPStatusError: If token request fails
        """
        if self._token and not self._token.is_expired:
            return self._token.access_token

        if self._token and self._token.refresh_token:
            return await self._refresh_token()

        return await self._request_new_token()

    async def _request_new_token(self) -> str:
        """Request a new token using Client Credentials grant."""
        response = await self._http.post(
            f"{self.settings.bitbucket_auth_url}/access_token",
            data={"grant_type": "client_credentials"},
            auth=(
                self.settings.bitbucket_client_id,
                self.settings.bitbucket_client_secret,
            ),
        )
        # DEBUG: Log the response body before raising
        if response.status_code != 200:
            logger.error(
                f"Token request failed: {response.status_code} "
                f"Body: {response.text}"
            )

        response.raise_for_status()
        data = response.json()

        self._token = TokenInfo(
            access_token=data["access_token"],
            expires_at=time.time() + data.get("expires_in", 7200),
            refresh_token=data.get("refresh_token"),
            scopes=data.get("scopes", "").split(),
        )
        return self._token.access_token

    async def _refresh_token(self) -> str:
        """Refresh an expired token."""
        response = await self._http.post(
            f"{self.settings.bitbucket_auth_url}/access_token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token,
            },
            auth=(
                self.settings.bitbucket_client_id,
                self.settings.bitbucket_client_secret,
            ),
        )
        response.raise_for_status()
        data = response.json()

        self._token = TokenInfo(
            access_token=data["access_token"],
            expires_at=time.time() + data.get("expires_in", 7200),
            refresh_token=data.get("refresh_token", self._token.refresh_token),
        )
        return self._token.access_token

    async def close(self):
        """Cleanup HTTP client."""
        await self._http.aclose()
