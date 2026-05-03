"""
main.py — FastAPI backend for TerraScope.
Serves the PydanticAI agent via REST API.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import get_config
from backend.agent.models import (
    AgentResponse, QueryRequest, IndexRequest, IndexStatus
)
from backend.agent.terrascope_agent import run_query
from backend.agent.tools.git_tools import list_tags_for_repo, get_latest_tag
from backend.agent.tools.search_tools import (
    is_indexed, get_indexed_tags, get_chunk_count
)
from backend.indexer.repo_indexer import index_repo, index_all


# ── Background indexing state ─────────────────────────────────────────────────
_indexing_jobs: dict[str, str] = {}   # repo_name → "indexing" | "done" | "error"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    print("\n🔭 TerraScope API starting...")
    print(f"   LLM: {cfg.llm.model} via {cfg.llm.base_url}")
    print(f"   Repos: {[r.name for r in cfg.enabled_repos]}")
    print(f"   Grounding: {cfg.grounding.mode}")
    yield
    print("TerraScope API shutting down.")


app = FastAPI(
    title="TerraScope API",
    description="AI agent for Terraform module curation — Google Cloud",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    cfg = get_config()
    import httpx
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{cfg.llm.base_url}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        pass

    return {
        "status": "ok",
        "ollama": "running" if ollama_ok else "unreachable — start Ollama first",
        "model": cfg.llm.model,
        "repos_configured": len(cfg.enabled_repos),
        "grounding_mode": cfg.grounding.mode,
    }


# ── Repos ─────────────────────────────────────────────────────────────────────

@app.get("/api/repos")
async def list_repos():
    """List all configured repos with their metadata."""
    cfg = get_config()
    result = []
    for repo in cfg.enabled_repos:
        tags = list_tags_for_repo(repo.name)
        latest = tags[0] if tags else None
        indexed_tags = get_indexed_tags(repo.name)
        result.append({
            "name": repo.name,
            "display_name": repo.display_name,
            "gcp_product": repo.gcp_product,
            "description": repo.description,
            "tags": tags,
            "latest_tag": latest,
            "indexed_tags": indexed_tags,
            "indexing_status": _indexing_jobs.get(repo.name, "idle"),
        })
    return result


# ── Tags ──────────────────────────────────────────────────────────────────────

@app.get("/api/repos/{repo_name}/tags")
async def get_tags(repo_name: str):
    """List all Git tags for a specific repo."""
    tags = list_tags_for_repo(repo_name)
    if tags is None:
        raise HTTPException(404, f"Repo '{repo_name}' not found")
    indexed = get_indexed_tags(repo_name)
    return {
        "repo_name": repo_name,
        "tags": tags,
        "indexed_tags": indexed,
        "latest": tags[0] if tags else None,
    }


# ── Index ─────────────────────────────────────────────────────────────────────

def _do_index(repo_name: Optional[str], force: bool):
    """Blocking indexing task — run in thread pool."""
    try:
        if repo_name:
            _indexing_jobs[repo_name] = "indexing"
            index_repo(repo_name, force=force)
            _indexing_jobs[repo_name] = "done"
        else:
            cfg = get_config()
            for r in cfg.enabled_repos:
                _indexing_jobs[r.name] = "indexing"
            index_all(force=force)
            for r in cfg.enabled_repos:
                _indexing_jobs[r.name] = "done"
    except Exception as e:
        key = repo_name or "all"
        _indexing_jobs[key] = f"error: {e}"


@app.post("/api/index")
async def trigger_index(request: IndexRequest, background_tasks: BackgroundTasks):
    """Trigger indexing in the background. Returns immediately."""
    background_tasks.add_task(
        asyncio.get_event_loop().run_in_executor,
        None, _do_index, request.repo_name, request.force
    )
    return {
        "status": "indexing_started",
        "repo": request.repo_name or "all",
        "force": request.force,
        "message": "Indexing started in background. Poll /api/index/status for progress.",
    }


@app.get("/api/index/status")
async def index_status():
    """Return indexing status for all repos."""
    cfg = get_config()
    statuses = []
    for repo in cfg.enabled_repos:
        indexed_tags = get_indexed_tags(repo.name)
        total_chunks = sum(get_chunk_count(repo.name, t) for t in indexed_tags)
        statuses.append(IndexStatus(
            repo_name=repo.name,
            display_name=repo.display_name,
            gcp_product=repo.gcp_product,
            tags_indexed=indexed_tags,
            total_chunks=total_chunks,
            last_indexed_at=None,
            status=_indexing_jobs.get(repo.name, "ready" if indexed_tags else "not_indexed"),
        ))
    return statuses


# ── Query ─────────────────────────────────────────────────────────────────────

@app.post("/api/query", response_model=AgentResponse)
async def query(request: QueryRequest):
    """
    Main query endpoint. Runs the PydanticAI agent.
    Returns a fully typed AgentResponse with sources and optional IssueSolution.
    """
    try:
        response = await run_query(request)
        return response
    except Exception as e:
        raise HTTPException(500, detail=f"Agent error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    cfg = get_config()
    uvicorn.run(
        "backend.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=cfg.server.reload,
    )
