"""
ga_orchestrator.py — GA Workflow pipeline coordinator.

Executes all seven stages in sequence, updating the WorkflowRun state machine
after each stage. Any unrecovered exception in a stage calls run.fail() and
returns early — downstream stages are not executed.

Multi-cloud support (v2.3):
  - Auto-detects cloud provider (google/aws/azurerm) from versions.tf
  - Routes service scan to GCP / AWS / Azure scanner automatically
  - Incremental state: skips changes already applied in a previous run
    (state persisted in ./data/ga_state/{repo_name}.json)

Public API:
  run_ga_workflow(request) → WorkflowRun   (used by FastAPI router)

CLI entry point:
  python -m backend.ga_workflow.ga_orchestrator --repo NAME [options]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.config import get_config
from backend.ga_workflow.ga_models import (
    GAChange,
    GAWorkflowRequest,
    IncrementalState,
    WorkflowRun,
    WorkflowStage,
)
from backend.ga_workflow.ga_detector import detect_ga_release, detect_terraform_provider
from backend.ga_workflow.gcp_service_detector import scan_gcp_service_features
from backend.ga_workflow.ga_implementer import (
    create_ga_branch,
    generate_code_changes,
    apply_code_changes,
)
from backend.ga_workflow.ga_validators import validate_all
from backend.ga_workflow.ga_compat import check_provider_compatibility
from backend.ga_workflow.ga_pr_manager import create_or_update_pr

# In-memory store of all workflow runs (keyed by run_id).
_runs: dict[str, WorkflowRun] = {}

_STATE_DIR = Path("./data/ga_state")


# ── Incremental state helpers ─────────────────────────────────────────────────

def _change_hash(change: GAChange) -> str:
    """Stable content hash for a GAChange — used to detect already-applied changes."""
    key = f"{change.change_type}:{change.resource_type}:{change.attribute_name}:{change.provider_version}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def load_incremental_state(repo_name: str) -> IncrementalState:
    path = _STATE_DIR / f"{repo_name}.json"
    if path.exists():
        try:
            return IncrementalState(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return IncrementalState(repo_name=repo_name)


def save_incremental_state(state: IncrementalState) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STATE_DIR / f"{state.repo_name}.json"
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def filter_new_changes(changes: list[GAChange], state: IncrementalState) -> list[GAChange]:
    """Return only changes whose hash is NOT already in the applied state."""
    seen = set(state.applied_change_hashes)
    return [c for c in changes if _change_hash(c) not in seen]


def mark_changes_applied(changes: list[GAChange], state: IncrementalState) -> IncrementalState:
    """Add change hashes to state (call after successful PR creation)."""
    new_hashes = [_change_hash(c) for c in changes]
    existing = set(state.applied_change_hashes)
    state.applied_change_hashes = list(existing | set(new_hashes))
    state.last_scan = datetime.now(timezone.utc).isoformat()
    return state


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_ga_workflow(request: GAWorkflowRequest) -> WorkflowRun:
    """
    Execute the full 7-stage GA upgrade pipeline for one repo.

    Stages:
      1+2  ga_detector.detect_ga_release()         → GAChangeSet
      3    ga_implementer.create_ga_branch()        → BranchResult
      4    ga_implementer.generate_code_changes()   → list[CodeChange]
           ga_implementer.apply_code_changes()      → CodeChangeSet
      5    ga_validators.validate_all()             → ValidationResult
      6    ga_compat.check_provider_compatibility() → ProviderCompatibility
      7    ga_pr_manager.create_or_update_pr()      → PRResult

    Returns a WorkflowRun containing outputs from all completed stages and
    a full log. The WorkflowRun is also stored in _runs for poll-based status.
    """
    cfg = get_config()

    # ── Build run object
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{request.repo_name.split('-')[-1]}"
    repo_cfg = cfg.get_repo(request.repo_name)
    product  = repo_cfg.gcp_product if repo_cfg else "unknown"

    run = WorkflowRun(
        run_id=run_id,
        repo_name=request.repo_name,
        gcp_product=product,
    )
    _runs[run_id] = run
    run.log(f"🚀 GA Workflow started — {request.repo_name}", level="info")

    if not repo_cfg:
        run.fail(f"Repo '{request.repo_name}' not found in terrascope.config.yaml.")
        return run

    # Load incremental state for this repo
    inc_state = load_incremental_state(request.repo_name)
    run.log(
        f"Incremental state: {len(inc_state.applied_change_hashes)} previously applied change(s) "
        f"(last scan: {inc_state.last_scan or 'never'})"
    )

    # ────────────────────────────────────────────────────────────────────────
    # Stage 1 + 2 — Detect GA release and analyse changes
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.DETECTING
    run.log("Stage 1/7 — Detecting GA release and analysing changes")
    try:
        change_set = await detect_ga_release(
            repo_name=request.repo_name,
            run=run,
            github_token=request.github_token,
        )
    except Exception as e:
        run.fail(f"GA detection failed: {e}")
        return run

    if change_set is None:
        run.fail("detect_ga_release() returned None — check logs above.")
        return run

    run.ga_release = change_set.ga_release
    run.change_set = change_set

    # Filter to only changes not yet applied (incremental mode)
    all_changes = change_set.changes
    new_changes = filter_new_changes(all_changes, inc_state)
    change_set.new_changes = new_changes
    skipped = len(all_changes) - len(new_changes)
    if skipped:
        run.log(f"Incremental: skipping {skipped} already-applied change(s), {len(new_changes)} new")

    if not change_set.ga_release.upgrade_required and not new_changes:
        run.log("Module is already on the latest GA provider version. Nothing to do.")
        run.stage = WorkflowStage.DONE
        run.overall_success = True
        run.completed_at = datetime.now(timezone.utc).isoformat()
        return run

    # ────────────────────────────────────────────────────────────────────────
    # Stage 1b — Scan cloud service for new GA features (multi-cloud)
    # ────────────────────────────────────────────────────────────────────────
    provider_label = change_set.ga_release.cloud_provider.value if change_set.ga_release.cloud_provider else "cloud"
    run.log(f"Stage 1b/7 — Scanning {provider_label} service for new GA features")
    try:
        cloud_scan = await scan_gcp_service_features(repo_name=request.repo_name, run=run)
        run.gcp_service_scan = cloud_scan

        # Filter service features already seen
        seen_features = set(inc_state.service_features_seen)
        new_service_features = [f for f in cloud_scan.actionable_features
                                 if f.feature_name not in seen_features]
        run.log(
            f"Service scan: {cloud_scan.total_features} features, "
            f"{cloud_scan.actionable_count} actionable, "
            f"{len(new_service_features)} not yet seen"
        )
    except Exception as e:
        run.log(f"Cloud service scan failed (non-critical): {e}", level="warning")

    run.log(
        f"Upgrade: v{change_set.ga_release.current_version} → "
        f"v{change_set.ga_release.latest_ga_version} "
        f"({len(new_changes)} new changes, "
        f"{change_set.ga_release.breaking_changes} breaking)"
    )

    # ────────────────────────────────────────────────────────────────────────
    # Stage 3 — Create (or reuse) GA branch
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.BRANCHING
    run.log("Stage 3/7 — Creating GA branch")
    try:
        branch_result = create_ga_branch(
            repo_name=request.repo_name,
            ga_version=change_set.ga_release.latest_ga_version,
            base_branch=request.base_branch,
            run=run,
        )
    except Exception as e:
        run.fail(f"Branch creation failed: {e}")
        return run

    if branch_result.error:
        run.fail(f"Branch creation error: {branch_result.error}")
        return run

    run.branch_result = branch_result
    action = "Created" if branch_result.created else "Reusing existing"
    run.log(f"{action} branch: {branch_result.branch_name}")

    # ────────────────────────────────────────────────────────────────────────
    # Stage 4 — Generate and apply code changes
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.IMPLEMENTING
    run.log("Stage 4/7 — Generating and applying code changes")
    try:
        code_changes_list = await generate_code_changes(
            change_set=change_set,
            run=run,
        )
    except Exception as e:
        run.fail(f"Code generation failed: {e}")
        return run

    if not code_changes_list:
        run.log(
            "No code changes were generated. "
            "The LLM may have found no actionable diffs. "
            "Continuing with empty commit — PR body will note this.",
            level="warning",
        )

    try:
        code_change_set = apply_code_changes(
            change_set=change_set,
            code_changes=code_changes_list,
            branch_result=branch_result,
            run=run,
            dry_run=request.dry_run,
        )
    except Exception as e:
        run.fail(f"Applying code changes failed: {e}")
        return run

    if code_change_set.apply_errors:
        run.log(
            f"{len(code_change_set.apply_errors)} file(s) failed to write: "
            + "; ".join(code_change_set.apply_errors[:3]),
            level="warning",
        )

    run.code_changes = code_change_set

    # ────────────────────────────────────────────────────────────────────────
    # Stage 5 — Validate generated HCL
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.VALIDATING
    run.log("Stage 5/7 — Validating generated code")
    changed_files = [c.file_path for c in code_changes_list] + ["CHANGELOG.md"]
    try:
        validation_result = validate_all(
            repo_name=request.repo_name,
            branch_name=branch_result.branch_name,
            changed_files=changed_files,
            run=run,
            auto_fix=request.auto_fix,
        )
    except Exception as e:
        run.fail(f"Validation stage failed unexpectedly: {e}")
        return run

    run.validation_result = validation_result

    if not validation_result.overall_passed:
        run.log(
            f"Validation found {validation_result.error_count} error(s). "
            f"The PR will be created with ERROR annotations — "
            f"do not merge until errors are resolved.",
            level="warning",
        )
        # Do NOT fail the workflow here — create the PR so the team can
        # see errors in context and fix them on the branch.

    # ────────────────────────────────────────────────────────────────────────
    # Stage 6 — Provider compatibility check
    # ────────────────────────────────────────────────────────────────────────
    run.log("Stage 6/7 — Checking provider compatibility")
    try:
        compat = await check_provider_compatibility(
            change_set=change_set,
            run=run,
        )
    except Exception as e:
        run.log(f"Compatibility check failed: {e} — continuing without compat data.", level="warning")
        # Build a minimal compat object so Stage 7 can still run
        from backend.ga_workflow.ga_compat import generate_versions_update
        from backend.ga_workflow.ga_models import ProviderCompatibility
        compat = ProviderCompatibility(
            repo_name=request.repo_name,
            current_version=change_set.ga_release.current_version,
            target_version=change_set.ga_release.latest_ga_version,
            all_compatible=True,
            checks=[],
            versions_tf_update=generate_versions_update(
                change_set.ga_release.current_version,
                change_set.ga_release.latest_ga_version,
            ),
        )

    run.provider_compat = compat

    # ────────────────────────────────────────────────────────────────────────
    # Stage 7 — Create or update GitHub PR
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.CHECKING_PR
    run.log("Stage 7/7 — Creating or updating GitHub PR")
    try:
        pr_result = await create_or_update_pr(
            repo_cfg=repo_cfg,
            branch_result=branch_result,
            code_changes=code_change_set,
            validation=validation_result,
            compat=compat,
            run=run,
            github_token=request.github_token,
            pr_labels=request.pr_labels,
            dry_run=request.dry_run,
        )
    except Exception as e:
        run.fail(f"PR stage failed: {e}")
        return run

    run.pr_result = pr_result

    if pr_result.action.value == "failed":
        run.log(f"PR operation failed: {pr_result.error}", level="error")
        # Don't run.fail() — the code changes are committed; only PR creation failed.
        # The team can create the PR manually.
    else:
        action_verb = {"created": "Created", "updated": "Updated", "skipped": "Skipped (dry run)"}.get(
            pr_result.action.value, pr_result.action.value
        )
        run.log(f"✅ {action_verb} PR #{pr_result.pr_number}: {pr_result.pr_url}")

    # ────────────────────────────────────────────────────────────────────────
    # Finalise + persist incremental state
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.DONE
    run.overall_success = (
        validation_result.overall_passed
        and compat.all_compatible
        and pr_result.action.value != "failed"
    )
    run.completed_at = datetime.now(timezone.utc).isoformat()

    # Persist applied changes so next run skips them (incremental mode)
    if pr_result.action.value in ("created", "updated") and new_changes:
        updated_state = mark_changes_applied(new_changes, inc_state)
        updated_state.applied_provider_version = change_set.ga_release.latest_ga_version
        # Also record service features seen this run
        if run.gcp_service_scan:
            seen = set(updated_state.service_features_seen)
            for f in run.gcp_service_scan.actionable_features:
                seen.add(f.feature_name)
            updated_state.service_features_seen = list(seen)
        save_incremental_state(updated_state)
        run.log(f"Incremental state saved — {len(updated_state.applied_change_hashes)} total applied changes")

    run.log(
        f"{'✅ Workflow complete' if run.overall_success else '⚠️ Workflow complete with warnings'} "
        f"— PR: {pr_result.pr_url or '(none)'}",
        level="info" if run.overall_success else "warning",
    )
    return run


# ── Run store helpers (used by FastAPI router) ────────────────────────────────

def get_run(run_id: str) -> Optional[WorkflowRun]:
    return _runs.get(run_id)


def list_runs() -> list[WorkflowRun]:
    return list(reversed(list(_runs.values())))


def delete_run(run_id: str) -> bool:
    if run_id in _runs:
        del _runs[run_id]
        return True
    return False


# ── CLI entry point ───────────────────────────────────────────────────────────

def _cli_main() -> None:
    parser = argparse.ArgumentParser(
        description="TerraScope GA Release Workflow — CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full workflow for one repo
  python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery

  # Dry-run (no push, no PR)
  python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --dry-run

  # Detect only — print version diff and exit
  python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --detect-only

  # Run for all enabled repos
  python -m backend.ga_workflow.ga_orchestrator --all

  # Auto-fix + custom base branch
  python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery \\
      --base-branch develop --auto-fix
        """,
    )
    parser.add_argument("--repo",         type=str,  help="Repo name (matches terrascope.config.yaml)")
    parser.add_argument("--all",          action="store_true", help="Run for all enabled repos")
    parser.add_argument("--base-branch",  type=str,  default="main")
    parser.add_argument("--dry-run",      action="store_true", help="Skip push and PR creation")
    parser.add_argument("--detect-only",  action="store_true", help="Print version diff and exit")
    parser.add_argument("--auto-fix",     action="store_true", default=True,
                        help="Auto-fix WARNING-level validation issues (default: on)")
    parser.add_argument("--no-auto-fix",  action="store_true",
                        help="Disable auto-fix")
    args = parser.parse_args()

    if not args.repo and not args.all:
        parser.error("Specify --repo NAME or --all")

    cfg = get_config()
    repos_to_run = (
        [r.name for r in cfg.enabled_repos]
        if args.all
        else [args.repo]
    )

    auto_fix = args.auto_fix and not args.no_auto_fix

    # ── detect-only mode
    if args.detect_only:
        import asyncio as _asyncio

        async def _detect_only(repo_name: str) -> None:
            from backend.ga_workflow.ga_models import WorkflowRun, WorkflowStage
            run = WorkflowRun(run_id="detect", repo_name=repo_name, gcp_product="")
            cs = await detect_ga_release(repo_name, run, github_token=None)
            if cs:
                ga = cs.ga_release
                upgrade = "YES ⚠" if ga.upgrade_required else "NO ✓"
                print(f"\n{'='*56}")
                print(f"Repo:            {repo_name}")
                print(f"Current version: {ga.current_version}")
                print(f"Latest GA:       {ga.latest_ga_version}")
                print(f"Upgrade needed:  {upgrade}")
                print(f"Breaking changes:{ga.breaking_changes}")
                print(f"New features:    {ga.new_features}")
                print(f"Changes found:   {len(cs.changes)}")
                if cs.changes:
                    print("\nChanges:")
                    for c in cs.changes:
                        b = " ⚠ BREAKING" if c.breaking else ""
                        attr = f".{c.attribute_name}" if c.attribute_name else ""
                        print(f"  [{c.change_type.value}] {c.resource_type}{attr}{b}")
            else:
                print(f"Could not detect GA release for {repo_name}")

        for repo_name in repos_to_run:
            _asyncio.run(_detect_only(repo_name))
        return

    # ── Full workflow
    for repo_name in repos_to_run:
        request = GAWorkflowRequest(
            repo_name=repo_name,
            base_branch=args.base_branch,
            dry_run=args.dry_run,
            auto_fix=auto_fix,
        )

        print(f"\n{'='*56}")
        print(f"TerraScope GA Workflow")
        print(f"   Repo:     {repo_name}")
        print(f"   Dry-run:  {args.dry_run}")
        print(f"   Auto-fix: {auto_fix}")
        print(f"{'='*56}")

        start = time.monotonic()
        run = asyncio.run(run_ga_workflow(request))
        elapsed = time.monotonic() - start

        # Print log
        print()
        for log in run.logs:
            prefix = {"error": "✗", "warning": "⚠", "info": "→"}.get(log.level, "→")
            print(f"  [{log.stage.value:20s}] {prefix} {log.message}")

        print(f"\n{'='*56}")
        status = "✅ DONE" if run.overall_success else ("❌ FAILED" if run.stage == WorkflowStage.FAILED else "⚠ DONE WITH WARNINGS")
        print(f"{status}  ({elapsed:.1f}s)")
        if run.pr_result and run.pr_result.pr_url:
            print(f"PR: {run.pr_result.pr_url}")
        if run.error:
            print(f"Error: {run.error}")


if __name__ == "__main__":
    _cli_main()
