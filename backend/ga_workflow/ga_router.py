"""
ga_router.py — FastAPI router for all GA Workflow endpoints.

Endpoints (GA_WORKFLOW_README.md §13):
  POST   /api/ga/workflow              → run full 7-stage pipeline (async background)
  GET    /api/ga/detect/{repo_name}    → detect GA version only
  GET    /api/ga/runs                  → list all workflow run records
  GET    /api/ga/runs/{run_id}         → get one run's full state
  DELETE /api/ga/runs/{run_id}         → delete a run record
  GET    /api/ga/changelog/{repo_name} → fetch raw provider changelog (cached)
  POST   /api/ga/validate/{repo_name}  → run validators on current branch files
  GET    /api/ga/compat/{repo_name}    → check provider compatibility for current changes

Mount in backend/main.py with:
  from backend.ga_workflow.ga_router import ga_router
  app.include_router(ga_router, prefix="/api/ga")
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.config import get_config
from backend.ga_workflow.ga_models import (
    GAWorkflowRequest,
    WorkflowRun,
    WorkflowStage,
)
from backend.ga_workflow.ga_orchestrator import (
    run_ga_workflow,
    get_run,
    list_runs,
    delete_run,
)
from backend.ga_workflow.gcp_service_detector import scan_gcp_service_features
from backend.ga_workflow.ga_detector import (
    detect_ga_release,
    fetch_provider_changelog,
    get_current_provider_version,
)
from backend.ga_workflow.ga_validators import validate_all
from backend.ga_workflow.ga_compat import check_provider_compatibility
from backend.ga_workflow.gcp_service_scanner import scan_gcp_service, GCP_PRODUCT_API_MAP
from backend.agent.tools.git_tools import get_latest_tag, list_tags_for_repo


ga_router = APIRouter(tags=["GA Workflow"])

# Simple in-process changelog cache: repo_name → (fetched_at_iso, content)
_changelog_cache: dict[str, tuple[str, str]] = {}
CHANGELOG_CACHE_MINUTES = 60


# ── POST /workflow ─────────────────────────────────────────────────────────────

@ga_router.post("/workflow", response_model=WorkflowRun)
async def trigger_workflow(
    request: GAWorkflowRequest,
    background_tasks: BackgroundTasks,
) -> WorkflowRun:
    """
    Start the full 7-stage GA workflow for a repo.

    Runs synchronously (awaited) so the caller receives the complete
    WorkflowRun when the pipeline finishes. For very large repos use
    the background variant by passing ?async=true (see /runs/{run_id}).

    Request body:
      repo_name    — required; must match an entry in terrascope.config.yaml
      base_branch  — optional, default "main"
      dry_run      — optional, default false; skips push + PR
      auto_fix     — optional, default true; fixes WARNING-level validation issues
      github_token — optional; falls back to GITHUB_TOKEN env var
      pr_labels    — optional list of label strings
    """
    cfg = get_config()
    if not cfg.get_repo(request.repo_name):
        raise HTTPException(
            status_code=404,
            detail=f"Repo '{request.repo_name}' not found in terrascope.config.yaml.",
        )

    try:
        run = await run_ga_workflow(request)
        return run
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Workflow error: {e}")


# ── GET /detect/{repo_name} ────────────────────────────────────────────────────

@ga_router.get("/detect/{repo_name}")
async def detect_ga(repo_name: str) -> dict:
    """
    Detect the latest GA provider version and compare to what the module uses.
    Does NOT create a branch, implement code, or open a PR.

    Returns a JSON object matching the shape in README §13:
      current_version, latest_ga_version, upgrade_required,
      breaking_changes, new_features, changelog_url, fetched_at
    """
    cfg = get_config()
    if not cfg.get_repo(repo_name):
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not configured.")

    # Minimal run object — only for logging; not stored
    run = WorkflowRun(run_id="_detect", repo_name=repo_name, gcp_product="")

    change_set = await detect_ga_release(repo_name=repo_name, run=run)
    if change_set is None:
        raise HTTPException(
            status_code=502,
            detail="Could not detect GA release. Check Terraform Registry connectivity.",
        )

    ga = change_set.ga_release
    return {
        "repo_name":        repo_name,
        "current_version":  ga.current_version,
        "latest_ga_version": ga.latest_ga_version,
        "upgrade_required": ga.upgrade_required,
        "breaking_changes": ga.breaking_changes,
        "new_features":     ga.new_features,
        "changelog_url":    ga.changelog_url,
        "fetched_at":       ga.fetched_at,
        "changes":          [
            {
                "change_type":   c.change_type.value,
                "resource_type": c.resource_type,
                "attribute":     c.attribute_name,
                "description":   c.description,
                "breaking":      c.breaking,
            }
            for c in change_set.changes
        ],
        "files_to_modify":  change_set.files_to_modify,
        "summary":          change_set.summary,
    }


# ── GET /runs ─────────────────────────────────────────────────────────────────

@ga_router.get("/runs")
async def get_all_runs(
    limit: int = Query(default=20, ge=1, le=200),
    repo_name: Optional[str] = Query(default=None),
) -> list[dict]:
    """
    List workflow run records, most recent first.
    Optionally filter by repo_name.
    """
    runs = list_runs()
    if repo_name:
        runs = [r for r in runs if r.repo_name == repo_name]
    runs = runs[:limit]

    return [
        {
            "run_id":          r.run_id,
            "repo_name":       r.repo_name,
            "gcp_product":     r.gcp_product,
            "stage":           r.stage.value,
            "overall_success": r.overall_success,
            "started_at":      r.started_at,
            "completed_at":    r.completed_at,
            "pr_url":          r.pr_result.pr_url if r.pr_result else None,
            "pr_number":       r.pr_result.pr_number if r.pr_result else None,
            "error":           r.error,
        }
        for r in runs
    ]


# ── GET /runs/{run_id} ────────────────────────────────────────────────────────

@ga_router.get("/runs/{run_id}", response_model=WorkflowRun)
async def get_run_by_id(run_id: str) -> WorkflowRun:
    """
    Return the full WorkflowRun state for a specific run_id.
    Includes all stage outputs, logs, and PR details.
    """
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return run


# ── DELETE /runs/{run_id} ─────────────────────────────────────────────────────

@ga_router.delete("/runs/{run_id}")
async def delete_run_by_id(run_id: str) -> dict:
    """Remove a workflow run record from the in-process store."""
    ok = delete_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return {"deleted": run_id}


# ── GET /changelog/{repo_name} ────────────────────────────────────────────────

@ga_router.get("/changelog/{repo_name}")
async def get_changelog(repo_name: str) -> dict:
    """
    Fetch the raw Google provider CHANGELOG.md from GitHub and return the
    section relevant to this repo's current → latest version range.
    Results are cached for CHANGELOG_CACHE_MINUTES minutes.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not configured.")

    # Check cache
    cached = _changelog_cache.get(repo_name)
    if cached:
        fetched_at_str, content = cached
        fetched_at = datetime.fromisoformat(fetched_at_str)
        age_minutes = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
        if age_minutes < CHANGELOG_CACHE_MINUTES:
            return {
                "repo_name":   repo_name,
                "content":     content,
                "fetched_at":  fetched_at_str,
                "cached":      True,
                "age_minutes": int(age_minutes),
            }

    # Get current version
    tag = get_latest_tag(repo_name) or "main"
    from backend.ga_workflow.ga_detector import (
        get_current_provider_version,
        fetch_latest_ga_version,
    )
    current_ver = get_current_provider_version(repo_name, tag) or "0.0.0"
    latest_ver  = await fetch_latest_ga_version() or current_ver

    content = await fetch_provider_changelog(current_ver, latest_ver)
    now_iso = datetime.now(timezone.utc).isoformat()
    _changelog_cache[repo_name] = (now_iso, content)

    return {
        "repo_name":    repo_name,
        "content":      content,
        "fetched_at":   now_iso,
        "cached":       False,
        "from_version": current_ver,
        "to_version":   latest_ver,
    }


# ── POST /validate/{repo_name} ────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    branch_name:   str
    changed_files: list[str] = []   # empty = validate all .tf files in repo
    auto_fix:      bool = True


@ga_router.post("/validate/{repo_name}")
async def validate_repo(repo_name: str, body: ValidateRequest) -> dict:
    """
    Run the four validators (HCL syntax, required attrs, naming, types)
    against the specified files on the current working tree.

    Useful for validating manual edits on a GA branch before merging.
    """
    cfg = get_config()
    if not cfg.get_repo(repo_name):
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not configured.")

    run = WorkflowRun(run_id="_validate", repo_name=repo_name, gcp_product="")

    result = validate_all(
        repo_name=repo_name,
        branch_name=body.branch_name,
        changed_files=body.changed_files,
        run=run,
        auto_fix=body.auto_fix,
    )

    return {
        "repo_name":      repo_name,
        "branch_name":    body.branch_name,
        "overall_passed": result.overall_passed,
        "error_count":    result.error_count,
        "warning_count":  result.warning_count,
        "validated_files": result.validated_files,
        "reports": [
            {
                "validator": r.validator_name,
                "passed":    r.passed,
                "duration_ms": r.duration_ms,
                "issues": [
                    {
                        "severity":   i.severity.value,
                        "file":       i.file_path,
                        "line":       i.line,
                        "rule":       i.rule,
                        "message":    i.message,
                        "suggestion": i.suggestion,
                    }
                    for i in r.issues
                ],
            }
            for r in result.reports
        ],
    }


# ── GET /compat/{repo_name} ───────────────────────────────────────────────────

@ga_router.get("/compat/{repo_name}")
async def check_compat(
    repo_name: str,
    target_version: Optional[str] = Query(default=None),
) -> dict:
    """
    Check provider schema compatibility for the repo's current detected changes.
    If target_version is not provided, the latest GA version is fetched.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not configured.")

    # Build a minimal change_set via detect
    run = WorkflowRun(run_id="_compat", repo_name=repo_name, gcp_product=repo_cfg.gcp_product)
    change_set = await detect_ga_release(repo_name=repo_name, run=run)
    if change_set is None:
        raise HTTPException(status_code=502, detail="Could not detect GA changes.")

    # Override target version if requested
    if target_version:
        change_set.ga_release.latest_ga_version = target_version

    compat = await check_provider_compatibility(change_set=change_set, run=run)

    return {
        "repo_name":       repo_name,
        "current_version": compat.current_version,
        "target_version":  compat.target_version,
        "all_compatible":  compat.all_compatible,
        "checks": [
            {
                "resource_type":  c.resource_type,
                "attribute_name": c.attribute_name,
                "supported":      c.supported,
                "min_version":    c.min_version,
                "deprecated_in":  c.deprecated_in,
                "notes":          c.notes,
            }
            for c in compat.checks
        ],
        "versions_tf_update": compat.versions_tf_update,
    }


# ── GET /service-scan/{repo_name} ─────────────────────────────────────────────

@ga_router.get("/service-scan/{repo_name}")
async def scan_service(
    repo_name: str,
    days_back: int = Query(default=180, ge=7, le=365),
) -> dict:
    """
    Scan Google Cloud release notes and API Discovery for new GA features
    for the GCP service/product associated with this repo.

    This is independent of Terraform provider version — it checks what
    Google Cloud itself has announced as GA for the service.

    Args:
        days_back: How many days of release notes to scan (default 180 = 6 months)
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not configured.")

    run = WorkflowRun(run_id="_service_scan", repo_name=repo_name, gcp_product=repo_cfg.gcp_product)

    scan = await scan_gcp_service_features(repo_name=repo_name, run=run, days_back=days_back)
    if scan is None:
        raise HTTPException(status_code=502, detail="GCP service scan failed. Check logs.")

    return scan.to_dict()


# ── GET /scan/{repo_name} ─────────────────────────────────────────────────────

@ga_router.get("/scan/{repo_name}")
async def scan_gcp_service_endpoint(
    repo_name: str,
    force: bool = Query(default=False, description="Re-scan even if cached"),
) -> dict:
    """
    Scan the GCP service/product associated with this repo for new GA features
    that are not yet reflected in the Terraform module code.

    Queries:
      - Google Cloud Release Notes feed for the product
      - Google API Discovery Service schema
      - Local LLM to map GCP features to Terraform impact

    Returns a GCPServiceScanResult with all detected features and
    actionable_features (those not yet in the module).
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        raise HTTPException(status_code=404, detail=f"Repo \'{repo_name}\' not configured.")

    product = repo_cfg.gcp_product
    product_info = GCP_PRODUCT_API_MAP.get(product, {})

    run = WorkflowRun(run_id="_scan", repo_name=repo_name, gcp_product=product)

    result = await scan_gcp_service(repo_name=repo_name, run=run)

    return {
        "repo_name":          result.repo_name,
        "gcp_product":        result.gcp_product,
        "scan_date":          result.scan_date,
        "total_features":     result.total_features,
        "actionable_count":   result.actionable_count,
        "summary":            result.summary,
        "module_resources":   result.module_resources,
        "docs_url":           product_info.get("docs_url", ""),
        "features": [
            {
                "feature_name":        f.feature_name,
                "description":         f.description,
                "announced_date":      f.announced_date,
                "terraform_impact":    f.terraform_impact,
                "terraform_resources": f.terraform_resources,
                "terraform_args":      f.terraform_args,
                "ga_confirmed":        f.ga_confirmed,
                "source":              f.source,
                "source_url":          f.source_url,
            }
            for f in result.features
        ],
        "actionable_features": [
            {
                "feature_name":        f.feature_name,
                "description":         f.description,
                "terraform_impact":    f.terraform_impact,
                "terraform_resources": f.terraform_resources,
                "terraform_args":      f.terraform_args,
                "ga_confirmed":        f.ga_confirmed,
                "source_url":          f.source_url,
            }
            for f in result.actionable_features
        ],
        "logs": [
            {"level": lg.level, "message": lg.message}
            for lg in run.logs
        ],
    }


# ── GET /products ─────────────────────────────────────────────────────────────

@ga_router.get("/products")
async def list_supported_products() -> dict:
    """
    List all GCP products/services that TerraScope can scan for GA features,
    with their Discovery API names and release notes slugs.
    """
    return {
        "supported_products": {
            product: {
                "api_name":    info["api_name"],
                "version":     info["version"],
                "docs_url":    info["docs_url"],
                "tf_prefix":   info["tf_resource_prefix"],
            }
            for product, info in GCP_PRODUCT_API_MAP.items()
        },
        "total": len(GCP_PRODUCT_API_MAP),
    }
