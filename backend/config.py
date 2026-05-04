"""
config.py — Loads terrascope.config.yaml into typed Pydantic models.
Handles Windows/Mac path normalization automatically.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ── Path of the config file ────────────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent.parent / "terrascope.config.yaml"


# ── Sub-models ─────────────────────────────────────────────────────────────────

class LLMConfig(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "gemma3:4b"
    embedding_model: str = "nomic-embed-text"
    temperature: float = 0.0
    max_tokens: int = 2048
    context_window: int = 8192


class GroundingConfig(BaseModel):
    mode: str = "strict"
    require_source_citation: bool = True
    min_confidence_threshold: float = 0.65
    max_retrieval_chunks: int = 8
    chunk_overlap_tokens: int = 50


class VectorStoreConfig(BaseModel):
    type: str = "chromadb"
    persist_path: str = "./data/chromadb"

    def resolved_path(self, base: Path) -> Path:
        p = Path(self.persist_path)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = True


class UIConfig(BaseModel):
    port: int = 5173
    theme: str = "dark"


class GCPProductMeta(BaseModel):
    apis: list[str] = []
    common_roles: list[str] = []
    provider_resources: list[str] = []


class RepoConfig(BaseModel):
    name: str
    display_name: str
    local_path: str
    gcp_product: str = "unknown"
    description: str = ""
    enabled: bool = True

    @field_validator("local_path", mode="before")
    @classmethod
    def normalize_path(cls, v: str) -> str:
        """Convert backslashes on Windows, expand ~, resolve env vars."""
        v = os.path.expandvars(os.path.expanduser(str(v)))
        return str(Path(v))

    def resolved_local_path(self, base: Path) -> Path:
        p = Path(self.local_path)
        if not p.is_absolute():
            p = base / p
        return p.resolve()

    @property
    def collection_name(self) -> str:
        """Safe ChromaDB collection name from repo name."""
        return self.name.replace("-", "_").replace(".", "_").lower()


class TerrascopeInner(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


class TerrascopeConfig(BaseModel):
    terrascope: TerrascopeInner = Field(default_factory=TerrascopeInner)
    repos: list[RepoConfig] = []
    gcp_products: dict[str, GCPProductMeta] = {}

    # Convenience shortcuts
    @property
    def llm(self) -> LLMConfig:
        return self.terrascope.llm

    @property
    def grounding(self) -> GroundingConfig:
        return self.terrascope.grounding

    @property
    def vector_store(self) -> VectorStoreConfig:
        return self.terrascope.vector_store

    @property
    def server(self) -> ServerConfig:
        return self.terrascope.server

    @property
    def enabled_repos(self) -> list[RepoConfig]:
        return [r for r in self.repos if r.enabled]

    def get_repo(self, name: str) -> Optional[RepoConfig]:
        return next((r for r in self.repos if r.name == name), None)

    def get_gcp_product_meta(self, product: str) -> Optional[GCPProductMeta]:
        return self.gcp_products.get(product)


def load_config(path: Path = CONFIG_FILE) -> TerrascopeConfig:
    """Load and validate terrascope.config.yaml."""
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            "Copy terrascope.config.yaml.example to terrascope.config.yaml and edit it."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config = TerrascopeConfig.model_validate(raw)

    # Validate repos exist on disk
    base = path.parent
    for repo in config.enabled_repos:
        resolved = repo.resolved_local_path(base)
        if not resolved.exists():
            print(f"  [WARN]  Repo '{repo.name}' not found at: {resolved}")
            print(f"     Clone it first or disable it in terrascope.config.yaml")

    return config


# Singleton
_config: Optional[TerrascopeConfig] = None

def get_config() -> TerrascopeConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config
