"""
loader.py — Thin wrapper around module_fetcher for the scenario generator.

Supports the same source options as the Curate "From Module" UI:
  - GitHub URL     (fetch_from_github)
  - Local path     (fetch_from_local)
  - ZIP bytes      (fetch_from_zip)
  - Pre-loaded .tf files dict  (fetch_from_uploaded_tf)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def load_from_github(url: str, tag: Optional[str] = None) -> dict[str, str]:
    """Clone a GitHub repo and return {rel_path: hcl_content}."""
    from backend.module_curator.module_fetcher import fetch_from_github
    return fetch_from_github(url, git_tag=tag, session_tag="scenario")


def load_from_local(path: str) -> dict[str, str]:
    """Load .tf files from a local directory."""
    from backend.module_curator.module_fetcher import fetch_from_local
    return fetch_from_local(path)


def load_from_zip(zip_bytes: bytes) -> dict[str, str]:
    """Extract a ZIP archive and return {rel_path: hcl_content}."""
    from backend.module_curator.module_fetcher import fetch_from_zip
    return fetch_from_zip(zip_bytes, session_tag="scenario")


def load_from_tf_files(files: dict[str, str]) -> dict[str, str]:
    """Accept an already-loaded dict of .tf files (e.g. from file upload)."""
    from backend.module_curator.module_fetcher import fetch_from_uploaded_tf
    return fetch_from_uploaded_tf(files)


def load_module(
    source_type: str,
    url: str = "",
    local_path: str = "",
    tag: Optional[str] = None,
    zip_bytes: Optional[bytes] = None,
    tf_files: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Unified loader — dispatches to the right fetcher based on source_type.

    source_type values: 'github' | 'local' | 'zip' | 'tf'
    """
    if source_type == "github":
        if not url:
            raise ValueError("url is required for source_type='github'")
        return load_from_github(url, tag=tag)
    if source_type == "local":
        return load_from_local(local_path or url)
    if source_type == "zip":
        if zip_bytes is None:
            raise ValueError("zip_bytes is required for source_type='zip'")
        return load_from_zip(zip_bytes)
    if source_type == "tf":
        return load_from_tf_files(tf_files or {})
    raise ValueError(f"Unknown source_type: {source_type!r}. Use 'github', 'local', 'zip', or 'tf'.")
