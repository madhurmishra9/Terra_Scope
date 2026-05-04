"""
git_tools.py — Git operations for reading repo content at any tag.
Works on Windows and Mac via GitPython.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from git import Repo, GitCommandError, InvalidGitRepositoryError
from pydantic_ai import RunContext

from backend.config import get_config, RepoConfig
from backend.agent.models import QueryRequest


def _get_repo(repo_cfg: RepoConfig) -> Repo:
    path = repo_cfg.resolved_local_path(
        Path(__file__).parent.parent.parent.parent
    )
    if not path.exists():
        raise FileNotFoundError(f"Repo path not found: {path}")
    return Repo(str(path))


def list_tags_for_repo(repo_name: str) -> list[str]:
    """
    List all Git tags for a repo, newest first.
    Returns [] if repo is not found or not a git repo.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return []
    try:
        repo = _get_repo(repo_cfg)
        tags = sorted(
            [t.name for t in repo.tags],
            key=lambda t: _parse_semver(t),
            reverse=True,
        )
        return tags
    except (InvalidGitRepositoryError, FileNotFoundError, GitCommandError) as e:
        print(f"[git] Error listing tags for {repo_name}: {e}")
        return []


def get_latest_tag(repo_name: str) -> Optional[str]:
    tags = list_tags_for_repo(repo_name)
    return tags[0] if tags else None


def get_file_at_tag(repo_name: str, tag: str, file_path: str) -> Optional[str]:
    """
    Read the content of a file at a specific Git tag.
    Returns None if file or tag does not exist.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return None
    try:
        repo = _get_repo(repo_cfg)
        # Normalize path separators (Windows uses \)
        file_path = file_path.replace("\\", "/")
        commit = repo.commit(tag)
        blob = commit.tree / file_path
        return blob.data_stream.read().decode("utf-8", errors="replace")
    except (KeyError, GitCommandError, Exception) as e:
        print(f"[git] Cannot read {file_path}@{tag} in {repo_name}: {e}")
        return None


def list_tf_files_at_tag(repo_name: str, tag: str) -> list[str]:
    """
    List all .tf files in the repo at a given tag.
    Returns relative paths (always using forward slashes).
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return []
    try:
        repo = _get_repo(repo_cfg)
        commit = repo.commit(tag)
        tf_files = []
        for blob in commit.tree.traverse():
            if hasattr(blob, "path") and blob.path.endswith(".tf"):
                tf_files.append(blob.path.replace("\\", "/"))
        return sorted(tf_files)
    except Exception as e:
        print(f"[git] Cannot list files@{tag} in {repo_name}: {e}")
        return []


def diff_tags(repo_name: str, tag_a: str, tag_b: str) -> str:
    """
    Diff two tags, filtered to .tf files only.
    Returns a readable unified diff string (truncated to 6000 chars).
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return f"Repo '{repo_name}' not found."
    try:
        repo = _get_repo(repo_cfg)
        diff = repo.git.diff(tag_a, tag_b, "--", "*.tf")
        if len(diff) > 6000:
            diff = diff[:6000] + "\n... [diff truncated] ..."
        return diff or "No .tf file changes between these tags."
    except GitCommandError as e:
        return f"Git diff error: {e}"


def get_changelog(repo_name: str, tag: str) -> str:
    """Read CHANGELOG.md at a specific tag if it exists."""
    for candidate in ["CHANGELOG.md", "CHANGELOG", "CHANGES.md"]:
        content = get_file_at_tag(repo_name, tag, candidate)
        if content:
            return content[:4000]
    return "No CHANGELOG found at this tag."


def _parse_semver(tag: str) -> tuple:
    """Parse v1.2.3 into (1, 2, 3) for correct sorting."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", tag)
    if match:
        return tuple(int(x) for x in match.groups())
    return (0, 0, 0)
