"""
Bitbucket REST API client.

Wraps the Bitbucket Cloud 2.0 API with typed methods for
PR review operations.
"""

import re
import logging
import httpx
from typing import Any
from src.auth import BitbucketAuth
from src.config import get_settings

logger = logging.getLogger(__name__)


def _validate_slug(value: str, field_name: str) -> str:
    """
    Validate that a value is a safe Bitbucket slug.
    Prevents path traversal and injection attacks.
    """
    if not re.match(r"^[a-zA-Z0-9._-]+$", value):
        raise ValueError(
            f"Invalid {field_name}: '{value}'. "
            "Only alphanumeric, dots, hyphens, and underscores allowed."
        )
    return value


class BitbucketClient:
    """
    Async client for Bitbucket Cloud REST API 2.0.

    Handles authentication, input validation, and error handling
    for all PR review operations.
    """

    def __init__(self, auth: BitbucketAuth):
        self.auth = auth
        self.settings = get_settings()
        self._http = httpx.AsyncClient(
            base_url=self.settings.bitbucket_api_base,
            timeout=30.0,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Make an authenticated request to the Bitbucket API.

        Automatically injects the Bearer token and handles
        common error responses.
        """
        token = await self.auth.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **kwargs.pop("headers", {}),
        }

        response = await self._http.request(method, path, headers=headers, **kwargs)

        if response.status_code == 404:
            raise ValueError(f"Resource not found: {path}")
        if response.status_code == 403:
            raise PermissionError(
                f"Insufficient permissions for: {path}. "
                "Check your OAuth consumer scopes."
            )

        response.raise_for_status()

        # Some endpoints return empty body (204)
        if response.status_code == 204:
            return {"status": "success"}

        return response.json()

    # ── PR Details ──────────────────────────────────────────

    async def get_pull_request(
        self, workspace: str, repo_slug: str, pr_id: int
    ) -> dict:
        """
        Fetch full details of a pull request.

        Returns: PR metadata including title, description,
        author, reviewers, source/destination branches, and state.
        """
        workspace = _validate_slug(workspace, "workspace")
        repo_slug = _validate_slug(repo_slug, "repo_slug")

        return await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}",
        )

    # ── PR Diff ─────────────────────────────────────────────

    async def get_pull_request_diff(
        self, workspace: str, repo_slug: str, pr_id: int
    ) -> str:
        """
        Fetch the unified diff for a pull request.

        Returns: Raw diff text showing all file changes.
        This is what the LLM will analyze for code review.
        """
        workspace = _validate_slug(workspace, "workspace")
        repo_slug = _validate_slug(repo_slug, "repo_slug")

        token = await self.auth.get_access_token()
        response = await self._http.get(
            f"/repositories/{workspace}/{repo_slug}" f"/pullrequests/{pr_id}/diff",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/plain",
            },
            follow_redirects=True,
        )
        response.raise_for_status()

        # Truncate very large diffs to avoid LLM context overflow
        diff_text = response.text
        max_chars = 100_000  # ~25k tokens
        if len(diff_text) > max_chars:
            diff_text = diff_text[:max_chars] + (
                "\n\n[DIFF TRUNCATED — too large for review. "
                "Consider reviewing files individually.]"
            )

        return diff_text

    # ── PR Comments ─────────────────────────────────────────

    async def list_pull_request_comments(
        self, workspace: str, repo_slug: str, pr_id: int
    ) -> list[dict]:
        """
        List all comments on a pull request.

        Returns: List of comment objects with content,
        author, and inline location data.
        """
        workspace = _validate_slug(workspace, "workspace")
        repo_slug = _validate_slug(repo_slug, "repo_slug")

        result = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}" f"/pullrequests/{pr_id}/comments",
        )
        return result.get("values", [])

    # ── File Contents ───────────────────────────────────────

    async def get_file_content(
        self,
        workspace: str,
        repo_slug: str,
        file_path: str,
        ref: str = "main",
    ) -> str:
        """
        Fetch the content of a specific file from the repository.

        Useful for reading style guides, linting configs, or
        related documentation that informs the review.
        """
        workspace = _validate_slug(workspace, "workspace")
        repo_slug = _validate_slug(repo_slug, "repo_slug")

        token = await self.auth.get_access_token()
        response = await self._http.get(
            f"/repositories/{workspace}/{repo_slug}/src/{ref}/{file_path}",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    async def close(self):
        """Cleanup HTTP clients."""
        await self._http.aclose()
        await self.auth.close()
