"""
config.py — Confluence connection settings loaded from a .env file.

Required keys
-------------
  CONFLUENCE_BASE_URL     https://<domain>/wiki  (Cloud) or https://<domain> (DC/Server)
  CONFLUENCE_API_TOKEN    API token or Personal Access Token (PAT)

Optional keys
-------------
  CONFLUENCE_EMAIL        If set  → HTTP Basic auth  (email:token)
                          If absent → Bearer auth  (token as PAT/Bearer)
  CONFLUENCE_SPACE_KEY    Target space; empty → publish to user's private (~) space
  CONFLUENCE_PARENT_TITLE Title of the parent page under which product pages are
                          nested (default: "TerraScope Modules")

Fail-fast: get_confluence_settings() raises ValueError on first missing required key.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfluenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    confluence_base_url:     str = ""
    confluence_api_token:    str = ""
    confluence_email:        str = ""
    confluence_space_key:    str = ""
    confluence_parent_title: str = "TerraScope Modules"

    @field_validator("confluence_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _validate_required(self) -> "ConfluenceSettings":
        if not self.confluence_base_url:
            raise ValueError(
                "CONFLUENCE_BASE_URL is not set. "
                "Add it to your .env file or environment."
            )
        if not self.confluence_api_token:
            raise ValueError(
                "CONFLUENCE_API_TOKEN is not set. "
                "Add it to your .env file or environment."
            )
        return self

    @property
    def auth_mode(self) -> str:
        """'basic' when CONFLUENCE_EMAIL is provided, otherwise 'bearer'."""
        return "basic" if self.confluence_email else "bearer"

    @property
    def api_base(self) -> str:
        """Resolved REST API root (Cloud or DC/Server)."""
        base = self.confluence_base_url
        # Cloud: base already ends in /wiki → rest/api lives at /wiki/rest/api
        # DC/Server: base is the site root → rest/api lives at /rest/api
        if base.endswith("/wiki"):
            return f"{base}/rest/api"
        return f"{base}/wiki/rest/api"


@lru_cache(maxsize=1)
def get_confluence_settings() -> ConfluenceSettings:
    """Return a validated ConfluenceSettings singleton. Raises on misconfiguration."""
    return ConfluenceSettings()


def check_confluence_config() -> tuple[bool, str]:
    """Return (ok, message) without raising — suitable for health-check endpoints."""
    try:
        s = get_confluence_settings()
        return True, (
            f"Confluence OK: {s.confluence_base_url} "
            f"({s.auth_mode} auth, "
            f"space={'<private>' if not s.confluence_space_key else s.confluence_space_key})"
        )
    except Exception as exc:
        return False, str(exc)
