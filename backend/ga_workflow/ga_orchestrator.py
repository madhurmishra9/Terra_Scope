"""
ga_orchestrator.py — GA Workflow pipeline coordinator.

Executes all seven stages in sequence, updating the WorkflowRun state machine
after each stage. Any unrecovered exception in a stage calls run.fail() and
returns early — downstream stages are not executed.

Public API:
  run_ga_workflow(request) → WorkflowRun   (used by FastAPI router)

CLI entry point:
  python -m backend.ga_workflow.ga_orchestrator --repo NAME [options]
"""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from backend.config import get_config
from backend.ga_workflow.ga_models import (
    GAWorkflowRequest,
    WorkflowRun,
    WorkflowStage,
)
from backend.ga_workflow.ga_detector import detect_ga_release
from backend.ga_workflow.gcp_service_scanner import scan_gcp_service
from backend.ga_workflow.ga_implementer import (
    create_ga_branch,
    generate_code_changes,
    apply_code_changes,
)
from backend.ga_workflow.ga_validators import validate_all
from backend.ga_workflow.ga_compat import check_provider_compatibility
from backend.ga_workflow.ga_pr_manager import create_or_update_pr
from backend.ga_workflow.gcp_service_detector import scan_gcp_service_features
from backend.ga_workflow.ga_models import GCPServiceScanResult, GCPServiceFeatureModel

# In-memory store of all workflow runs (keyed by run_id).
# In production this could be Redis or a DB table; in-process is fine for
# a single-server curation tool.
_runs: dict[str, WorkflowRun] = {}


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

    run.ga_release  = change_set.ga_release
    run.change_set  = change_set

    # ────────────────────────────────────────────────────────────────────────
    # Stage 1b — Scan GCP service for new GA features (runs alongside provider check)
    # This is a separate signal: "what did GOOGLE CLOUD announce for this product?"
    # independent of provider version changes.
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.SCANNING_SERVICE
    run.log("Stage 1b/7 — Scanning GCP service release notes for new GA features")
    try:
        gcp_scan = await scan_gcp_service_features(
            repo_name=request.repo_name,
            run=run,
            days_back=180,
        )
        if gcp_scan:
            # Convert to Pydantic model for WorkflowRun serialization
            run.gcp_service_scan = GCPServiceScanResult(
                repo_name=gcp_scan.repo_name,
                gcp_product=gcp_scan.gcp_product,
                scan_date=gcp_scan.scan_date,
                total_features=len(gcp_scan.features),
                actionable_count=len(gcp_scan.actionable_features),
                features=[GCPServiceFeatureModel(**f.to_dict()) for f in gcp_scan.features],
                actionable_features=[GCPServiceFeatureModel(**f.to_dict()) for f in gcp_scan.actionable_features],
                module_resources=gcp_scan.module_resources,
                summary=gcp_scan.summary,
            )
            run.log(
                f"GCP service scan: {gcp_scan.actionable_count} actionable new GA features "
                f"found for {gcp_scan.gcp_product}"
            )
            # Merge actionable GCP service features into the change_set as additional GAChanges
            from backend.ga_workflow.ga_models import GAChange, ChangeType
            for feat in gcp_scan.actionable_features:
                ct = (ChangeType.NEW_RESOURCE if feat.terraform_impact == "new_resource"
                      else ChangeType.NEW_ARGUMENT)
                for res_type in (feat.terraform_resources or [f"google_{gcp_scan.gcp_product}_resource"]):
                    change_set.changes.append(GAChange(
                        change_type=ct,
                        resource_type=res_type,
                        attribute_name=feat.terraform_args[0] if feat.terraform_args else None,
                        description=f"[GCP Service GA] {feat.feature_name}: {feat.description[:150]}",
                        provider_version=change_set.ga_release.latest_ga_version,
                        breaking=False,
                        source_url=feat.source_url,
                    ))
            run.log(f"Merged {len(gcp_scan.actionable_features)} GCP service features into change set")
    except Exception as e:
        run.log(f"GCP service scan failed ({e}) — continuing with provider-only changes", level="warning")

    if not change_set.ga_release.upgrade_required:
        run.log("Module is already on the latest GA provider version. Nothing to do.")
        run.stage = WorkflowStage.DONE
        run.overall_success = True
        run.completed_at = datetime.now(timezone.utc).isoformat()
        return run

    # ────────────────────────────────────────────────────────────────────────
    # Stage 1b — Scan GCP service for new GA features beyond provider changelog
    # ────────────────────────────────────────────────────────────────────────
    run.log("Stage 1b/7 — Scanning GCP service for new GA features")
    try:
        gcp_scan = await scan_gcp_service(repo_name=request.repo_name, run=run)
        run.gcp_service_scan = gcp_scan
        run.log(
            f"GCP service scan: {gcp_scan.total_features} features, "
            f"{gcp_scan.actionable_count} actionable gaps"
        )
    except Exception as e:
        run.log(f"GCP service scan failed (non-critical): {e}", level="warning")

    run.log(
        f"Upgrade: v{change_set.ga_release.current_version} → "
        f"v{change_set.ga_release.latest_ga_version} "
        f"({len(change_set.changes)} changes, "
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
    # Finalise
    # ────────────────────────────────────────────────────────────────────────
    run.stage = WorkflowStage.DONE
    run.overall_success = (
        validation_result.overall_passed
        and compat.all_compatible
        and pr_result.action.value != "failed"
    )
    run.completed_at = datetime.now(timezone.utc).isoformat()
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
        print(f"🚀 TerraScope GA Workflow")
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
