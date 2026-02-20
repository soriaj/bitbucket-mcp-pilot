"""
Configuration management using environment variables.
All secrets are loaded from env vars — never hardcoded.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Bitbucket OAuth
    bitbucket_client_id: str
    bitbucket_client_secret: str
    bitbucket_api_base: str = "https://api.bitbucket.org/2.0"
    bitbucket_auth_url: str = "https://bitbucket.org/site/oauth2"

    # Server
    mcp_server_host: str = "0.0.0.0"
    mcp_server_port: int = 8080
    mcp_server_name: str = "bitbucket-pr-review"

    # Inbound authentication (clients -> this server)
    auth_mode: str = "none"  # "none" or "glean_only"
    glean_instance: str = ""  # Required if auth_mode = "glean_only"

    # Security
    allowed_origins: str = ""
    log_level: str = "INFO"
    max_chars: int = 300_000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
