"""
main.py — FastAPI backend for TerraScope.

Endpoints:
  GET  /api/health                            — Ollama + network status
  GET  /api/repos                             — List configured repos
  GET  /api/repos/{name}/tags                 — Tags for a repo
  POST /api/index                             — Trigger background indexing
  GET  /api/index/status                      — Indexing progress
  POST /api/query                             — Query/analysis agent (grounded Q&A)
  POST /api/generate                          — HCL generation pipeline
  POST /api/generate/async                    — Async HCL generation
  GET  /api/generate/{job_id}                 — Retrieve completed generation job

  POST /api/curate/start                      — Create curation session
  GET  /api/curate/{session_id}               — Poll session state
  POST /api/curate/{session_id}/upload-doc    — Upload PDF/DOCX/TXT spec
  POST /api/curate/{session_id}/upload-module — Upload ZIP or .tf file(s)
  POST /api/curate/{session_id}/set-source    — Set GitHub / local source
  POST /api/curate/{session_id}/answer        — Answer current clarifying question
  POST /api/curate/{session_id}/generate      — Trigger Terraform code generation

  GET  /api/registry/status                   — Registry cache stats + network status
  POST /api/registry/fetch                    — Pre-fetch docs for a provider+service
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.config import get_config
from backend.agent.models import (
    AgentResponse, QueryRequest, IndexRequest, IndexStatus,
    GenerateRequest, GenerationResponse,
)
from backend.agent.terrascope_agent import run_query, run_generation
from backend.agent.tools.git_tools import list_tags_for_repo, get_latest_tag
from backend.agent.tools.search_tools import is_indexed, get_indexed_tags, get_chunk_count
from backend.indexer.repo_indexer import index_repo, index_all
from backend.module_curator import curator
from backend.module_curator.models import StartCurationRequest, SetSourceRequest, AnswerRequest
from backend.registry_fetcher.registry_api import fetch_service_docs, is_network_available
from backend.registry_fetcher.cache_manager import cache_stats


# ── State ─────────────────────────────────────────────────────────────────────

_indexing_jobs:    dict[str, str]               = {}   # repo_name -> status string
_generation_jobs:  dict[str, GenerationResponse] = {}   # job_id    -> result


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    print("\nTerraScope API starting...")
    print(f"   LLM   : {cfg.llm.model} via {cfg.llm.base_url}")
    print(f"   Repos : {[r.name for r in cfg.enabled_repos]}")
    print(f"   Ground: {cfg.grounding.mode}")
    yield
    print("TerraScope API shutting down.")


app = FastAPI(
    title="TerraScope API",
    description="AI agent for Terraform module curation — Google Cloud",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:8080", "http://127.0.0.1:8080",
    ],
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
        "status":            "ok",
        "ollama":            "running" if ollama_ok else "unreachable — start Ollama first",
        "model":             cfg.llm.model,
        "repos_configured":  len(cfg.enabled_repos),
        "grounding_mode":    cfg.grounding.mode,
        "network_available": is_network_available(),
    }


# ── Repos ─────────────────────────────────────────────────────────────────────

@app.get("/api/repos")
async def list_repos():
    cfg = get_config()
    result = []
    for repo in cfg.enabled_repos:
        tags         = list_tags_for_repo(repo.name)
        latest       = tags[0] if tags else None
        indexed_tags = get_indexed_tags(repo.name)
        result.append({
            "name":             repo.name,
            "display_name":     repo.display_name,
            "gcp_product":      repo.gcp_product,
            "description":      repo.description,
            "tags":             tags,
            "latest_tag":       latest,
            "indexed_tags":     indexed_tags,
            "indexing_status":  _indexing_jobs.get(repo.name, "idle"),
        })
    return result


@app.get("/api/repos/{repo_name}/tags")
async def get_tags(repo_name: str):
    cfg = get_config()
    if not cfg.get_repo(repo_name):
        raise HTTPException(404, f"Repo '{repo_name}' not found")
    tags    = list_tags_for_repo(repo_name)
    indexed = get_indexed_tags(repo_name)
    return {
        "repo_name":    repo_name,
        "tags":         tags,
        "indexed_tags": indexed,
        "latest":       tags[0] if tags else None,
    }


# ── Index ─────────────────────────────────────────────────────────────────────

def _do_index(repo_name: Optional[str], force: bool) -> None:
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
    background_tasks.add_task(
        asyncio.get_event_loop().run_in_executor,
        None, _do_index, request.repo_name, request.force,
    )
    return {
        "status":  "indexing_started",
        "repo":    request.repo_name or "all",
        "force":   request.force,
        "message": "Poll /api/index/status for progress.",
    }


@app.get("/api/index/status")
async def index_status():
    cfg      = get_config()
    statuses = []
    for repo in cfg.enabled_repos:
        indexed_tags  = get_indexed_tags(repo.name)
        total_chunks  = sum(get_chunk_count(repo.name, t) for t in indexed_tags)
        statuses.append(IndexStatus(
            repo_name       = repo.name,
            display_name    = repo.display_name,
            gcp_product     = repo.gcp_product,
            tags_indexed    = indexed_tags,
            total_chunks    = total_chunks,
            last_indexed_at = None,
            status          = _indexing_jobs.get(repo.name, "ready" if indexed_tags else "not_indexed"),
        ))
    return statuses


# ── Query ─────────────────────────────────────────────────────────────────────

@app.post("/api/query", response_model=AgentResponse)
async def query(request: QueryRequest):
    """Grounded Q&A over indexed Terraform repo code."""
    try:
        return await run_query(request)
    except Exception as e:
        raise HTTPException(500, detail=f"Agent error: {str(e)}")


# ── Generation ────────────────────────────────────────────────────────────────

@app.post("/api/generate", response_model=GenerationResponse)
async def generate(request: GenerateRequest):
    """
    HCL generation pipeline.

    Modes:
      extend  — Add features/fixes to an existing module (returns diffs)
      new     — Create a net-new module from scratch (4 complete files)
      compose — Create a composite module wiring existing modules together

    The pipeline:
      1. Assembles schema + existing-code context
      2. Calls the LLM with the TerraScope engineer system prompt
      3. Parses ---FILE: path---...---ENDFILE--- markers
      4. Validates output: security baseline, lint, variable completeness
      5. Returns GenerationResponse with files + validation_notes

    Note: quality depends on the configured LLM.
    Recommended: qwen2.5-coder:7b or llama3.1:8b for code generation tasks.
    Minimum:     gemma3:4b (smaller models may miss FILE markers — set a longer max_tokens).
    """
    try:
        return await run_generation(request)
    except Exception as e:
        raise HTTPException(500, detail=f"Generation error: {str(e)}")


@app.post("/api/generate/async")
async def generate_async(request: GenerateRequest, background_tasks: BackgroundTasks):
    """
    Kick off generation in the background. Returns a job_id immediately.
    Poll GET /api/generate/{job_id} for the result.
    Use this for larger generation tasks to avoid HTTP timeouts.
    """
    job_id = str(uuid.uuid4())
    _generation_jobs[job_id] = None  # sentinel: job queued

    async def _run() -> None:
        try:
            result = await run_generation(request)
        except Exception as e:
            result = GenerationResponse(
                mode             = request.mode,
                target_module    = request.target_module,
                files            = [],
                validation_notes = [],
                ready            = False,
                disclaimer       = f"Background generation failed: {e}",
            )
        _generation_jobs[job_id] = result

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/generate/{job_id}")
async def get_generation_result(job_id: str):
    """
    Retrieve result of an async generation job.
    Returns {status: 'pending'} while still running,
    or the full GenerationResponse when done.
    """
    if job_id not in _generation_jobs:
        raise HTTPException(404, f"Generation job '{job_id}' not found")
    result = _generation_jobs[job_id]
    if result is None:
        return {"status": "pending", "job_id": job_id}
    return result


# ── Curation ─────────────────────────────────────────────────────────────────

@app.post("/api/curate/start")
async def curate_start(request: StartCurationRequest):
    """Create a new curation session. Immediately triggers Q&A for new_product / self_curation."""
    session = curator.create_session(request)
    try:
        if request.mode.value == "new_product":
            await curator.start_new_product(session.session_id)
        elif request.mode.value == "self_curation":
            await curator.start_self_curation(session.session_id)
        # from_document and from_module wait for an upload before Q&A starts
    except Exception as exc:
        session = curator.get_session(session.session_id)
        if session:
            session.error = str(exc)
    return curator.to_view(curator.get_session(session.session_id))


@app.get("/api/curate/{session_id}")
async def curate_get(session_id: str):
    """Poll session state (status, current question, generated files, etc.)."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return curator.to_view(session)


@app.post("/api/curate/{session_id}/upload-doc")
async def curate_upload_doc(session_id: str, file: UploadFile = File(...)):
    """Upload a PDF, DOCX, or TXT specification document to seed the curation."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        from backend.document_processor.processor import extract_text
        raw = await file.read()
        text = extract_text(raw, file.filename or "upload.txt")
        await curator.start_from_document(session_id, text)
    except Exception as exc:
        raise HTTPException(500, f"Document processing failed: {exc}")
    return curator.to_view(curator.get_session(session_id))


@app.post("/api/curate/{session_id}/upload-module")
async def curate_upload_module(session_id: str, file: UploadFile = File(...)):
    """Upload a ZIP archive or individual .tf file as the base module."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        raw = await file.read()
        fname = file.filename or ""
        if fname.endswith(".zip"):
            await curator.start_from_zip(session_id, raw)
        elif fname.endswith(".tf"):
            await curator.start_from_tf_files(session_id, {fname: raw.decode("utf-8", errors="replace")})
        else:
            raise HTTPException(400, "Only .zip or .tf files are accepted")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Module upload failed: {exc}")
    return curator.to_view(curator.get_session(session_id))


@app.post("/api/curate/{session_id}/set-source")
async def curate_set_source(session_id: str, request: SetSourceRequest):
    """Set a GitHub repo URL or local filesystem path as the module source."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        if request.source_type == "github":
            if not request.url:
                raise HTTPException(400, "url is required for github source_type")
            await curator.start_from_github(session_id, request.url, request.tag)
        elif request.source_type == "local":
            if not request.path:
                raise HTTPException(400, "path is required for local source_type")
            await curator.start_from_local(session_id, request.path)
        else:
            raise HTTPException(400, f"Unknown source_type '{request.source_type}'")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Source ingestion failed: {exc}")
    return curator.to_view(curator.get_session(session_id))


@app.post("/api/curate/{session_id}/answer")
async def curate_answer(session_id: str, request: AnswerRequest):
    """Submit the user's answer to the current clarifying question."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        await curator.answer_question(session_id, request.answer)
    except Exception as exc:
        raise HTTPException(500, f"Answer processing failed: {exc}")
    return curator.to_view(curator.get_session(session_id))


@app.post("/api/curate/{session_id}/generate")
async def curate_generate(session_id: str):
    """Trigger Terraform module generation for a session in READY state."""
    session = curator.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        result = await curator.generate(session_id)
        return curator.to_view(curator.get_session(session_id))
    except Exception as exc:
        raise HTTPException(500, f"Code generation failed: {exc}")


# ── Registry ──────────────────────────────────────────────────────────────────

@app.get("/api/registry/status")
async def registry_status():
    """Return cache statistics and network availability."""
    try:
        stats = cache_stats()
    except Exception:
        stats = {}
    return {
        "network_available": is_network_available(),
        "cache":             stats,
    }


class RegistryFetchRequest(BaseModel):
    provider:     str
    service_name: str


@app.post("/api/registry/fetch")
async def registry_fetch(request: RegistryFetchRequest):
    """Pre-fetch and cache provider documentation for a given service."""
    try:
        docs = await fetch_service_docs(request.provider, request.service_name)
        return {
            "provider":     request.provider,
            "service_name": request.service_name,
            "fetched":      not docs.startswith("No documentation"),
            "preview":      docs[:500],
        }
    except Exception as exc:
        raise HTTPException(500, f"Registry fetch failed: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    cfg = get_config()
    uvicorn.run(
        "backend.main:app",
        host   = cfg.server.host,
        port   = cfg.server.port,
        reload = cfg.server.reload,
    )
