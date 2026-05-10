"""
module_fetcher.py — Load Terraform module files from various sources.

Supported:
  - GitHub URL  (clones to temp dir)
  - Local path  (reads .tf files recursively)
  - ZIP bytes   (extracts to temp dir, then reads .tf files)
  - Raw .tf text dict (caller already loaded the files)
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

_TEMP_BASE = Path(tempfile.gettempdir()) / "terrascope_curator"


def _temp_dir(tag: str) -> Path:
    d = _TEMP_BASE / tag
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_tf_files(directory: Path) -> dict[str, str]:
    """Recursively collect all .tf files under directory.
    Returns {relative_path: content}.
    """
    result: dict[str, str] = {}
    for tf in sorted(directory.rglob("*.tf")):
        try:
            rel = str(tf.relative_to(directory)).replace("\\", "/")
            result[rel] = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return result


def extract_module_sources(tf_files: dict[str, str]) -> list[str]:
    """Pull module `source` values from HCL content.
    Ignores local (./... or ../...) paths — only external sources.
    """
    pattern = re.compile(r'source\s*=\s*"([^"]+)"', re.MULTILINE)
    sources: set[str] = set()
    for content in tf_files.values():
        for m in pattern.finditer(content):
            src = m.group(1)
            if not src.startswith("./") and not src.startswith("../"):
                sources.add(src)
    return list(sources)


# ── Source loaders ────────────────────────────────────────────────────────────

def fetch_from_local(local_path: str) -> dict[str, str]:
    p = Path(local_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"Not a directory: {p}")
    return load_tf_files(p)


def fetch_from_zip(zip_bytes: bytes, session_tag: str = "zip") -> dict[str, str]:
    dest = _temp_dir(session_tag)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(dest)
    return load_tf_files(dest)


def fetch_from_github(url: str, git_tag: Optional[str] = None, session_tag: str = "gh") -> dict[str, str]:
    """Clone a GitHub repository (shallow) and load its .tf files."""
    from git import Repo, GitCommandError

    dest = _temp_dir(session_tag)
    try:
        repo = Repo.clone_from(url, str(dest), depth=1)
        if git_tag:
            repo.git.checkout(git_tag)
    except GitCommandError as exc:
        raise ValueError(f"Cannot clone {url}: {exc}") from exc

    return load_tf_files(dest)


def fetch_from_uploaded_tf(files: dict[str, str]) -> dict[str, str]:
    """Accept a dict of filename → raw HCL text uploaded directly by the user."""
    return {k: v for k, v in files.items() if k.endswith(".tf")}
