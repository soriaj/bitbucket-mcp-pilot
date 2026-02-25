"""
Bitbucket REST API client.

Wraps the Bitbucket Cloud 2.0 API with typed methods for
PR review operations. All methods validate inputs and handle
errors gracefully.
"""

import re
import logging
import httpx
from typing import Any
from urllib.parse import quote
import json
from src.auth import BitbucketAuth
from src.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


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


def _sanitize_text(value: str) -> str:
    """
    Strip carriage returns that appear in Bitbucket API
    responses using Windows-style CRLF line endings. These encode as
    %0D in URLs and cause malformed redirect targets.
    """
    return value.replace("\r", "").strip()


def _parse_diff_into_files(raw_diff: str) -> list[dict]:
    """
    Splits a unified diff into per-file sections and annotates each
    with a change_type and fetchable flag so get_pull_request_diff
    can guide the agent away from calling get_file_content on files
    that don't exist at the source commit (deleted, binary, etc.).

    change_type values:
      added    → new file, exists at source commit     → fetchable
      modified → existing file changed                 → fetchable
      renamed  → moved/renamed, use new filename       → fetchable
      deleted  → removed, does NOT exist at source     → NOT fetchable
      binary   → image/compiled artifact, not text     → NOT fetchable
    """
    file_sections = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)
    files = []

    for section in file_sections:
        if not section.strip():
            continue

        header = re.match(r"diff --git a/(.+?) b/(.+)", section)
        if not header:
            continue

        old_path = header.group(1)
        new_path = header.group(2)

        # Determine change type from diff header markers
        if "new file mode" in section:
            change_type = "added"
        elif "deleted file mode" in section:
            change_type = "deleted"
        elif "Binary files" in section or "GIT binary patch" in section:
            change_type = "binary"
        elif old_path != new_path:
            change_type = "renamed"
        else:
            change_type = "modified"

        additions = max(section.count("\n+") - section.count("\n+++"), 0)
        deletions = max(section.count("\n-") - section.count("\n---"), 0)

        files.append(
            {
                # Always use new_path — correct after renames, same as old for others
                "filename": new_path,
                "old_filename": old_path if change_type == "renamed" else None,
                "change_type": change_type,
                "additions": additions,
                "deletions": deletions,
                # fetchable=false means get_file_content WILL 404 — agent must skip
                "fetchable": change_type in ("added", "modified", "renamed"),
                "size_chars": len(section),
                # Inline diff only for small files — avoids blowing context on large ones
                "diff": (
                    section
                    if len(section) < 8_000
                    else "[use get_file_content to fetch full diff]"
                ),
            }
        )

    return files


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

        Each file entry includes:
          - filename      → path to use when calling get_file_content
          - change_type   → added | modified | renamed | deleted | binary
          - fetchable     → True if get_file_content can be called safely
          - additions     → lines added
          - deletions     → lines removed
          - diff          → inline diff if small (<8k chars), else placeholder

        The agent MUST check fetchable=true before calling get_file_content.
        Deleted and binary files do not exist at the source commit and will 404.
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
        if response.status_code == 404:
            raise ValueError(
                f"PR #{pr_id} diff not found in {workspace}/{repo_slug}. "
                "Check that the PR ID is correct and the PR is open."
            )
        if response.status_code == 403:
            raise PermissionError(
                "Insufficient permissions to read diff. "
                "Ensure your OAuth token has 'pullrequest' read scope."
            )

        response.raise_for_status()

        diff_text = _sanitize_text(response.text)
        files = _parse_diff_into_files(diff_text)

        manifest = {
            "total_files_changed": len(files),
            "total_additions": sum(f["additions"] for f in files),
            "total_deletions": sum(f["deletions"] for f in files),
            "note": (
                "Only call get_file_content for files where fetchable=true. "
                "Do NOT call it for deleted or binary files — they will 404."
            ),
            "files": files,
        }

        # Truncate very large diffs to avoid LLM context overflow
        max_chars = settings.max_chars
        manifest_text = json.dumps(manifest, indent=2)
        if len(manifest_text) > max_chars:
            while len(json.dumps(manifest, indent=2)) > max_chars and manifest["files"]:
                manifest["files"].pop()
            manifest["truncated"] = True
            manifest["note"] += (
                "\n\n[MANIFEST TRUNCATED — not all files shown. "
                "Remaining files were removed to fit context limit."
            )
            manifest_text = json.dumps(manifest, indent=2)

        return manifest_text

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
        # URL-encode the ref to safely handle branch names containing '/'
        # e.g. 'feature/hello-world' → 'feature%2Fhello-world'
        encoded_ref = quote(ref, safe="")

        response = await self._http.get(
            f"/repositories/{workspace}/{repo_slug}/src/{encoded_ref}/{file_path}",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        if response.status_code == 404:
            logger.warning(
                f"File '{file_path}' not found at ref='{ref}' "
                f"in {workspace}/{repo_slug}. "
                "Verify the branch name and file path from the PR diff. "
                "Likely delted, renamed, or binary."
            )
            return (
                f"[FILE SKIPPED: '{file_path}' does not exist at ref '{ref}'. "
                f"Possible reasons: file was deleted, renamed (check 'old_filename' "
                f"in the diff manifest for the new path), or is a binary file. "
                f"Continue reviewing the remaining files.]"
            )
        if response.status_code == 403:
            raise PermissionError(
                f"Cannot read '{file_path}' — check repository read permissions."
            )
        response.raise_for_status()
        return response.text

    # ── Helper: Get PR source branch ref ────────────────────────────────────
    async def get_pr_source_ref(
        self, workspace: str, repo_slug: str, pr_id: int
    ) -> str:
        """
        Returns the source branch name of a PR.
        Use this ref when calling get_file_content for files changed in the PR.
        """
        pr = await self.get_pull_request(workspace, repo_slug, pr_id)
        branch = pr.get("source", {}).get("branch", {}).get("name", "")
        commit = pr.get("source", {}).get("commit", {}).get("hash", "")
        # Prefer commit hash (immutable) over branch name for precision
        return _sanitize_text(commit or branch)

    async def close(self):
        """Cleanup HTTP clients."""
        await self._http.aclose()
        await self.auth.close()
