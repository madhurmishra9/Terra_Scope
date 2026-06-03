"""
confluence/space.py — Resolve (or create) a Confluence space.

Rules
-----
  CONFLUENCE_SPACE_KEY set   → verify it exists; raise RuntimeError if not found
  CONFLUENCE_SPACE_KEY empty → find the user's private space (~<accountId>)
                                or create one named "<DisplayName>'s Space"
"""
from __future__ import annotations

from backend.module_curator.docgen.confluence.client import ConfluenceClient


def resolve_space(client: ConfluenceClient, space_key: str = "") -> str:
    """
    Return a verified, usable Confluence space key.
    Creates a private space automatically when *space_key* is empty.
    Raises RuntimeError if a configured key does not exist.
    """
    if space_key:
        space = client.get_space(space_key)
        if space is None:
            raise RuntimeError(
                f"Confluence space '{space_key}' not found. "
                "Check CONFLUENCE_SPACE_KEY in your .env file."
            )
        return space_key

    return _get_or_create_private_space(client)


def _get_or_create_private_space(client: ConfluenceClient) -> str:
    try:
        user = client.get_current_user()
    except Exception as exc:
        raise RuntimeError(f"Could not identify the Confluence user: {exc}") from exc

    # Cloud uses accountId; DC/Server uses username or userKey
    account_id: str = (
        user.get("accountId")
        or user.get("username")
        or user.get("userKey")
        or "terrascope"
    )
    private_key = f"~{account_id}"

    if client.get_space(private_key):
        return private_key

    # Create the private space
    display_name = user.get("displayName", "TerraScope")
    client.create_space(
        key=private_key,
        name=f"{display_name}'s Space",
        description="Auto-created by TerraScope for module documentation.",
    )
    return private_key
