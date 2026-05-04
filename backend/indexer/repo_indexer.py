"""
repo_indexer.py — Indexes Terraform repo code into ChromaDB.

Chunking strategy:
  - HCL-aware: splits by resource/variable/output/data blocks
  - Each chunk = one logical block (not arbitrary line count)
  - Preserves file_path, line_start, line_end as metadata
  - Per-tag collections: never cross-contaminates versions

Run from repo root:
  python -m backend.indexer.repo_indexer              # index all enabled repos
  python -m backend.indexer.repo_indexer --repo NAME  # index one repo
  python -m backend.indexer.repo_indexer --force      # force re-index
"""
from __future__ import annotations

import argparse
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

import chromadb
from chromadb.config import Settings

from backend.config import get_config, RepoConfig, load_config
from backend.agent.tools.git_tools import (
    list_tags_for_repo, list_tf_files_at_tag, get_file_at_tag
)
from backend.agent.tools.search_tools import (
    get_chroma_client, _collection_name, _get_embedding_fn,
    is_indexed, get_chunk_count
)


# ── Chunking ──────────────────────────────────────────────────────────────────

# Matches the start of a top-level HCL block
BLOCK_PATTERN = re.compile(
    r'^(resource|variable|output|data|locals|module|provider|terraform)\s',
    re.MULTILINE
)


def chunk_tf_file(content: str, file_path: str, tag: str) -> list[dict]:
    """
    Split a .tf file into resource-block-level chunks.
    Each chunk is one logical HCL block (resource, variable, output, etc.)

    Returns list of:
      {text, file_path, tag, line_start, line_end, block_type, block_name}
    """
    if not content.strip():
        return []

    lines = content.splitlines()
    chunks = []

    # Find block start positions
    block_starts: list[int] = []
    for i, line in enumerate(lines):
        if BLOCK_PATTERN.match(line):
            block_starts.append(i)

    if not block_starts:
        # File has no top-level blocks (e.g., a pure locals file)
        # Treat the whole file as one chunk
        return [{
            "text": content[:3000],
            "file_path": file_path,
            "tag": tag,
            "line_start": 1,
            "line_end": len(lines),
            "block_type": "file",
            "block_name": file_path,
        }]

    # Split into block-level chunks
    for idx, start in enumerate(block_starts):
        end = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
        block_lines = lines[start:end]
        block_text = "\n".join(block_lines).strip()

        if len(block_text) < 10:
            continue

        # Extract block type and name
        first_line = block_lines[0]
        parts = first_line.split()
        block_type = parts[0] if parts else "unknown"
        block_name = " ".join(parts[1:3]).replace('"', '') if len(parts) > 1 else "unnamed"

        # Build the chunk text with rich prefix for better embeddings
        chunk_text = (
            f"File: {file_path} | Tag: {tag} | Type: {block_type} | Name: {block_name}\n"
            f"{block_text}"
        )

        chunks.append({
            "text": chunk_text[:4000],  # ChromaDB max doc size
            "file_path": file_path,
            "tag": tag,
            "line_start": start + 1,
            "line_end": end,
            "block_type": block_type,
            "block_name": block_name,
        })

    return chunks


def chunk_repo_at_tag(repo_name: str, tag: str) -> list[dict]:
    """Walk all .tf files at a tag and produce all chunks."""
    all_chunks = []
    tf_files = list_tf_files_at_tag(repo_name, tag)

    if not tf_files:
        print(f"  [WARN]  No .tf files found at {tag}")
        return []

    for file_path in tf_files:
        content = get_file_at_tag(repo_name, tag, file_path)
        if content:
            chunks = chunk_tf_file(content, file_path, tag)
            all_chunks.extend(chunks)

    return all_chunks


# ── ChromaDB ingestion ────────────────────────────────────────────────────────

def index_tag(repo_name: str, tag: str, force: bool = False) -> int:
    """
    Index all .tf files at a specific tag into ChromaDB.
    Returns number of chunks indexed.
    Skips if already indexed (unless force=True).
    """
    if is_indexed(repo_name, tag) and not force:
        count = get_chunk_count(repo_name, tag)
        print(f"  [OK] {repo_name}@{tag} already indexed ({count} chunks). Skipping.")
        return count

    print(f"  [..] Indexing {repo_name}@{tag}...")
    chunks = chunk_repo_at_tag(repo_name, tag)

    if not chunks:
        print(f"  [WARN]  No chunks produced for {repo_name}@{tag}")
        return 0

    client = get_chroma_client()
    col_name = _collection_name(repo_name, tag)
    emb_fn = _get_embedding_fn()

    # Delete existing collection if force
    if force:
        try:
            client.delete_collection(col_name)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=col_name,
        embedding_function=emb_fn,
        metadata={"repo": repo_name, "tag": tag, "indexed_at": datetime.utcnow().isoformat()},
    )

    # Batch upsert (ChromaDB performs better in batches of ~100)
    BATCH_SIZE = 100
    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start: batch_start + BATCH_SIZE]
        ids       = [f"{repo_name}__{tag}__{batch_start + i}" for i in range(len(batch))]
        docs      = [c["text"] for c in batch]
        metadatas = [
            {
                "file_path":   c["file_path"],
                "tag":         c["tag"],
                "line_start":  c["line_start"],
                "line_end":    c["line_end"],
                "block_type":  c["block_type"],
                "block_name":  c["block_name"],
                "repo_name":   repo_name,
            }
            for c in batch
        ]
        collection.upsert(ids=ids, documents=docs, metadatas=metadatas)
        print(f"     Batch {batch_start // BATCH_SIZE + 1}: {len(batch)} chunks done")

    print(f"  [DONE] {repo_name}@{tag}: {len(chunks)} chunks indexed -> collection '{col_name}'")
    return len(chunks)


def index_repo(repo_name: str, force: bool = False, tags_limit: int = 0) -> dict:
    """
    Index all tags for a repo.
    tags_limit=0 means index all tags. tags_limit=N means latest N tags only.
    """
    print(f"\n{'='*60}")
    print(f"Indexing repo: {repo_name}")
    print(f"{'='*60}")

    tags = list_tags_for_repo(repo_name)
    if not tags:
        print(f"  [WARN]  No Git tags found for '{repo_name}'.")
        print(f"     Ensure the repo is cloned and has at least one tag.")
        return {"repo": repo_name, "tags": [], "total_chunks": 0, "status": "no_tags"}

    if tags_limit > 0:
        tags = tags[:tags_limit]

    print(f"  Tags to index: {tags}")
    total_chunks = 0
    indexed_tags = []

    for tag in tags:
        try:
            n = index_tag(repo_name, tag, force=force)
            total_chunks += n
            indexed_tags.append(tag)
        except Exception as e:
            print(f"  [ERR] Error indexing {repo_name}@{tag}: {e}")

    return {
        "repo": repo_name,
        "tags": indexed_tags,
        "total_chunks": total_chunks,
        "status": "done",
    }


def index_all(force: bool = False) -> list[dict]:
    """Index all enabled repos from terrascope.config.yaml."""
    cfg = get_config()
    results = []
    for repo_cfg in cfg.enabled_repos:
        result = index_repo(repo_cfg.name, force=force)
        results.append(result)
    return results


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TerraScope Repo Indexer")
    parser.add_argument("--repo", type=str, default=None,
                        help="Index a specific repo by name (default: all enabled repos)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-index even if already indexed")
    parser.add_argument("--tags-limit", type=int, default=0,
                        help="Only index the N most recent tags (0 = all)")
    args = parser.parse_args()

    start = time.time()
    print("\nTerraScope Indexer")
    print(f"   Config: terrascope.config.yaml")
    print(f"   Force:  {args.force}")

    if args.repo:
        result = index_repo(args.repo, force=args.force, tags_limit=args.tags_limit)
        print(f"\n[DONE] {result}")
    else:
        results = index_all(force=args.force)
        total = sum(r.get("total_chunks", 0) for r in results)
        print(f"\n[DONE] All repos indexed. Total chunks: {total}")

    elapsed = time.time() - start
    print(f"   Time: {elapsed:.1f}s")
