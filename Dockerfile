# ============================================================
# Dockerfile — Bitbucket MCP Server for Glean
# Multi-stage build for minimal, secure production image
# ============================================================

# ── Stage 1: Builder ────────────────────────────────────────
# Install dependencies in an isolated build stage so build
# tools and caches never end up in the final image.
FROM python:3.12-slim AS builder

# Install uv for fast, deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Compile bytecode in the build stage for faster cold starts
ENV UV_COMPILE_BYTECODE=1

# Use copy mode so the venv is portable across stages
ENV UV_LINK_MODE=copy

WORKDIR /build

# Copy ALL project files before sync — uv treats the project
# itself as a dependency, so it needs pyproject.toml AND src/
# to resolve the dependency graph.
COPY pyproject.toml uv.lock* ./
COPY src/ src/

# Install ONLY the third-party dependencies, not the project
# itself. We run the code directly via `python -m src.server`,
# so there's no need for uv to build/install our package
# (which would require a [build-system] in pyproject.toml).
#
#   --no-install-project → skip building & installing *this* project
#   --no-dev             → exclude test/dev packages
#
# NOTE: Once you have a uv.lock committed to git, add --frozen
# for fully reproducible builds.
RUN uv sync --no-install-project --no-dev --frozen

# ── Stage 2: Runtime ────────────────────────────────────────
# Minimal runtime image — no build tools, no package caches
FROM python:3.12-slim AS runtime

# Security: Create a non-root user
# Running as root inside a container is a common vulnerability.
# If the process is compromised, the attacker gets root in the
# container (and potentially escapes to the host).
RUN groupadd --gid 1001 mcpuser && \
  useradd --uid 1001 --gid 1001 --shell /bin/false mcpuser

WORKDIR /app

# Copy the virtual environment from the builder stage
# This contains only production dependencies — no pip, no gcc
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/src /app/src

# Ensure the venv's Python is on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Don't buffer Python output — ensures logs appear immediately
# in Docker logs / CloudWatch / Cloud Logging
ENV PYTHONUNBUFFERED=1

# Don't write .pyc files — saves disk and avoids permission issues
ENV PYTHONDONTWRITEBYTECODE=1

# Default configuration (override at runtime via env vars)
ENV MCP_SERVER_HOST=0.0.0.0
ENV MCP_SERVER_PORT=8080
ENV LOG_LEVEL=INFO

# Expose the server port
EXPOSE 8080

# Docker HEALTHCHECK — used by orchestrators (ECS, Cloud Run,
# Kubernetes) to know if the container is ready for traffic
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Drop to non-root user before running the server
USER mcpuser

# Start the MCP server
# Using the module syntax ensures proper signal handling (SIGTERM)
CMD ["python", "-m", "src.server"]