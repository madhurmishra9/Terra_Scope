"""
settings_manager.py — Read and write TerraScope runtime configuration.

Handles two files:
  terrascope.config.yaml   → LLM, server, UI, grounding settings
  .env                     → Confluence credentials (secrets kept out of YAML)

Rules
-----
  • Never exposes raw secret values over the API — token fields are masked.
  • Re-reads from disk on every call so UI changes are immediately visible
    to other callers without a server restart (config singleton is reset).
  • YAML rewrites preserve top-level structure but strip inline comments
    (acceptable tradeoff; structural comments like section headers survive
    because they sit on their own lines as blank-separated keys).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

_ROOT = Path(__file__).parent.parent          # terra_scope/
_YAML = _ROOT / "terrascope.config.yaml"
_ENV  = _ROOT / ".env"


# ── .env helpers ─────────────────────────────────────────────────────────────

class EnvManager:
    """Simple .env file reader/writer that preserves unrelated keys."""

    def __init__(self, path: Path = _ENV) -> None:
        self._path = path

    def read(self) -> dict[str, str]:
        """Return all key=value pairs, ignoring comments and blank lines."""
        result: dict[str, str] = {}
        if not self._path.exists():
            return result
        for line in self._path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, v = stripped.partition("=")
                result[k.strip()] = v.strip().strip('"').strip("'")
        return result

    def write(self, updates: dict[str, str]) -> None:
        """
        Update specific keys, preserving all other content exactly.
        Creates the file if it doesn't exist.
        """
        lines: list[str] = []
        existing_keys: set[str] = set()

        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, _ = stripped.partition("=")
                    k = k.strip()
                    if k in updates:
                        # Replace this line with the new value
                        lines.append(f"{k}={updates[k]}")
                        existing_keys.add(k)
                        continue
                lines.append(line)

        # Append any keys that didn't already exist
        new_keys = [k for k in updates if k not in existing_keys]
        if new_keys:
            if lines and lines[-1].strip():
                lines.append("")          # blank separator
            for k in new_keys:
                lines.append(f"{k}={updates[k]}")

        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── YAML helpers ──────────────────────────────────────────────────────────────

def _read_yaml() -> dict:
    if not _YAML.exists():
        return {}
    with open(_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(data: dict) -> None:
    """Write back, preserving structure (comments are lost — known limitation)."""
    with open(_YAML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False, indent=2)


def _nested_set(d: dict, keys: list[str], value: Any) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_settings() -> dict:
    """Return the full merged settings view (secrets masked)."""
    raw  = _read_yaml()
    ts   = raw.get("terrascope", {})
    env  = EnvManager().read()

    def mask(v: str) -> str:
        if not v:
            return ""
        return v[:4] + "•" * max(0, len(v) - 4) if len(v) > 8 else "•" * len(v)

    return {
        "llm": {
            "provider":        ts.get("llm", {}).get("provider", "ollama"),
            "base_url":        ts.get("llm", {}).get("base_url", "http://localhost:11434"),
            "model":           ts.get("llm", {}).get("model", "qwen2.5-coder:7b"),
            "embedding_model": ts.get("llm", {}).get("embedding_model", "nomic-embed-text"),
            "temperature":     ts.get("llm", {}).get("temperature", 0.0),
            "max_tokens":      ts.get("llm", {}).get("max_tokens", 2048),
            "disable_thinking": ts.get("llm", {}).get("disable_thinking", True),
        },
        "server": {
            "host":   ts.get("server", {}).get("host", "127.0.0.1"),
            "port":   ts.get("server", {}).get("port", 8000),
            "reload": ts.get("server", {}).get("reload", False),
        },
        "grounding": {
            "mode":                       ts.get("grounding", {}).get("mode", "strict"),
            "min_confidence_threshold":   ts.get("grounding", {}).get("min_confidence_threshold", 0.65),
            "max_retrieval_chunks":       ts.get("grounding", {}).get("max_retrieval_chunks", 8),
        },
        "confluence": {
            "base_url":     env.get("CONFLUENCE_BASE_URL", ""),
            "email":        env.get("CONFLUENCE_EMAIL", ""),
            "api_token":    mask(env.get("CONFLUENCE_API_TOKEN", "")),
            "api_token_set": bool(env.get("CONFLUENCE_API_TOKEN", "")),
            "space_key":    env.get("CONFLUENCE_SPACE_KEY", ""),
            "parent_title": env.get("CONFLUENCE_PARENT_TITLE", "TerraScope Modules"),
        },
    }


def update_llm_settings(updates: dict) -> dict:
    """Persist LLM settings back to terrascope.config.yaml."""
    allowed = {"provider", "base_url", "model", "embedding_model", "temperature", "max_tokens", "disable_thinking"}
    data = _read_yaml()
    llm_cfg = data.setdefault("terrascope", {}).setdefault("llm", {})
    for k, v in updates.items():
        if k in allowed:
            llm_cfg[k] = v
    _write_yaml(data)
    # Reset the singleton so the next get_config() picks up the changes
    _reset_config_singleton()
    return {"status": "ok", "updated": list(updates.keys())}


def update_server_settings(updates: dict) -> dict:
    """Persist server settings back to terrascope.config.yaml."""
    allowed = {"host", "port", "reload"}
    data = _read_yaml()
    srv = data.setdefault("terrascope", {}).setdefault("server", {})
    for k, v in updates.items():
        if k in allowed:
            srv[k] = v
    _write_yaml(data)
    _reset_config_singleton()
    return {"status": "ok", "message": "Server config saved — restart backend for port/host changes."}


def update_grounding_settings(updates: dict) -> dict:
    """Persist grounding settings back to terrascope.config.yaml."""
    allowed = {"mode", "min_confidence_threshold", "max_retrieval_chunks", "chunk_overlap_tokens"}
    data = _read_yaml()
    gr = data.setdefault("terrascope", {}).setdefault("grounding", {})
    for k, v in updates.items():
        if k in allowed:
            gr[k] = v
    _write_yaml(data)
    _reset_config_singleton()
    return {"status": "ok", "updated": list(updates.keys())}


def update_confluence_settings(updates: dict) -> dict:
    """Persist Confluence credentials to .env (creates file if absent)."""
    key_map = {
        "base_url":     "CONFLUENCE_BASE_URL",
        "email":        "CONFLUENCE_EMAIL",
        "api_token":    "CONFLUENCE_API_TOKEN",
        "space_key":    "CONFLUENCE_SPACE_KEY",
        "parent_title": "CONFLUENCE_PARENT_TITLE",
    }
    env_updates: dict[str, str] = {}
    for k, v in updates.items():
        env_key = key_map.get(k)
        if env_key and v is not None:
            env_updates[env_key] = str(v)

    EnvManager().write(env_updates)

    # Invalidate the Confluence settings cache so next call re-reads
    try:
        from backend.module_curator.docgen.config import get_confluence_settings
        get_confluence_settings.cache_clear()
    except Exception:
        pass

    return {"status": "ok", "updated": list(env_updates.keys())}


async def test_connections() -> dict:
    """Test Ollama and Confluence connections. Returns status per service."""
    import httpx

    results: dict[str, dict] = {}

    # ── Ollama ────────────────────────────────────────────────────────────────
    try:
        from backend.config import get_config
        cfg = get_config()
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{cfg.llm.base_url}/api/tags")
            results["ollama"] = {
                "ok": r.status_code == 200,
                "message": "Running" if r.status_code == 200 else f"HTTP {r.status_code}",
                "url": cfg.llm.base_url,
            }
    except Exception as e:
        results["ollama"] = {"ok": False, "message": str(e), "url": ""}

    # ── Confluence ────────────────────────────────────────────────────────────
    try:
        from backend.module_curator.docgen.config import get_confluence_settings
        from backend.module_curator.docgen.confluence.client import ConfluenceClient
        s = get_confluence_settings()
        client = ConfluenceClient(s)
        user = client.get_current_user()
        display = user.get("displayName") or user.get("username", "unknown")
        results["confluence"] = {
            "ok": True,
            "message": f"Connected as {display}",
            "url": s.confluence_base_url,
        }
    except Exception as e:
        results["confluence"] = {"ok": False, "message": str(e), "url": ""}

    return results


def _reset_config_singleton() -> None:
    try:
        import backend.config as cfg_mod
        cfg_mod._config = None
    except Exception:
        pass
