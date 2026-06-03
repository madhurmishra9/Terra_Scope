"""
confluence/client.py — Minimal Confluence REST API client.

Supports
--------
  Confluence Cloud     (https://<domain>.atlassian.net/wiki/...)
  Confluence DC/Server (https://<domain>/...)

Auth (auto-detected from ConfluenceSettings.auth_mode)
------
  "basic"  → HTTP Basic: email + API token
  "bearer" → Bearer / PAT token in Authorization header

All methods raise httpx.HTTPStatusError on 4xx/5xx.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from backend.module_curator.docgen.config import ConfluenceSettings


class ConfluenceClient:
    def __init__(self, settings: Optional[ConfluenceSettings] = None) -> None:
        from backend.module_curator.docgen.config import get_confluence_settings
        self._s   = settings or get_confluence_settings()
        self._api = self._s.api_base

        if self._s.auth_mode == "basic":
            self._auth    = httpx.BasicAuth(self._s.confluence_email, self._s.confluence_api_token)
            self._headers = {"Content-Type": "application/json", "Accept": "application/json"}
        else:
            self._auth    = None
            self._headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._s.confluence_api_token}",
            }

    # ── Pages ─────────────────────────────────────────────────────────────────

    def get_page_by_title(self, space_key: str, title: str) -> Optional[dict]:
        """Return the page dict (id, version, …) or None if not found."""
        resp = self._get(
            f"{self._api}/content",
            params={"type": "page", "spaceKey": space_key, "title": title, "expand": "version"},
        )
        results = resp.get("results", [])
        return results[0] if results else None

    def create_page(
        self,
        space_key: str,
        title: str,
        body_html: str,
        parent_id: Optional[str] = None,
    ) -> str:
        """Create a page and return its string ID."""
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {"storage": {"value": body_html, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": parent_id}]
        resp = self._post(f"{self._api}/content", payload)
        return str(resp["id"])

    def update_page(
        self,
        page_id: str,
        title: str,
        body_html: str,
        current_version: int,
    ) -> str:
        """Update an existing page (bumps version) and return its ID."""
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "version": {"number": current_version + 1},
            "body": {"storage": {"value": body_html, "representation": "storage"}},
        }
        resp = self._put(f"{self._api}/content/{page_id}", payload)
        return str(resp["id"])

    def get_page_version(self, page_id: str) -> int:
        resp = self._get(f"{self._api}/content/{page_id}", params={"expand": "version"})
        return int(resp.get("version", {}).get("number", 1))

    def add_label(self, page_id: str, label: str) -> None:
        self._post(
            f"{self._api}/content/{page_id}/label",
            [{"prefix": "global", "name": label}],
        )

    # ── Attachments ───────────────────────────────────────────────────────────

    def upload_attachment(
        self,
        page_id: str,
        filename: str,
        data: bytes,
        content_type: str = "image/png",
    ) -> str:
        """Upload or replace an attachment; return attachment ID."""
        url = f"{self._api}/content/{page_id}/child/attachment"
        # X-Atlassian-Token header required to bypass XSRF check
        if self._s.auth_mode == "basic":
            auth    = self._auth
            headers = {"X-Atlassian-Token": "no-check"}
        else:
            auth    = None
            headers = {
                "Authorization": f"Bearer {self._s.confluence_api_token}",
                "X-Atlassian-Token": "no-check",
            }
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                url,
                auth=auth,
                headers=headers,
                files={"file": (filename, data, content_type)},
            )
            resp.raise_for_status()
            result = resp.json()
            results = result.get("results", [result])
            return str(results[0]["id"]) if results else ""

    # ── Spaces ────────────────────────────────────────────────────────────────

    def get_space(self, space_key: str) -> Optional[dict]:
        try:
            return self._get(f"{self._api}/space/{space_key}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def create_space(self, key: str, name: str, description: str = "") -> dict:
        payload: dict[str, Any] = {
            "key": key,
            "name": name,
            "description": {
                "plain": {"value": description, "representation": "plain"}
            },
        }
        return self._post(f"{self._api}/space", payload)

    def get_current_user(self) -> dict:
        return self._get(f"{self._api}/user/current")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, auth=self._auth, headers=self._headers, params=params)
            resp.raise_for_status()
            return resp.json()

    def _post(self, url: str, payload: Any) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                url, auth=self._auth, headers=self._headers,
                content=json.dumps(payload),
            )
            resp.raise_for_status()
            return resp.json()

    def _put(self, url: str, payload: Any) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.put(
                url, auth=self._auth, headers=self._headers,
                content=json.dumps(payload),
            )
            resp.raise_for_status()
            return resp.json()
