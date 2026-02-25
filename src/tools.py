"""
MCP Tool definitions for Bitbucket PR Review.

Each tool corresponds to a step in the PR review workflow:
1. get_pull_request → Fetch PR metadata
2. get_pull_request_diff → Fetch code changes
3. add_pull_request_comment → Post review feedback
4. update_pull_request_description → Enrich PR with context
5. get_file_content → Read style guides / docs
6. list_pull_request_comments → See existing review comments
"""

import json
import logging
from mcp.server import Server
from mcp.types import Tool, TextContent
from src.bitbucket_client import BitbucketClient
from src.auth import BitbucketAuth

logger = logging.getLogger(__name__)


def create_mcp_server() -> Server:
    """Create and configure the MCP server with Bitbucket tools."""
    server = Server("bitbucket-pr-review")
    auth = BitbucketAuth()
    client = BitbucketClient(auth)

    # ── Tool Discovery ──────────────────────────────────────────────────────
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """
        Return all available tools to the MCP host (Glean).
        Glean's Agent Builder will display these tools and the
        LLM will decide which to call based on descriptions.
        """
        return [
            Tool(
                name="get_pull_request",
                description=(
                    "Fetch full details of a Bitbucket pull request "
                    "including title, description, author, reviewers, "
                    "source/destination branches, and approval state. "
                    "ALWAYS call this first when reviewing a PR — the "
                    "response includes 'source_branch' and 'source_commit' "
                    "which MUST be passed as the 'ref' when calling "
                    "get_file_content for any files changed in the PR."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": "Pull request ID number",
                        },
                    },
                    "required": ["workspace", "repo_slug", "pr_id"],
                },
            ),
            Tool(
                name="get_pull_request_diff",
                description=(
                    "Fetch the unified diff (code changes) of a "
                    "Bitbucket pull request. Returns the raw diff "
                    "text showing all added, modified, and deleted "
                    "lines. Essential for code review analysis."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": "Pull request ID number",
                        },
                    },
                    "required": ["workspace", "repo_slug", "pr_id"],
                },
            ),
            Tool(
                name="get_file_content",
                description=(
                    "Read the content of a file from a Bitbucket repository. "
                    "Useful for fetching style guides, linting configurations, "
                    "CONTRIBUTING.md, or any file changed in the PR. "
                    "ALWAYS pass pr_id when reading files changed in a PR — "
                    "the server will automatically resolve the correct source "
                    "commit as the ref. Do NOT pass a ref parameter for PR files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file (e.g. 'src/hello.py')",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": (
                                "PR ID — when provided, the server auto-resolves "
                                "the correct source commit as the ref. Always pass "
                                "this when reading files changed in the PR."
                            ),
                        },
                        "ref": {
                            "type": "string",
                            "description": (
                                "Branch or commit ref. Only use this for files on "
                                "main that are NOT part of the PR (e.g. style guides). "
                                "For PR files, pass pr_id instead and omit this field."
                            ),
                        },
                    },
                    "required": ["workspace", "repo_slug", "file_path"],
                },
            ),
            Tool(
                name="list_pull_request_comments",
                description=(
                    "List all existing comments on a Bitbucket pull "
                    "request. Use this to check what feedback has "
                    "already been provided before adding new comments."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": "Pull request ID number",
                        },
                    },
                    "required": ["workspace", "repo_slug", "pr_id"],
                },
            ),
            Tool(
                name="add_pull_request_comment",
                description=(
                    "Add a review comment to a Bitbucket pull request. "
                    "Can be a general comment or an inline comment on "
                    "a specific file and line. Use this to post code "
                    "review feedback, style guide violations, or "
                    "improvement suggestions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": "Pull request ID number",
                        },
                        "content": {
                            "type": "string",
                            "description": "Comment text in Markdown format",
                        },
                        "inline_path": {
                            "type": "string",
                            "description": "File path for inline comment (optional)",
                        },
                        "inline_line": {
                            "type": "integer",
                            "description": "Line number for inline comment (optional)",
                        },
                    },
                    "required": ["workspace", "repo_slug", "pr_id", "content"],
                },
            ),
            Tool(
                name="update_pull_request_description",
                description=(
                    "Update the description (and optionally title) of "
                    "a Bitbucket pull request. Use this to enrich the "
                    "PR with contextual information from the codebase "
                    "and documentation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Bitbucket workspace slug",
                        },
                        "repo_slug": {
                            "type": "string",
                            "description": "Repository slug",
                        },
                        "pr_id": {
                            "type": "integer",
                            "description": "Pull request ID number",
                        },
                        "description": {
                            "type": "string",
                            "description": "New PR description in Markdown",
                        },
                        "title": {
                            "type": "string",
                            "description": "New PR title (optional)",
                        },
                    },
                    "required": ["workspace", "repo_slug", "pr_id", "description"],
                },
            ),
        ]

    # ── Tool Execution ──────────────────────────────────────────────────────
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """
        Execute a tool call from Glean's Agent Builder.
        Each tool call is logged for audit purposes.
        ALL errors are caught and returned as structured TextContent —
        never raised — to prevent ASGI SSE connection crashes.
        """
        logger.info(
            f"Tool called: {name} with args: {json.dumps(arguments, default=str)}"
        )
        try:
            # ── get_pull_request ──────────────────────────────────────────
            if name == "get_pull_request":
                result = await client.get_pull_request(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                # FIX: expose both source_branch and source_commit so the
                # agent can pass source_commit as `ref` to get_file_content.
                # Commit hash is preferred — it's immutable and unambiguous.
                summary = {
                    "title": result.get("title"),
                    "description": result.get("description"),
                    "state": result.get("state"),
                    "author": result.get("author", {}).get("display_name"),
                    "source_branch": (
                        result.get("source", {}).get("branch", {}).get("name")
                    ),
                    "source_commit": (
                        result.get("source", {}).get("commit", {}).get("hash")
                    ),
                    "destination_branch": (
                        result.get("destination", {}).get("branch", {}).get("name")
                    ),
                    "destination_commit": (
                        result.get("destination", {}).get("commit", {}).get("hash")
                    ),
                    "reviewers": [
                        r.get("display_name") for r in result.get("reviewers", [])
                    ],
                    "created_on": result.get("created_on"),
                    "updated_on": result.get("updated_on"),
                    "comment_count": result.get("comment_count"),
                    "link": result.get("links", {}).get("html", {}).get("href"),
                }
                return [TextContent(type="text", text=json.dumps(summary, indent=2))]

            # ── get_pull_request_diff ─────────────────────────────────────
            elif name == "get_pull_request_diff":
                diff = await client.get_pull_request_diff(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                return [TextContent(type="text", text=diff)]

            # ── get_file_content ──────────────────────────────────────────
            elif name == "get_file_content":
                workspace = arguments["workspace"]
                repo_slug = arguments["repo_slug"]
                file_path = arguments["file_path"]
                ref = arguments.get("ref")
                pr_id = arguments.get("pr_id")

                INVALID_REFS = {
                    None,
                    "",
                    "main",
                    "source_commit",  # ← agent passes field name instead of value
                    "source_branch",  # ← other common literal placeholders
                    "commit_hash",
                    "branch_name",
                    "ref",
                }

                if pr_id:
                    try:
                        resolved_ref = await client.get_pr_source_ref(
                            workspace, repo_slug, int(pr_id)
                        )
                        if resolved_ref:
                            if ref in INVALID_REFS:
                                logger.info(
                                    f"Ref='{ref}' is a placeholder — overriding with "
                                    f"PR #{pr_id} source commit: '{resolved_ref}'"
                                )
                            else:
                                logger.info(
                                    f"pr_id provided — using PR #{pr_id} source "
                                    f"commit '{resolved_ref}' (agent passed ref='{ref}')"
                                )
                            ref = resolved_ref
                    except Exception as ref_err:
                        logger.warning(
                            f"Could not auto-resolve ref from PR #{pr_id}: {ref_err}. "
                            f"Falling back to ref='{ref or 'main'}'"
                        )

                # Final fallback — only reached if no pr_id was provided
                if ref in INVALID_REFS:
                    ref = "main"

                content = await client.get_file_content(
                    workspace,
                    repo_slug,
                    file_path,
                    ref,
                )
                return [TextContent(type="text", text=content)]

            # ── list_pull_request_comments ────────────────────────────────
            elif name == "list_pull_request_comments":
                comments = await client.list_pull_request_comments(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                summary = [
                    {
                        "id": c.get("id"),
                        "author": c.get("user", {}).get("display_name"),
                        "content": c.get("content", {}).get("raw", ""),
                        "created_on": c.get("created_on"),
                        "inline": c.get("inline"),
                    }
                    for c in comments
                ]
                return [TextContent(type="text", text=json.dumps(summary, indent=2))]

            # ── add_pull_request_comment ──────────────────────────────────
            elif name == "add_pull_request_comment":
                result = await client.add_pull_request_comment(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                    arguments["content"],
                    arguments.get("inline_path"),
                    arguments.get("inline_line"),
                )
                return [
                    TextContent(
                        type="text",
                        text=f"Comment posted successfully. ID: {result.get('id')}",
                    )
                ]

            # ── update_pull_request_description ──────────────────────────
            elif name == "update_pull_request_description":
                await client.update_pull_request_description(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                    arguments["description"],
                    arguments.get("title"),
                )
                return [
                    TextContent(
                        type="text", text="PR description updated successfully."
                    )
                ]

            # ── Unknown tool ──────────────────────────────────────────────
            else:
                logger.warning(f"Unknown tool requested: {name}")
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        # ── Error handling — NEVER raise, always return TextContent ────────
        except ValueError as e:
            logger.warning(f"Validation error in {name}: {e}")
            return [TextContent(type="text", text=f"Error: {str(e)}")]

        except PermissionError as e:
            logger.error(f"Permission error in {name}: {e}")
            return [TextContent(type="text", text=f"Permission denied: {str(e)}")]

        except Exception as e:
            # CRITICAL: catching here prevents the exception from propagating
            # to Starlette's ServerErrorMiddleware, which would attempt to
            # send an HTTP 500 response on an already-open SSE stream,
            # crashing the ASGI app with:
            # RuntimeError: Unexpected ASGI message 'http.response.start'
            logger.exception(f"Unexpected error in {name}")
            return [
                TextContent(
                    type="text",
                    text=f"Internal error executing '{name}': {str(e)}. Please try again.",
                )
            ]

    return server
