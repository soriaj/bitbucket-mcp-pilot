"""
MCP Tool definitions for Bitbucket PR Review.

Each tool corresponds to a step in the PR review workflow:
1. get_pull_request → Fetch PR metadata
2. get_pull_request_diff → Fetch code changes
3. get_file_content → Read style guides / docs
4. list_pull_request_comments → See existing review comments
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

    # ── Tool Discovery ──────────────────────────────────

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
                    "Use this as the first step when reviewing a PR."
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
                    "Read the content of a file from a Bitbucket "
                    "repository. Useful for fetching style guides, "
                    "linting configurations, CONTRIBUTING.md, or "
                    "other documentation that informs the review."
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
                            "description": "Path to the file in repo",
                        },
                        "ref": {
                            "type": "string",
                            "description": ("Branch or commit ref (default: main)"),
                            "default": "main",
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
        ]

    # ── Tool Execution ──────────────────────────────────

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """
        Execute a tool call from Glean's Agent Builder.

        Each tool call is logged for audit purposes.
        Errors are caught and returned as structured messages.
        """
        logger.info(
            f"Tool called: {name} with args: " f"{json.dumps(arguments, default=str)}"
        )

        try:
            if name == "get_pull_request":
                result = await client.get_pull_request(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                # Return a clean summary for the LLM
                summary = {
                    "title": result.get("title"),
                    "description": result.get("description"),
                    "state": result.get("state"),
                    "author": result.get("author", {}).get("display_name"),
                    "source_branch": result.get("source", {})
                    .get("branch", {})
                    .get("name"),
                    "destination_branch": result.get("destination", {})
                    .get("branch", {})
                    .get("name"),
                    "reviewers": [
                        r.get("display_name") for r in result.get("reviewers", [])
                    ],
                    "created_on": result.get("created_on"),
                    "updated_on": result.get("updated_on"),
                    "comment_count": result.get("comment_count"),
                    "link": result.get("links", {}).get("html", {}).get("href"),
                }
                return [TextContent(type="text", text=json.dumps(summary, indent=2))]

            elif name == "get_pull_request_diff":
                diff = await client.get_pull_request_diff(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                return [TextContent(type="text", text=diff)]

            elif name == "get_file_content":
                content = await client.get_file_content(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["file_path"],
                    arguments.get("ref", "main"),
                )
                return [TextContent(type="text", text=content)]

            elif name == "list_pull_request_comments":
                comments = await client.list_pull_request_comments(
                    arguments["workspace"],
                    arguments["repo_slug"],
                    arguments["pr_id"],
                )
                # Summarize comments for the LLM
                summary = []
                for c in comments:
                    summary.append(
                        {
                            "id": c.get("id"),
                            "author": c.get("user", {}).get("display_name"),
                            "content": c.get("content", {}).get("raw", ""),
                            "created_on": c.get("created_on"),
                            "inline": c.get("inline"),
                        }
                    )
                return [TextContent(type="text", text=json.dumps(summary, indent=2))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except ValueError as e:
            logger.warning(f"Validation error in {name}: {e}")
            return [TextContent(type="text", text=f"Error: {str(e)}")]
        except PermissionError as e:
            logger.error(f"Permission error in {name}: {e}")
            return [TextContent(type="text", text=f"Permission denied: {str(e)}")]
        except Exception as e:
            logger.exception(f"Unexpected error in {name}")
            return [
                TextContent(
                    type="text",
                    text=f"Internal error executing {name}. " f"Please try again.",
                )
            ]

    return server
