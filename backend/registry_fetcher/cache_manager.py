"""
cache_manager.py — Local disk cache for Terraform provider documentation.
Cache lives at ./data/registry_cache/{provider}/{resource_slug}.json
TTL is 72 hours; after that docs are re-fetched when network is available.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


CACHE_TTL_HOURS = 72
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _cache_dir() -> Path:
    d = _PROJECT_ROOT / "data" / "registry_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(provider: str, slug: str) -> Path:
    d = _cache_dir() / provider
    d.mkdir(exist_ok=True)
    return d / f"{slug}.json"


def get_cached_doc(provider: str, slug: str) -> Optional[str]:
    path = _cache_path(provider, slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.utcnow() - cached_at > timedelta(hours=CACHE_TTL_HOURS):
            return None
        return data.get("content")
    except Exception:
        return None


def save_cached_doc(provider: str, slug: str, content: str) -> None:
    path = _cache_path(provider, slug)
    path.write_text(
        json.dumps({"cached_at": datetime.utcnow().isoformat(), "content": content}, indent=2),
        encoding="utf-8",
    )


def list_cached_slugs(provider: str) -> list[str]:
    d = _cache_dir() / provider
    if not d.exists():
        return []
    return [p.stem for p in d.glob("*.json")]


def cache_stats() -> dict:
    root = _cache_dir()
    stats: dict[str, int] = {}
    for provider_dir in root.iterdir():
        if provider_dir.is_dir():
            stats[provider_dir.name] = len(list(provider_dir.glob("*.json")))
    return stats
