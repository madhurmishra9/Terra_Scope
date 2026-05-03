"""
ga_implementer.py — Generates and applies HCL code changes for GA provider features.

Pipeline:
  1. Receive GAChangeSet (what needs changing)
  2. Read current file content at the latest tag
  3. Use LLM to generate precise HCL diffs
  4. Apply changes to the working tree of the local repo
  5. Commit to the GA branch
"""
from __future__ import annotations

import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional

from git import Repo, GitCommandError

from backend.config import get_config
from backend.agent.tools.git_tools import get_file_at_tag, list_tf_files_at_tag, _get_repo
from backend.agent.tools.hcl_tools import parse_hcl_content, get_provider_requirements
from backend.ga_workflow.ga_models import (
    GAChangeSet, GAChange, ChangeType,
    CodeChange, CodeChangeSet, BranchResult,
    WorkflowRun, WorkflowStage,
)


# ── Branch management ─────────────────────────────────────────────────────────

def create_ga_branch(
    repo_name: str,
    ga_version: str,
    base_branch: str,
    run: WorkflowRun,
) -> BranchResult:
    """
    Create (or reuse) a GA upgrade branch.
    Branch name format: terrascope/ga-upgrade-v{version}
    """
    run.stage = WorkflowStage.BRANCHING
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return BranchResult(repo_name=repo_name, branch_name="", base_branch=base_branch,
                            created=False, error=f"Repo '{repo_name}' not found")

    safe_version = ga_version.replace(".", "-")
    branch_name = f"terrascope/ga-upgrade-v{safe_version}"
    run.log(f"Target branch: {branch_name}")

    try:
        git_repo = _get_repo(repo_cfg)

        # Fetch latest from remote
        try:
            git_repo.remotes.origin.fetch()
            run.log("Fetched latest from origin")
        except Exception:
            run.log("Could not fetch from origin (local-only mode)", level="warning")

        # Check if branch already exists (local or remote)
        local_branches = [b.name for b in git_repo.branches]
        remote_branches = []
        try:
            remote_branches = [r.name.replace("origin/", "") for r in git_repo.remote("origin").refs]
        except Exception:
            pass

        if branch_name in local_branches:
            run.log(f"Branch '{branch_name}' already exists locally — reusing it")
            git_repo.git.checkout(branch_name)
            return BranchResult(
                repo_name=repo_name,
                branch_name=branch_name,
                base_branch=base_branch,
                created=False,
                already_existed=True,
            )

        if branch_name in remote_branches:
            run.log(f"Branch '{branch_name}' exists on remote — checking out")
            git_repo.git.checkout("-b", branch_name, f"origin/{branch_name}")
            return BranchResult(
                repo_name=repo_name,
                branch_name=branch_name,
                base_branch=base_branch,
                created=False,
                already_existed=True,
            )

        # Create new branch from base
        run.log(f"Creating new branch from {base_branch}")
        try:
            git_repo.git.checkout(base_branch)
        except GitCommandError:
            git_repo.git.checkout("HEAD")

        git_repo.git.checkout("-b", branch_name)
        run.log(f"✅ Created branch '{branch_name}'")

        return BranchResult(
            repo_name=repo_name,
            branch_name=branch_name,
            base_branch=base_branch,
            created=True,
        )

    except Exception as e:
        return BranchResult(
            repo_name=repo_name,
            branch_name=branch_name,
            base_branch=base_branch,
            created=False,
            error=str(e),
        )


# ── LLM-powered code generation ───────────────────────────────────────────────

async def generate_code_changes(
    change_set: GAChangeSet,
    run: WorkflowRun,
) -> list[CodeChange]:
    """
    Use the local LLM to generate precise HCL code changes for each GAChange.
    Returns a list of CodeChange objects (file edits + additions).
    """
    run.stage = WorkflowStage.IMPLEMENTING
    run.log(f"Generating code changes for {len(change_set.changes)} GA changes")

    cfg = get_config()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    code_changes: list[CodeChange] = []
    repo_name = change_set.repo_name
    current_tag = change_set.current_tag

    # Process deterministic changes first (provider version bump)
    for ga_change in change_set.changes:
        if ga_change.change_type == ChangeType.PROVIDER_VERSION:
            vc = _generate_version_bump(repo_name, current_tag,
                                        change_set.ga_release.latest_ga_version, ga_change)
            if vc:
                code_changes.append(vc)
                run.log(f"Generated provider version bump → versions.tf")

    # For each .tf file that needs modification, read it and ask LLM for changes
    for file_path in change_set.files_to_modify:
        current_content = get_file_at_tag(repo_name, current_tag, file_path)
        if not current_content:
            run.log(f"Could not read {file_path}@{current_tag} — skipping", level="warning")
            continue

        # Filter changes relevant to this file
        relevant_changes = _changes_for_file(change_set.changes, file_path)
        if not relevant_changes:
            continue

        run.log(f"Generating changes for {file_path} ({len(relevant_changes)} changes)")

        changes_desc = "\n".join(
            f"- [{c.change_type.value}] {c.resource_type}"
            + (f".{c.attribute_name}" if c.attribute_name else "")
            + f": {c.description[:120]}"
            for c in relevant_changes
        )

        prompt = f"""You are a Terraform HCL expert implementing Google provider GA changes.

FILE: {file_path}
CURRENT CONTENT:
```hcl
{current_content[:6000]}
```

CHANGES TO IMPLEMENT:
{changes_desc}

Instructions:
1. Generate the COMPLETE updated file content with all changes applied
2. For new arguments: add them with sensible defaults or as optional variables
3. For deprecated arguments: add a comment "# Deprecated in provider v{change_set.ga_release.latest_ga_version} — remove in next major version"
4. For new resources: add a complete resource block with all required arguments
5. Preserve all existing code — only add or annotate, never remove existing working code
6. Follow HCL2 syntax exactly
7. Add a comment above each change: "# GA v{change_set.ga_release.latest_ga_version}: <description>"

Return ONLY the complete updated file content as valid HCL. No markdown fences, no explanation."""

        try:
            resp = await client.chat.completions.create(
                model=cfg.llm.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=4000,
            )
            new_content = resp.choices[0].message.content.strip()
            # Strip any accidental markdown fences
            new_content = re.sub(r"^```(?:hcl|terraform)?\n?", "", new_content)
            new_content = re.sub(r"\n?```$", "", new_content).strip()

            if new_content and new_content != current_content:
                code_changes.append(CodeChange(
                    file_path=file_path,
                    change_type=relevant_changes[0].change_type,
                    description=f"GA v{change_set.ga_release.latest_ga_version}: "
                                + "; ".join(c.description[:60] for c in relevant_changes[:3]),
                    old_content=current_content,
                    new_content=new_content,
                    ga_change=relevant_changes[0],
                ))
                run.log(f"✅ Generated changes for {file_path}")
            else:
                run.log(f"No effective changes for {file_path} (content unchanged)", level="info")

        except Exception as e:
            run.log(f"LLM error for {file_path}: {e}", level="warning")
            # Fallback: generate a minimal manual change
            fallback = _generate_fallback_change(file_path, current_content,
                                                  relevant_changes, change_set)
            if fallback:
                code_changes.append(fallback)

    run.log(f"Generated {len(code_changes)} code changes total")
    return code_changes


def _generate_version_bump(
    repo_name: str,
    tag: str,
    new_version: str,
    ga_change: GAChange,
) -> Optional[CodeChange]:
    """Generate a versions.tf update to bump the provider version."""
    current_content = get_file_at_tag(repo_name, tag, "versions.tf")
    if not current_content:
        # Try main.tf
        current_content = get_file_at_tag(repo_name, tag, "main.tf")
        file_path = "main.tf"
    else:
        file_path = "versions.tf"

    if not current_content:
        return None

    # Extract major.minor from new version
    m = re.match(r"(\d+)\.(\d+)", new_version)
    if not m:
        return None
    major, minor = m.group(1), m.group(2)

    # Build a new version constraint: ">= X.Y, < (X+1).0"
    new_constraint = f">= {major}.{minor}, < {int(major) + 1}.0"

    # Replace existing version constraint
    new_content = re.sub(
        r'(version\s*=\s*")[^"]*(")',
        f'\\1{new_constraint}\\2',
        current_content,
        flags=re.IGNORECASE,
    )

    # Add GA comment if the substitution worked
    if new_content != current_content:
        new_content = f"# Updated by TerraScope GA workflow — provider v{new_version}\n" + new_content
        return CodeChange(
            file_path=file_path,
            change_type=ChangeType.PROVIDER_VERSION,
            description=f"Bump Google provider version constraint to {new_constraint}",
            old_content=current_content,
            new_content=new_content,
            ga_change=ga_change,
        )
    return None


def _generate_fallback_change(
    file_path: str,
    current_content: str,
    changes: list[GAChange],
    change_set: GAChangeSet,
) -> Optional[CodeChange]:
    """
    Minimal fallback: add a comment block describing the changes
    when the LLM cannot generate the full implementation.
    """
    comment_lines = [
        f"",
        f"# ================================================================",
        f"# TODO: GA v{change_set.ga_release.latest_ga_version} — Manual implementation required",
        f"# The following changes need to be implemented:",
    ]
    for c in changes:
        comment_lines.append(f"#   [{c.change_type.value}] {c.resource_type}: {c.description[:100]}")
    comment_lines.append(f"# ================================================================")
    comment_lines.append(f"")

    todo_block = "\n".join(comment_lines)
    new_content = current_content + "\n" + todo_block

    return CodeChange(
        file_path=file_path,
        change_type=changes[0].change_type,
        description=f"TODO comments for GA v{change_set.ga_release.latest_ga_version} changes",
        old_content=current_content,
        new_content=new_content,
        ga_change=changes[0],
    )


# ── Apply changes to disk ─────────────────────────────────────────────────────

def apply_code_changes(
    change_set: GAChangeSet,
    code_changes: list[CodeChange],
    branch_result: BranchResult,
    run: WorkflowRun,
    dry_run: bool = False,
) -> CodeChangeSet:
    """
    Write all generated code changes to the working tree and commit them.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(change_set.repo_name)
    base = Path(__file__).parent.parent.parent

    apply_errors: list[str] = []
    commit_msg = (
        f"feat: upgrade google provider to v{change_set.ga_release.latest_ga_version}\n\n"
        f"- Implements {len(code_changes)} GA changes for {change_set.gcp_product}\n"
        f"- Provider: {change_set.ga_release.current_version} → {change_set.ga_release.latest_ga_version}\n"
        f"- Breaking changes: {change_set.ga_release.breaking_changes}\n"
        f"- New features: {change_set.ga_release.new_features}\n\n"
        f"Generated by TerraScope GA Workflow"
    )

    changelog_entry = _build_changelog_entry(change_set, code_changes)

    code_change_set = CodeChangeSet(
        repo_name=change_set.repo_name,
        branch_name=branch_result.branch_name,
        changes=code_changes,
        changelog_entry=changelog_entry,
        commit_message=commit_msg,
    )

    if dry_run:
        run.log("DRY RUN — changes not written to disk")
        code_change_set.applied = True
        return code_change_set

    repo_path = repo_cfg.resolved_local_path(base)

    try:
        git_repo = Repo(str(repo_path))

        # Write each changed file
        for change in code_changes:
            file_abs = repo_path / change.file_path.replace("/", Path.cwd().root)
            file_abs = repo_path / Path(change.file_path)
            file_abs.parent.mkdir(parents=True, exist_ok=True)

            try:
                file_abs.write_text(change.new_content, encoding="utf-8")
                run.log(f"Written: {change.file_path}")
            except Exception as e:
                apply_errors.append(f"Write failed for {change.file_path}: {e}")
                run.log(f"Write error: {change.file_path}: {e}", level="error")

        # Prepend to CHANGELOG.md
        changelog_path = repo_path / "CHANGELOG.md"
        existing_changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
        changelog_path.write_text(changelog_entry + "\n\n" + existing_changelog, encoding="utf-8")
        run.log("Updated CHANGELOG.md")

        if not apply_errors:
            # Stage all changes
            git_repo.git.add("-A")
            git_repo.index.commit(commit_msg)
            run.log(f"✅ Committed: {commit_msg.splitlines()[0]}")
            code_change_set.applied = True
        else:
            run.log(f"{len(apply_errors)} files failed to write — skipping commit", level="error")
            code_change_set.apply_errors = apply_errors

    except Exception as e:
        apply_errors.append(str(e))
        run.log(f"Apply error: {e}", level="error")
        code_change_set.apply_errors = apply_errors

    return code_change_set


# ── Helpers ───────────────────────────────────────────────────────────────────

def _changes_for_file(changes: list[GAChange], file_path: str) -> list[GAChange]:
    """Return changes that are likely relevant to a specific .tf file."""
    fname = file_path.lower()
    relevant = []
    for c in changes:
        if "main.tf" in fname:
            relevant.append(c)
        elif "variables.tf" in fname and c.change_type in (
            ChangeType.NEW_VARIABLE, ChangeType.UPDATED_VARIABLE, ChangeType.REMOVED_VARIABLE
        ):
            relevant.append(c)
        elif "outputs.tf" in fname and c.change_type == ChangeType.NEW_OUTPUT:
            relevant.append(c)
        elif "versions.tf" in fname and c.change_type == ChangeType.PROVIDER_VERSION:
            relevant.append(c)
        elif "iam" in fname and c.change_type == ChangeType.IAM_CHANGE:
            relevant.append(c)
    return relevant or changes  # fallback: all changes if no file-specific match


def _build_changelog_entry(change_set: GAChangeSet, code_changes: list[CodeChange]) -> str:
    """Build a CHANGELOG.md entry for the GA upgrade."""
    now = datetime.utcnow().strftime("%Y-%m-%d")
    version = change_set.ga_release.latest_ga_version
    lines = [
        f"## [Unreleased] — GA Provider Upgrade v{version} ({now})",
        f"",
        f"### Provider",
        f"- Upgraded `hashicorp/google` from `v{change_set.ga_release.current_version}` "
        f"to `v{version}`",
        f"",
    ]

    new_features = [c for c in change_set.changes if not c.breaking]
    breaking = [c for c in change_set.changes if c.breaking]

    if new_features:
        lines.append("### Added")
        for c in new_features:
            attr = f"`.{c.attribute_name}`" if c.attribute_name else ""
            lines.append(f"- `{c.resource_type}`{attr}: {c.description[:100]}")
        lines.append("")

    if breaking:
        lines.append("### Breaking Changes")
        for c in breaking:
            attr = f"`.{c.attribute_name}`" if c.attribute_name else ""
            lines.append(f"- ⚠️ `{c.resource_type}`{attr}: {c.description[:100]}")
        lines.append("")

    lines += [
        f"### Files Modified",
    ]
    for cc in code_changes:
        lines.append(f"- `{cc.file_path}`: {cc.description[:80]}")

    lines += [
        "",
        f"_Generated by TerraScope GA Workflow on {now}_",
    ]
    return "\n".join(lines)
