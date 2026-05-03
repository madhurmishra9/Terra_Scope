"""
ga_pr_manager.py — Stage 7: GitHub PR creation and update.

Functions (as documented in GA_WORKFLOW_README.md §5):
  push_branch()          → git push origin <branch> --force-with-lease
  find_existing_pr()     → GET /repos/{owner}/{repo}/pulls?head=<branch>&state=open
  create_pr()            → POST /repos/{owner}/{repo}/pulls  (full template body)
  update_pr()            → PATCH /repos/{owner}/{repo}/pulls/{number} (append update section)
  create_or_update_pr()  → full Stage 7 entry point used by orchestrator

PR body follows the exact template documented in README §10:
  - Summary table
  - Changes implemented (new features + breaking)
  - Files modified table
  - Validation results table
  - Provider compatibility section
  - Reviewer checklist
  - Changelog entry block

Update appends ### Update — {date} (TerraScope Re-run) preserving all original content.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from git import Repo, GitCommandError

from backend.config import get_config, RepoConfig
from backend.ga_workflow.ga_models import (
    BranchResult,
    CodeChangeSet,
    ExistingPR,
    PRAction,
    PRResult,
    ProviderCompatibility,
    ValidationResult,
    WorkflowRun,
    WorkflowStage,
)

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 20.0


# ── Git push ──────────────────────────────────────────────────────────────────

def push_branch(
    repo_cfg: RepoConfig,
    branch_name: str,
    run: WorkflowRun,
) -> bool:
    """
    Push the GA branch to origin using force-with-lease (safe force push).
    Returns True on success, False on failure.
    """
    base = Path(__file__).parent.parent.parent
    repo_path = repo_cfg.resolved_local_path(base)

    try:
        git_repo = Repo(str(repo_path))

        # Ensure we are on the correct branch
        current = git_repo.active_branch.name
        if current != branch_name:
            git_repo.git.checkout(branch_name)

        run.log(f"Pushing {branch_name} to origin …")
        # force-with-lease: fails if remote has commits we haven't seen
        git_repo.git.push(
            "origin",
            f"{branch_name}:{branch_name}",
            "--force-with-lease",
        )
        run.log(f"✅ Pushed branch {branch_name} to origin")
        return True

    except GitCommandError as e:
        error_msg = str(e)
        if "stale info" in error_msg or "rejected" in error_msg:
            run.log(
                "Push rejected — remote branch has commits not present locally. "
                "Pull the branch and re-run the workflow.",
                level="error",
            )
        else:
            run.log(f"Git push error: {error_msg[:300]}", level="error")
        return False

    except Exception as e:
        run.log(f"Push failed: {e}", level="error")
        return False


# ── PR lookup ─────────────────────────────────────────────────────────────────

async def find_existing_pr(
    repo_cfg: RepoConfig,
    branch_name: str,
    token: str,
) -> Optional[ExistingPR]:
    """
    Search GitHub for an open PR whose head branch matches branch_name.
    Returns ExistingPR or None if not found.

    API call:
      GET /repos/{owner}/{repo}/pulls?state=open&head={owner}:{branch}
    """
    owner = getattr(repo_cfg, "github_owner", None)
    repo  = getattr(repo_cfg, "github_repo",  None)
    if not owner or not repo:
        return None

    headers = _auth_headers(token)
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    params = {
        "state": "open",
        "head": f"{owner}:{branch_name}",
        "per_page": 5,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            pulls = resp.json()

        if not pulls:
            return None

        pr = pulls[0]          # take the first (most recent) open PR
        return ExistingPR(
            number=pr["number"],
            title=pr["title"],
            url=pr["html_url"],
            branch=pr["head"]["ref"],
            state=pr["state"],
            created_at=pr["created_at"],
            updated_at=pr["updated_at"],
            body=pr.get("body") or "",
        )

    except httpx.HTTPStatusError as e:
        # 404 = repo not found or no access; treat as no PR
        return None
    except Exception:
        return None


# ── PR create ─────────────────────────────────────────────────────────────────

async def create_pr(
    repo_cfg: RepoConfig,
    branch_result: BranchResult,
    code_changes: CodeChangeSet,
    validation: ValidationResult,
    compat: ProviderCompatibility,
    run: WorkflowRun,
    token: str,
    labels: list[str],
    reviewers: list[str],
    assignees: list[str],
    draft: bool = False,
) -> PRResult:
    """
    Open a new Pull Request with the full TerraScope PR template body.

    API calls:
      POST /repos/{owner}/{repo}/pulls           — create PR
      POST /repos/{owner}/{repo}/issues/{n}/labels      — add labels
      POST /repos/{owner}/{repo}/pulls/{n}/requested_reviewers — request review
      POST /repos/{owner}/{repo}/issues/{n}/assignees   — assign
    """
    owner = getattr(repo_cfg, "github_owner", None)
    repo  = getattr(repo_cfg, "github_repo",  None)
    if not owner or not repo:
        return PRResult(
            action=PRAction.FAILED,
            error="github_owner / github_repo not set in terrascope.config.yaml for this repo.",
        )

    title = (
        f"feat: upgrade google provider to "
        f"v{compat.target_version} [GA]"
    )
    body = _build_pr_body(
        repo_cfg=repo_cfg,
        branch_result=branch_result,
        code_changes=code_changes,
        validation=validation,
        compat=compat,
        run=run,
    )

    headers = _auth_headers(token)
    payload = {
        "title":  title,
        "body":   body,
        "head":   branch_result.branch_name,
        "base":   branch_result.base_branch,
        "draft":  draft,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            # ── Create PR
            resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
                json=payload,
            )
            resp.raise_for_status()
            pr_data = resp.json()
            pr_number = pr_data["number"]
            pr_url    = pr_data["html_url"]
            run.log(f"✅ Created PR #{pr_number}: {pr_url}")

            # ── Add labels
            if labels:
                await client.post(
                    f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/labels",
                    json={"labels": labels},
                )
                run.log(f"Added labels: {labels}")

            # ── Request reviewers
            if reviewers:
                await client.post(
                    f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
                    json={"reviewers": reviewers},
                )
                run.log(f"Requested reviewers: {reviewers}")

            # ── Assign
            if assignees:
                await client.post(
                    f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/assignees",
                    json={"assignees": assignees},
                )
                run.log(f"Assigned to: {assignees}")

        return PRResult(
            action=PRAction.CREATED,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_title=title,
            pr_body=body,
            branch=branch_result.branch_name,
            target_branch=branch_result.base_branch,
        )

    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("message", "")
        except Exception:
            pass
        msg = f"GitHub API error {e.response.status_code}: {detail}"
        run.log(msg, level="error")
        return PRResult(action=PRAction.FAILED, error=msg)

    except Exception as e:
        run.log(str(e), level="error")
        return PRResult(action=PRAction.FAILED, error=str(e))


# ── PR update ─────────────────────────────────────────────────────────────────

async def update_pr(
    repo_cfg: RepoConfig,
    existing_pr: ExistingPR,
    code_changes: CodeChangeSet,
    validation: ValidationResult,
    compat: ProviderCompatibility,
    run: WorkflowRun,
    token: str,
) -> PRResult:
    """
    Append an update section to an existing PR body.
    Never replaces the original content — only appends.

    Appended block title: ### Update — {date} (TerraScope Re-run)
    """
    owner = getattr(repo_cfg, "github_owner", None)
    repo  = getattr(repo_cfg, "github_repo",  None)
    if not owner or not repo:
        return PRResult(
            action=PRAction.FAILED,
            error="github_owner / github_repo not set in terrascope.config.yaml.",
        )

    update_section = _build_update_section(code_changes, validation, compat, run)
    new_body = (existing_pr.body or "") + "\n\n---\n\n" + update_section

    headers = _auth_headers(token)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{existing_pr.number}",
                json={"body": new_body},
            )
            resp.raise_for_status()
            run.log(f"✅ Updated PR #{existing_pr.number}: {existing_pr.url}")

        return PRResult(
            action=PRAction.UPDATED,
            pr_number=existing_pr.number,
            pr_url=existing_pr.url,
            pr_title=existing_pr.title,
            pr_body=new_body,
            branch=existing_pr.branch,
            existing_pr=existing_pr,
        )

    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("message", "")
        except Exception:
            pass
        msg = f"GitHub API error {e.response.status_code}: {detail}"
        run.log(msg, level="error")
        return PRResult(action=PRAction.FAILED, error=msg, existing_pr=existing_pr)

    except Exception as e:
        run.log(str(e), level="error")
        return PRResult(action=PRAction.FAILED, error=str(e), existing_pr=existing_pr)


# ── Main entry point ──────────────────────────────────────────────────────────

async def create_or_update_pr(
    repo_cfg: RepoConfig,
    branch_result: BranchResult,
    code_changes: CodeChangeSet,
    validation: ValidationResult,
    compat: ProviderCompatibility,
    run: WorkflowRun,
    github_token: Optional[str] = None,
    pr_labels: Optional[list[str]] = None,
    pr_reviewers: Optional[list[str]] = None,
    pr_assignees: Optional[list[str]] = None,
    dry_run: bool = False,
) -> PRResult:
    """
    Full Stage 7 entry point.

    1. Push the branch (force-with-lease).
    2. Check GitHub for an existing open PR on branch_result.branch_name.
    3a. If no PR → create_pr() with full template body.
    3b. If PR exists → update_pr() appending a new update section.
    """
    run.stage = WorkflowStage.CHECKING_PR
    run.log("Checking for existing PR …")

    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        run.log(
            "No GitHub token — set GITHUB_TOKEN env var or pass github_token in request.",
            level="error",
        )
        return PRResult(
            action=PRAction.FAILED,
            error="No GitHub token available. Cannot create or update PR.",
        )

    # Dry-run: skip push + PR, return a simulated result
    if dry_run:
        run.log("DRY RUN — skipping push and PR creation.")
        return PRResult(
            action=PRAction.SKIPPED,
            pr_title=f"[DRY RUN] feat: upgrade google provider to v{compat.target_version} [GA]",
            branch=branch_result.branch_name,
            target_branch=branch_result.base_branch,
        )

    # ── Push branch
    pushed = push_branch(repo_cfg, branch_result.branch_name, run)
    if not pushed:
        return PRResult(
            action=PRAction.FAILED,
            error="Branch push failed — see logs above.",
        )

    # ── Check for existing PR
    existing = await find_existing_pr(repo_cfg, branch_result.branch_name, token)

    labels    = pr_labels    or getattr(repo_cfg, "pr_labels",    ["ga-release", "automated", "terraform"])
    reviewers = pr_reviewers or getattr(repo_cfg, "pr_reviewers", [])
    assignees = pr_assignees or getattr(repo_cfg, "pr_assignees", [])
    draft     = getattr(repo_cfg, "draft_pr", False)

    if existing:
        run.log(f"Found existing PR #{existing.number}: {existing.url}")
        run.stage = WorkflowStage.UPDATING_PR
        return await update_pr(
            repo_cfg=repo_cfg,
            existing_pr=existing,
            code_changes=code_changes,
            validation=validation,
            compat=compat,
            run=run,
            token=token,
        )
    else:
        run.log("No existing PR found — creating new PR.")
        run.stage = WorkflowStage.CREATING_PR
        return await create_pr(
            repo_cfg=repo_cfg,
            branch_result=branch_result,
            code_changes=code_changes,
            validation=validation,
            compat=compat,
            run=run,
            token=token,
            labels=labels,
            reviewers=reviewers,
            assignees=assignees,
            draft=draft,
        )


# ── PR body builders ──────────────────────────────────────────────────────────

def _build_pr_body(
    repo_cfg: RepoConfig,
    branch_result: BranchResult,
    code_changes: CodeChangeSet,
    validation: ValidationResult,
    compat: ProviderCompatibility,
    run: WorkflowRun,
) -> str:
    """Build the full PR body matching the template in README §10."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Infer change_set from code_changes (stored in run)
    change_set = run.change_set
    ga_release  = run.ga_release

    total_changes  = len(change_set.changes) if change_set else 0
    breaking       = sum(1 for c in change_set.changes if c.breaking) if change_set else 0
    new_features   = total_changes - breaking

    # ── Summary table
    lines = [
        f"> 🤖 This PR was automatically generated by **TerraScope GA Workflow**.",
        f"> Review all changes carefully before merging, especially breaking changes.",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| **Module** | `{repo_cfg.name}` |",
        f"| **GCP Product** | {repo_cfg.display_name} |",
        f"| **Current provider version** | `{compat.current_version}` |",
        f"| **Target provider version** | `{compat.target_version}` |",
        f"| **Total changes** | {total_changes} |",
        f"| **Breaking changes** | {breaking} |",
        f"| **New features** | {new_features} |",
        f"| **Branch** | `{branch_result.branch_name}` |",
        f"| **Workflow run** | `{run.run_id}` |",
        f"",
        f"---",
        f"",
        f"## Changes Implemented",
        f"",
    ]

    # ── New features table
    features = [c for c in (change_set.changes if change_set else []) if not c.breaking]
    if features:
        lines += [
            f"### New Features",
            f"",
            f"| Resource | Attribute | Description |",
            f"|----------|-----------|-------------|",
        ]
        for c in features:
            attr = f"`{c.attribute_name}`" if c.attribute_name else "—"
            lines.append(f"| `{c.resource_type}` | {attr} | {c.description[:80]} |")
        lines.append("")

    # ── Breaking changes table
    breaks = [c for c in (change_set.changes if change_set else []) if c.breaking]
    if breaks:
        lines += [
            f"### ⚠️ Breaking Changes",
            f"",
            f"| Resource | Attribute | Details |",
            f"|----------|-----------|---------|",
        ]
        for c in breaks:
            attr = f"`{c.attribute_name}`" if c.attribute_name else "—"
            lines.append(f"| `{c.resource_type}` | {attr} | {c.description[:80]} |")
        lines.append("")

        # Migration guides
        migration_changes = [c for c in breaks if c.migration_guide]
        if migration_changes:
            lines.append("### Migration Guides")
            lines.append("")
            for c in migration_changes:
                attr = f".{c.attribute_name}" if c.attribute_name else ""
                lines += [
                    f"**`{c.resource_type}{attr}`**",
                    f"",
                    f"```hcl",
                    c.migration_guide,
                    f"```",
                    f"",
                ]

    lines += [
        f"---",
        f"",
        f"## Files Modified",
        f"",
        f"| File | Changes |",
        f"|------|---------|",
    ]
    for change in code_changes.changes:
        lines.append(f"| `{change.file_path}` | {change.description[:80]} |")
    lines += [
        f"| `CHANGELOG.md` | Prepended GA upgrade entry |",
        f"",
        f"---",
        f"",
        f"## Validation Results",
        f"",
        f"| Validator | Status | Errors | Warnings |",
        f"|-----------|--------|--------|----------|",
    ]
    for report in validation.reports:
        status  = "✅ PASSED" if report.passed else "❌ FAILED"
        errors  = sum(1 for i in report.issues if i.severity.value == "error")
        warns   = sum(1 for i in report.issues if i.severity.value == "warning")
        lines.append(f"| {report.validator_name} | {status} | {errors} | {warns} |")

    # List errors if any
    all_errors = [
        i for r in validation.reports
        for i in r.issues if i.severity.value == "error"
    ]
    if all_errors:
        lines += ["", "### ❌ Validation Errors — Must Fix Before Merging", ""]
        for issue in all_errors:
            loc = f":{issue.line}" if issue.line else ""
            lines.append(f"- **`{issue.file_path}{loc}`** `[{issue.rule}]` {issue.message}")
            if issue.suggestion:
                lines.append(f"  - 💡 {issue.suggestion}")
        lines.append("")

    lines += [
        f"---",
        f"",
        f"## Provider Compatibility",
        f"",
    ]

    if compat.all_compatible:
        lines.append(f"All {len(compat.checks)} changes verified against provider schema v{compat.target_version}. ✅")
    else:
        incompat = [c for c in compat.checks if not c.supported]
        lines += [
            f"⚠️ **{len(incompat)} compatibility issue(s) detected** — manual review required.",
            f"",
            f"| Resource | Attribute | Notes |",
            f"|----------|-----------|-------|",
        ]
        for c in incompat:
            attr = c.attribute_name or "—"
            lines.append(f"| `{c.resource_type}` | `{attr}` | {c.notes or 'Not found in schema'} |")

    lines += [
        f"",
        f"---",
        f"",
        f"## Reviewer Checklist",
        f"",
        f"- [ ] Provider version constraint range is correct (`{compat.versions_tf_update and _extract_constraint(compat.versions_tf_update) or 'see versions.tf'}`)",
        f"- [ ] Breaking changes have migration notes for downstream consumers",
        f"- [ ] New variables have descriptions and appropriate defaults",
        f"- [ ] CHANGELOG.md entry is accurate",
        f"- [ ] No existing tests are broken by the changes",
        f"- [ ] Module examples/ directory updated if affected",
        f"",
        f"---",
        f"",
        f"## Changelog Entry",
        f"",
        f"<details>",
        f"<summary>Click to expand</summary>",
        f"",
        f"```markdown",
        code_changes.changelog_entry,
        f"```",
        f"",
        f"</details>",
        f"",
        f"---",
        f"",
        f"_Generated by [TerraScope](https://github.com/your-org/terrascope) "
        f"on {now_utc}_",
    ]

    return "\n".join(lines)


def _build_update_section(
    code_changes: CodeChangeSet,
    validation: ValidationResult,
    compat: ProviderCompatibility,
    run: WorkflowRun,
) -> str:
    """Build the appended update section for an existing PR body."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    change_set = run.change_set

    lines = [
        f"### Update — {date_str} (TerraScope Re-run)",
        f"",
        f"**New target version:** `{compat.target_version}`  ",
        f"**Workflow run:** `{run.run_id}`  ",
        f"**Changes in this run:** {len(change_set.changes) if change_set else 0}",
        f"",
    ]

    if change_set and change_set.changes:
        lines += [
            f"| Change | Resource | Attribute | Breaking |",
            f"|--------|----------|-----------|----------|",
        ]
        for c in change_set.changes:
            attr = f"`{c.attribute_name}`" if c.attribute_name else "—"
            breaking = "Yes ⚠️" if c.breaking else "No"
            lines.append(
                f"| {c.change_type.value} | `{c.resource_type}` | {attr} | {breaking} |"
            )
        lines.append("")

    lines += [
        f"**Files updated:** {', '.join(f'`{c.file_path}`' for c in code_changes.changes)}  ",
        f"**Validation:** {'✅ PASSED' if validation.overall_passed else f'❌ {validation.error_count} errors'}  ",
        f"**Commit:** `{code_changes.commit_message.splitlines()[0][:60]}`",
        f"",
        f"_Updated by TerraScope GA Workflow on {date_str}_",
    ]

    return "\n".join(lines)


# ── Misc helpers ──────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _extract_constraint(versions_tf: str) -> str:
    """Extract the version constraint string from a versions.tf HCL block."""
    m = re.search(r'version\s*=\s*"([^"]+)"', versions_tf)
    return m.group(1) if m else ""


import re  # needed by _extract_constraint
