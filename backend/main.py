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

  POST /api/scenarios/start                   — Create scenario generation session
  POST /api/scenarios/{id}/upload-module      — Upload ZIP or .tf for scenario gen
  POST /api/scenarios/{id}/set-source         — Set GitHub / local source for scenario gen
  GET  /api/scenarios/{id}                    — Poll scenario session state
  POST /api/scenarios/{id}/generate           — Run full scenario pipeline (background)

  POST /api/troubleshoot                      — Detect logical/syntactic bugs + suggest safe upgrade
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
from backend.ga_workflow.ga_router import ga_router


# ── State ─────────────────────────────────────────────────────────────────────

_indexing_jobs:    dict[str, str]               = {}   # repo_name -> status string
_generation_jobs:  dict[str, GenerationResponse] = {}   # job_id    -> result
_scenario_sessions: dict[str, "ScenarioSession"] = {}   # session_id -> ScenarioSession


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

app.include_router(ga_router, prefix="/api/ga")

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


# ── Scenario Generator ────────────────────────────────────────────────────────
# Import lazily to avoid startup failures if optional deps missing
from backend.scenario_generator.models import (
    ScenarioSession,
    ScenarioSessionStatus,
)


class StartScenarioRequest(BaseModel):
    source_type: str = "github"   # github | local | zip | tf
    url:         str = ""
    path:        str = ""
    tag:         Optional[str] = None


class ScenarioSetSourceRequest(BaseModel):
    source_type: str
    url:         str = ""
    path:        str = ""
    tag:         Optional[str] = None


@app.post("/api/scenarios/start")
async def scenario_start(request: StartScenarioRequest):
    """Create a new scenario generation session."""
    session_id = str(uuid.uuid4())
    session = ScenarioSession(
        session_id=session_id,
        source_type=request.source_type,
        source_url=request.url or request.path,
        tag=request.tag,
        status=ScenarioSessionStatus.LOADING,
    )
    _scenario_sessions[session_id] = session
    return session.model_dump()


@app.post("/api/scenarios/{session_id}/upload-module")
async def scenario_upload_module(session_id: str, file: UploadFile = File(...)):
    """Upload a ZIP or .tf file as the module source for scenario generation."""
    session = _scenario_sessions.get(session_id)
    if not session:
        raise HTTPException(404, f"Scenario session '{session_id}' not found")
    try:
        raw = await file.read()
        fname = file.filename or ""
        if fname.endswith(".zip"):
            from backend.scenario_generator.loader import load_from_zip
            tf_files = load_from_zip(raw)
        elif fname.endswith(".tf"):
            tf_files = {fname: raw.decode("utf-8", errors="replace")}
        else:
            raise HTTPException(400, "Only .zip or .tf files are accepted")
        session.status = ScenarioSessionStatus.PARSING
        # Parse immediately so we know what we have
        from backend.scenario_generator.parser import build_module_spec
        module_name = fname.replace(".zip", "").replace(".tf", "") or "module"
        session.module_spec = build_module_spec(tf_files, module_name=module_name, module_source="uploaded")
        session.status = ScenarioSessionStatus.PLANNING
    except HTTPException:
        raise
    except Exception as exc:
        session.status = ScenarioSessionStatus.ERROR
        session.error_message = str(exc)
        raise HTTPException(500, f"Module upload failed: {exc}")
    return session.model_dump()


@app.post("/api/scenarios/{session_id}/set-source")
async def scenario_set_source(session_id: str, request: ScenarioSetSourceRequest):
    """Set GitHub or local source for the scenario session (loads + parses immediately)."""
    session = _scenario_sessions.get(session_id)
    if not session:
        raise HTTPException(404, f"Scenario session '{session_id}' not found")
    try:
        session.status = ScenarioSessionStatus.LOADING
        from backend.scenario_generator.loader import load_module
        tf_files = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: load_module(
                source_type=request.source_type,
                url=request.url,
                local_path=request.path,
                tag=request.tag,
            ),
        )
        session.source_url = request.url or request.path
        session.tag = request.tag
        session.status = ScenarioSessionStatus.PARSING

        from backend.scenario_generator.parser import build_module_spec
        module_name = (request.url or request.path).rstrip("/").split("/")[-1] or "module"
        session.module_spec = build_module_spec(
            tf_files, module_name=module_name, module_source=request.url or request.path
        )
        session.status = ScenarioSessionStatus.PLANNING
    except Exception as exc:
        session.status = ScenarioSessionStatus.ERROR
        session.error_message = str(exc)
        raise HTTPException(500, f"Source loading failed: {exc}")
    return session.model_dump()


@app.get("/api/scenarios/{session_id}")
async def scenario_get(session_id: str):
    """Poll scenario session state."""
    session = _scenario_sessions.get(session_id)
    if not session:
        raise HTTPException(404, f"Scenario session '{session_id}' not found")
    return session.model_dump()


@app.post("/api/scenarios/{session_id}/generate")
async def scenario_generate(session_id: str, background_tasks: BackgroundTasks):
    """Run the full scenario pipeline (plan → synthesize → validate) in the background."""
    session = _scenario_sessions.get(session_id)
    if not session:
        raise HTTPException(404, f"Scenario session '{session_id}' not found")
    if session.module_spec is None:
        raise HTTPException(400, "Module not loaded yet. Call /set-source or /upload-module first.")

    async def _pipeline() -> None:
        try:
            spec = session.module_spec

            # Plan
            session.status = ScenarioSessionStatus.PLANNING
            from backend.scenario_generator.planner import plan_scenarios
            plan = await plan_scenarios(spec)
            session.scenario_plan = plan

            # Synthesize
            session.status = ScenarioSessionStatus.GENERATING
            from backend.scenario_generator.synthesizer import (
                build_output_dir, synthesize_scenario, write_scenario,
            )
            out_dir = build_output_dir(spec.module_name)
            session.output_dir = str(out_dir)
            generated = []
            for entry in plan.scenarios:
                gs = synthesize_scenario(entry, spec)
                gs = write_scenario(gs, out_dir, session.source_url or "")
                generated.append(gs)
            session.generated_scenarios = generated

            # Validate
            session.status = ScenarioSessionStatus.VALIDATING
            from backend.scenario_generator.validator import validate_all
            results = await validate_all(generated, spec, module_source_path=session.source_url or "")
            session.validation_results = results

            # Coverage
            from backend.scenario_generator.coverage import build_coverage_report, write_coverage_md
            report = build_coverage_report(spec, generated, results)
            session.coverage_report = report
            write_coverage_md(report, out_dir)

            session.status = ScenarioSessionStatus.DONE
        except Exception as exc:
            session.status = ScenarioSessionStatus.ERROR
            session.error_message = str(exc)
            print(f"[scenario_generate] Pipeline error: {exc}")

    background_tasks.add_task(_pipeline)
    return {"status": "pipeline_started", "session_id": session_id,
            "message": "Poll GET /api/scenarios/{session_id} for progress."}


@app.post("/api/scenarios/{session_id}/rerun/{scenario_name}")
async def scenario_rerun(session_id: str, scenario_name: str, background_tasks: BackgroundTasks):
    """Re-run validation for a single failing scenario."""
    session = _scenario_sessions.get(session_id)
    if not session:
        raise HTTPException(404, f"Scenario session '{session_id}' not found")
    gs = next((g for g in session.generated_scenarios if g.name == scenario_name), None)
    if not gs:
        raise HTTPException(404, f"Scenario '{scenario_name}' not found")

    async def _rerun() -> None:
        from backend.scenario_generator.validator import validate_all
        new_results = await validate_all([gs], session.module_spec, session.source_url or "")
        # Merge: replace the result for this scenario
        updated = [r for r in session.validation_results if r.scenario != scenario_name]
        updated.extend(new_results)
        session.validation_results = updated

    background_tasks.add_task(_rerun)
    return {"status": "rerun_started", "scenario": scenario_name}


# ── Troubleshoot ─────────────────────────────────────────────────────────────

class TroubleshootRequest(BaseModel):
    repo_name:           str
    tag:                 Optional[str] = None   # defaults to latest tag
    problem_description: str = ""               # optional user-reported symptom


@app.post("/api/troubleshoot")
async def troubleshoot(request: TroubleshootRequest) -> dict:
    """
    Analyse a Terraform module at a specific tag for logical and syntactic bugs.

    Three-stage pipeline:
      1. Static analysis — undefined references, type issues, security patterns,
         deprecated usage, missing provider constraints.
      2. LLM analysis   — logical bugs, anti-patterns, security misconfigs,
         missing `depends_on`, hardcoded values, etc.
      3. Version recommendation — finds the minimum provider version that fixes
         detected issues without introducing breaking changes for this module.

    Request:
      repo_name           — must match terrascope.config.yaml
      tag                 — Git tag to analyse (omit for latest tag)
      problem_description — optional error message or symptom to guide the LLM

    Returns a TroubleshootResult with all issues and a version recommendation.
    """
    cfg = get_config()
    if not cfg.get_repo(request.repo_name):
        raise HTTPException(
            status_code=404,
            detail=f"Repo '{request.repo_name}' not found in terrascope.config.yaml.",
        )

    from backend.troubleshooter.troubleshooter import troubleshoot_module
    from backend.agent.tools.git_tools import get_latest_tag as _latest

    tag = request.tag or _latest(request.repo_name) or "main"

    try:
        result = await troubleshoot_module(
            repo_name=request.repo_name,
            tag=tag,
            problem_description=request.problem_description,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Troubleshoot failed: {e}")

    ver_rec = None
    if result.version_recommendation:
        vr = result.version_recommendation
        ver_rec = {
            "current_version":     vr.current_version,
            "recommended_version": vr.recommended_version,
            "reason":              vr.reason,
            "breaking_changes":    vr.breaking_changes,
            "safe_to_upgrade":     vr.safe_to_upgrade,
            "changelog_url":       vr.changelog_url,
            "fixes_in_version":    vr.fixes_in_version,
        }

    return {
        "repo_name":    result.repo_name,
        "tag":          result.tag,
        "scan_date":    result.scan_date,
        "summary":      result.summary,
        "error_count":  result.error_count,
        "warning_count": result.warning_count,
        "info_count":   result.info_count,
        "scanned_files": result.scanned_files,
        "version_recommendation": ver_rec,
        "issues": [
            {
                "severity":      i.severity.value,
                "category":      i.category.value,
                "file_path":     i.file_path,
                "line":          i.line,
                "resource_type": i.resource_type,
                "resource_name": i.resource_name,
                "message":       i.message,
                "suggestion":    i.suggestion,
                "fixed_in_version": i.fixed_in_version,
            }
            for i in result.issues
        ],
    }


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
