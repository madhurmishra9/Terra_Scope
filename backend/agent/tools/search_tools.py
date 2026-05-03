"""
search_tools.py — Semantic search over indexed Terraform code.
Uses ChromaDB + nomic-embed-text (via Ollama) for retrieval.
All search is SCOPED — never cross-contaminates repos or tags.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional
import chromadb
from chromadb.config import Settings

from backend.config import get_config, RepoConfig
from backend.agent.models import SourceReference


_chroma_client: Optional[chromadb.ClientAPI] = None


def get_chroma_client() -> chromadb.ClientAPI:
    global _chroma_client
    if _chroma_client is None:
        cfg = get_config()
        base = Path(__file__).parent.parent.parent.parent
        persist_path = cfg.vector_store.resolved_path(base)
        _chroma_client = chromadb.PersistentClient(
            path=str(persist_path),
            settings=Settings(anonymized_telemetry=False),
        )
    return _chroma_client


def _collection_name(repo_name: str, tag: str) -> str:
    """
    ChromaDB collection name: repo + tag, sanitized.
    Max 63 chars (ChromaDB limit).
    """
    raw = f"{repo_name}__{tag}".replace("-", "_").replace(".", "_").replace("/", "_")
    if len(raw) > 63:
        suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
        raw = raw[:54] + "_" + suffix
    return raw.lower()


def _get_embedding_fn():
    """Return ChromaDB-compatible embedding function using Ollama."""
    cfg = get_config()
    try:
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
        return OllamaEmbeddingFunction(
            url=cfg.llm.base_url.rstrip("/") + "/api/embeddings",
            model_name=cfg.llm.embedding_model,
        )
    except Exception:
        # Fallback: default embeddings (less accurate but functional)
        print("[search] Warning: Ollama embedding fn unavailable, using default")
        return chromadb.utils.embedding_functions.DefaultEmbeddingFunction()


def semantic_search(
    query: str,
    repo_name: str,
    tag: str,
    n_results: int = 8,
) -> list[SourceReference]:
    """
    Semantic search scoped to a specific repo+tag collection.
    Returns ranked SourceReference objects with code snippets + file citations.
    """
    client = get_chroma_client()
    col_name = _collection_name(repo_name, tag)

    try:
        collection = client.get_collection(
            name=col_name,
            embedding_function=_get_embedding_fn(),
        )
    except Exception:
        return []  # Not indexed yet

    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    sources: list[SourceReference] = []
    if not results["documents"] or not results["documents"][0]:
        return sources

    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        # Convert distance → relevance score (cosine: lower distance = higher relevance)
        relevance = max(0.0, 1.0 - distance)

        sources.append(SourceReference(
            repo_name=repo_name,
            file_path=meta.get("file_path", "unknown"),
            tag=meta.get("tag", tag),
            line_start=int(meta.get("line_start", 0)),
            line_end=int(meta.get("line_end", 0)),
            snippet=doc[:1500],
            relevance=round(relevance, 3),
        ))

    # Sort by relevance descending
    sources.sort(key=lambda s: s.relevance, reverse=True)
    return sources


def search_across_repos(
    query: str,
    tag_per_repo: dict[str, str],  # {repo_name: tag}
    n_per_repo: int = 4,
) -> list[SourceReference]:
    """Search across multiple repos simultaneously."""
    all_sources: list[SourceReference] = []
    for repo_name, tag in tag_per_repo.items():
        sources = semantic_search(query, repo_name, tag, n_per_repo)
        all_sources.extend(sources)
    all_sources.sort(key=lambda s: s.relevance, reverse=True)
    return all_sources


def is_indexed(repo_name: str, tag: str) -> bool:
    """Check if a repo+tag combination has been indexed."""
    client = get_chroma_client()
    col_name = _collection_name(repo_name, tag)
    try:
        col = client.get_collection(col_name)
        return col.count() > 0
    except Exception:
        return False


def get_indexed_tags(repo_name: str) -> list[str]:
    """Return all tags that have been indexed for a repo."""
    client = get_chroma_client()
    prefix = repo_name.replace("-", "_").replace(".", "_").lower() + "__"
    try:
        collections = client.list_collections()
        tags = []
        for col in collections:
            name = col.name if hasattr(col, "name") else str(col)
            if name.startswith(prefix):
                tag_part = name[len(prefix):]
                # Reverse the sanitization (best effort)
                tags.append(tag_part.replace("_", "."))
        return tags
    except Exception:
        return []


def get_chunk_count(repo_name: str, tag: str) -> int:
    client = get_chroma_client()
    col_name = _collection_name(repo_name, tag)
    try:
        return client.get_collection(col_name).count()
    except Exception:
        return 0
