# 🚀 TerraScope — GA Release Workflow

> **Automated GA provider upgrade pipeline for Google Cloud Terraform modules.**  
> Detects the latest GA provider release, analyzes what changed, creates a branch, implements HCL changes, validates code, checks provider compatibility, and opens (or updates) a GitHub PR — all from one button or API call.

This document covers the GA Workflow feature added on top of the core TerraScope agent. For base TerraScope setup (Ollama, repo indexing, query UI), see [`README.md`](./README.md).

---

## Table of Contents

1. [What the GA Workflow Does](#1-what-the-ga-workflow-does)
2. [Full Pipeline Architecture](#2-full-pipeline-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Setup](#4-setup)
   - [GitHub Token Configuration](#41-github-token-configuration)
   - [Config File Changes](#42-config-file-changes)
   - [Additional Dependencies](#43-additional-dependencies)
5. [Project Structure — GA Workflow Files](#5-project-structure--ga-workflow-files)
6. [Running the GA Workflow](#6-running-the-ga-workflow)
   - [Via the UI](#61-via-the-ui)
   - [Via the API](#62-via-the-api)
   - [Via the CLI](#63-via-the-cli)
7. [Stage-by-Stage Deep Dive](#7-stage-by-stage-deep-dive)
   - [Stage 1 — GA Release Detection](#stage-1--ga-release-detection)
   - [Stage 2 — Change Analysis](#stage-2--change-analysis)
   - [Stage 3 — Branch Creation](#stage-3--branch-creation)
   - [Stage 4 — Code Implementation](#stage-4--code-implementation)
   - [Stage 5 — Validation](#stage-5--validation)
   - [Stage 6 — Provider Compatibility](#stage-6--provider-compatibility)
   - [Stage 7 — PR Management](#stage-7--pr-management)
8. [Code Module Reference](#8-code-module-reference)
   - [ga_models.py](#81-ga_modelspy)
   - [ga_detector.py](#82-ga_detectorpy)
   - [ga_implementer.py](#83-ga_implementerpy)
9. [Worked Examples](#9-worked-examples)
   - [Example A — BigQuery New Argument](#example-a--bigquery-new-argument)
   - [Example B — Provider Version Bump](#example-b--provider-version-bump)
   - [Example C — Breaking Change with Migration](#example-c--breaking-change-with-migration)
   - [Example D — Existing PR Update](#example-d--existing-pr-update)
10. [PR Structure and Templates](#10-pr-structure-and-templates)
11. [Validation Rules Reference](#11-validation-rules-reference)
12. [Provider Compatibility Checks](#12-provider-compatibility-checks)
13. [API Reference — GA Endpoints](#13-api-reference--ga-endpoints)
14. [Configuration Reference](#14-configuration-reference)
15. [Troubleshooting](#15-troubleshooting)
16. [FAQ](#16-faq)

---

## 1. What the GA Workflow Does

The GA Workflow is a seven-stage automated pipeline triggered per GCP product repo. Given a repo name, it:

| # | Stage | What Happens |
|---|-------|-------------|
| 1 | **Detect** | Queries Terraform Registry API for latest GA provider version; reads current version from `versions.tf` |
| 2 | **Analyze** | Fetches provider CHANGELOG, uses local LLM to map changes to resources used in this module |
| 3 | **Branch** | Creates `terrascope/ga-upgrade-vX.Y.Z` off `main`; reuses the branch if it already exists |
| 4 | **Implement** | Generates updated HCL for every affected `.tf` file; prepends a CHANGELOG.md entry; commits |
| 5 | **Validate** | Runs HCL syntax check, required-attribute check, naming convention check, variable type check |
| 6 | **Compat** | Verifies every new attribute/resource exists in the target provider version schema |
| 7 | **PR** | Checks GitHub for an existing open PR on the branch; **creates** one if absent, **updates** body/notes if present |

Every stage is independently logged and its output is returned to the UI as structured JSON so the team sees live progress.

---

## 2. Full Pipeline Architecture

```
TerraScope GA Workflow — Data Flow
═══════════════════════════════════════════════════════════════════════

  [UI Button: "Run GA Workflow"]
          │
          ▼
  POST /api/ga/workflow
          │
          ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  ga_orchestrator.py  (WorkflowRun state machine)            │
  │                                                              │
  │  Stage 1: ga_detector.detect_ga_release()                   │
  │    ├─ GET registry.terraform.io/v1/providers/hashicorp/google│
  │    ├─ GET raw.githubusercontent.com/.../CHANGELOG.md        │
  │    ├─ GET api.github.com/repos/.../releases/tags/vX.Y.Z     │
  │    ├─ Read versions.tf via GitPython (current version)      │
  │    ├─ Read all .tf files via HCL parser (resource inventory)│
  │    └─ Ollama/Gemma 3 4B → GAChangeSet (structured changes)  │
  │                                                              │
  │  Stage 2: ga_implementer.create_ga_branch()                 │
  │    ├─ GitPython: fetch origin                               │
  │    ├─ Check local + remote branch existence                 │
  │    └─ git checkout -b terrascope/ga-upgrade-vX-Y-Z          │
  │                                                              │
  │  Stage 3: ga_implementer.generate_code_changes()            │
  │    ├─ For each .tf file in change_set.files_to_modify:      │
  │    │    Read current content → Ollama → new HCL content     │
  │    ├─ Deterministic: versions.tf version constraint bump    │
  │    └─ Fallback: TODO comment blocks if LLM fails            │
  │                                                              │
  │  Stage 4: ga_implementer.apply_code_changes()               │
  │    ├─ Write files to repo working tree                      │
  │    ├─ Prepend CHANGELOG.md entry                            │
  │    └─ git add -A && git commit                              │
  │                                                              │
  │  Stage 5: ga_validators.validate_all()                      │
  │    ├─ HCL syntax validator (python-hcl2 parse)              │
  │    ├─ Required attribute validator                          │
  │    ├─ Naming convention validator                           │
  │    ├─ Variable type validator                               │
  │    └─ Auto-fix minor issues if auto_fix=True                │
  │                                                              │
  │  Stage 6: ga_compat.check_provider_compatibility()          │
  │    ├─ GET registry.terraform.io/.../schema                  │
  │    ├─ Check every new attribute exists in target version    │
  │    └─ Generate versions.tf update if needed                 │
  │                                                              │
  │  Stage 7: ga_pr_manager.create_or_update_pr()               │
  │    ├─ git push origin terrascope/ga-upgrade-vX-Y-Z          │
  │    ├─ GET api.github.com/repos/{owner}/{repo}/pulls         │
  │    │    (filter by head branch)                             │
  │    ├─ If PR exists → PATCH (update body + append notes)     │
  │    └─ If no PR → POST (create with full template body)      │
  │                                                              │
  │  Return: WorkflowRun (all stages, logs, PR URL)             │
  └──────────────────────────────────────────────────────────────┘
          │
          ▼
  UI renders live progress per stage + final PR link
```

---

## 3. Prerequisites

Everything from the base TerraScope README, plus:

| Requirement | Purpose |
|-------------|---------|
| GitHub Personal Access Token | Creating and updating PRs via GitHub API |
| Internet access | Terraform Registry API + GitHub API + GitHub raw content |
| Git remote configured | The repos under `./repos/` must have `origin` pointing to GitHub |
| `GITHUB_TOKEN` env var | Preferred over passing token in request body |

The workflow does **not** require:
- Terraform CLI installed (no `terraform validate` — uses python-hcl2 instead)
- GitHub CLI (`gh`)
- Any CI system

---

## 4. Setup

### 4.1 GitHub Token Configuration

The workflow needs a GitHub Personal Access Token (classic or fine-grained) with these permissions:

| Permission | Needed For |
|-----------|-----------|
| `repo` (classic) or `contents: write` + `pull_requests: write` | Push branch, create/update PR |
| `read:org` (if repo is org-owned) | List existing PRs |

**Create the token:**

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Select scopes: `repo` (includes all sub-scopes needed)
4. Copy the token — you won't see it again

**Set as environment variable (recommended — never put tokens in config files):**

```bash
# Mac/Linux — add to ~/.zshrc or ~/.bashrc for persistence
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Windows PowerShell — add to $PROFILE for persistence
$env:GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Windows Command Prompt
set GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Verify:**
```bash
# Mac/Linux
echo $GITHUB_TOKEN

# Windows PowerShell
echo $env:GITHUB_TOKEN
```

The backend reads `GITHUB_TOKEN` automatically. You can also pass `github_token` in the API request body for per-request override (useful for testing with different tokens).

### 4.2 Config File Changes

Add the GitHub section to `terrascope.config.yaml`:

```yaml
terrascope:
  # ... existing llm, grounding, vector_store sections ...

  # ── GitHub / PR Settings ────────────────────────────────────
  github:
    default_base_branch: main         # Branch to base GA branches off
    pr_labels:
      - ga-release
      - automated
      - terraform
    pr_assignees: []                  # GitHub usernames to auto-assign PRs to
    pr_reviewers: []                  # GitHub usernames to request review from
    draft_pr: false                   # Open PRs as drafts (useful for review-first workflows)

repos:
  - name: terraform-google-bigquery
    display_name: BigQuery
    local_path: ./repos/terraform-google-bigquery
    gcp_product: bigquery
    description: "Terraform modules for Google BigQuery datasets, tables, IAM"
    enabled: true
    # ── GitHub repo info for PR creation ──────────────────────
    github_owner: your-org            # GitHub org or user who owns the repo
    github_repo: terraform-google-bigquery  # GitHub repo name (usually same as name)
```

`github_owner` and `github_repo` are used to construct the GitHub API URL:
`https://api.github.com/repos/{github_owner}/{github_repo}/pulls`

### 4.3 Additional Dependencies

The GA Workflow adds these Python packages on top of the base `requirements.txt`:

```bash
pip install httpx openai PyGithub
```

Or install everything at once:

```bash
pip install -r requirements.txt -r requirements-ga.txt
```

**`requirements-ga.txt`:**
```
# GA Workflow additional dependencies
httpx>=0.26.0          # Async HTTP for Registry + GitHub APIs (already in base)
openai>=1.10.0         # OpenAI-compatible client for Ollama (already in base)
PyGithub>=2.1.1        # GitHub API client for PR management
```

---

## 5. Project Structure — GA Workflow Files

```
terrascope/
└── backend/
    └── ga_workflow/
        ├── __init__.py
        │
        ├── ga_models.py          ← All Pydantic models for the pipeline
        │                            WorkflowRun, GARelease, GAChangeSet,
        │                            CodeChange, ValidationResult, PRResult, …
        │
        ├── ga_detector.py        ← Stage 1+2: GA version detection + change analysis
        │                            fetch_latest_ga_version()     → Terraform Registry API
        │                            fetch_provider_changelog()    → GitHub raw CHANGELOG
        │                            fetch_github_release_notes()  → GitHub Releases API
        │                            parse_changelog_to_changes()  → regex-based parser
        │                            analyze_changes_with_llm()    → Ollama/Gemma 3 4B
        │                            detect_ga_release()           → full Stage 1+2 entry point
        │
        ├── ga_implementer.py     ← Stage 3+4: Branch creation + code generation + commit
        │                            create_ga_branch()            → GitPython branch ops
        │                            generate_code_changes()       → Ollama generates HCL
        │                            apply_code_changes()          → write files + git commit
        │                            _generate_version_bump()      → deterministic versions.tf
        │
        ├── ga_validators.py      ← Stage 5: Code validation (4 independent validators)
        │                            validate_hcl_syntax()         → python-hcl2 parse
        │                            validate_required_attributes() → resource schema check
        │                            validate_naming_conventions()  → curation standards
        │                            validate_variable_types()     → type system check
        │                            validate_all()                → runs all, aggregates
        │
        ├── ga_compat.py          ← Stage 6: Provider compatibility check
        │                            fetch_provider_schema()       → Registry schema API
        │                            check_provider_compatibility() → attribute existence
        │                            generate_versions_update()    → versions.tf HCL patch
        │
        ├── ga_pr_manager.py      ← Stage 7: GitHub PR create + update
        │                            find_existing_pr()            → GET /pulls?head=branch
        │                            create_pr()                   → POST /pulls
        │                            update_pr()                   → PATCH /pulls/{number}
        │                            push_branch()                 → git push origin
        │                            create_or_update_pr()         → full Stage 7 entry point
        │
        └── ga_orchestrator.py    ← Pipeline coordinator
                                     run_ga_workflow()             → executes all 7 stages
                                     WorkflowRun state machine     → tracks progress + logs
```

---

## 6. Running the GA Workflow

### 6.1 Via the UI

The GA Workflow adds a new **"GA Release"** tab to the TerraScope sidebar (alongside the existing REPOS and TAGS tabs).

**Step-by-step:**

1. Start TerraScope (both backend and frontend, as in the base README).
2. In the sidebar, click the **"GA"** tab.
3. Select the target repo from the dropdown (e.g., `terraform-google-bigquery`).
4. Review the auto-detected settings:
   - Base branch (default: `main`)
   - Dry run toggle (does everything except push + PR)
   - Auto-fix toggle (auto-corrects minor validation issues)
5. Click **"Run GA Workflow"**.
6. Watch the live progress panel — each stage updates in real time with a status dot and log lines.
7. When complete, the panel shows:
   - Provider version comparison (current → latest)
   - List of detected changes with type badges
   - Validation report (errors, warnings)
   - PR link (opens in a new tab)

**Live progress example:**

```
Stage 1/7  ●  Detecting GA release...
  ✓ Current provider version: 5.10.0
  ✓ Latest GA version: 5.38.0
  ✓ Upgrade required: yes

Stage 2/7  ●  Analyzing changes...
  ✓ LLM identified 7 relevant changes
  ✓ 2 breaking, 5 new features

Stage 3/7  ●  Creating branch...
  ✓ Created: terrascope/ga-upgrade-v5-38-0

Stage 4/7  ●  Implementing changes...
  ✓ Generated changes for main.tf
  ✓ Generated changes for variables.tf
  ✓ Generated provider version bump → versions.tf
  ✓ Updated CHANGELOG.md
  ✓ Committed: feat: upgrade google provider to v5.38.0

Stage 5/7  ●  Validating code...
  ✓ HCL syntax: PASSED (3 files)
  ⚠ Required attributes: 1 warning (auto-fixed)
  ✓ Naming conventions: PASSED
  ✓ Variable types: PASSED

Stage 6/7  ●  Checking provider compatibility...
  ✓ All 7 changes compatible with v5.38.0
  ✓ versions.tf constraint updated

Stage 7/7  ●  Creating PR...
  ✓ No existing PR found
  ✓ Pushed branch to origin
  ✓ PR #47 created: https://github.com/your-org/terraform-google-bigquery/pull/47

✅  Workflow complete
```

### 6.2 Via the API

All GA endpoints are at `http://localhost:8000/api/ga/`.

**Run the full workflow:**

```bash
curl -X POST http://localhost:8000/api/ga/workflow \
  -H "Content-Type: application/json" \
  -d '{
    "repo_name": "terraform-google-bigquery",
    "base_branch": "main",
    "dry_run": false,
    "auto_fix": true
  }'
```

**Response (WorkflowRun object):**

```json
{
  "run_id": "run_20240115_143022_bigquery",
  "repo_name": "terraform-google-bigquery",
  "gcp_product": "bigquery",
  "started_at": "2024-01-15T14:30:22Z",
  "completed_at": "2024-01-15T14:33:47Z",
  "stage": "done",
  "overall_success": true,
  "ga_release": {
    "provider": "hashicorp/google",
    "current_version": "5.10.0",
    "latest_ga_version": "5.38.0",
    "upgrade_required": true,
    "breaking_changes": 2,
    "new_features": 5,
    "changelog_url": "https://github.com/hashicorp/terraform-provider-google/blob/main/CHANGELOG.md",
    "fetched_at": "2024-01-15T14:30:24Z"
  },
  "change_set": {
    "repo_name": "terraform-google-bigquery",
    "gcp_product": "bigquery",
    "current_tag": "v2.3.0",
    "changes": [
      {
        "change_type": "new_argument",
        "resource_type": "google_bigquery_dataset",
        "attribute_name": "max_time_travel_hours",
        "description": "added new argument max_time_travel_hours for time travel window",
        "provider_version": "5.38.0",
        "breaking": false
      }
    ],
    "files_to_modify": ["main.tf", "variables.tf", "versions.tf"],
    "summary": "Google provider upgrade from v5.10.0 to v5.38.0 introduces 7 changes..."
  },
  "branch_result": {
    "repo_name": "terraform-google-bigquery",
    "branch_name": "terrascope/ga-upgrade-v5-38-0",
    "base_branch": "main",
    "created": true,
    "already_existed": false
  },
  "validation_result": {
    "overall_passed": true,
    "error_count": 0,
    "warning_count": 1,
    "reports": [...]
  },
  "pr_result": {
    "action": "created",
    "pr_number": 47,
    "pr_url": "https://github.com/your-org/terraform-google-bigquery/pull/47",
    "pr_title": "feat: upgrade google provider to v5.38.0 [GA]",
    "branch": "terrascope/ga-upgrade-v5-38-0",
    "target_branch": "main"
  },
  "logs": [
    {"timestamp": "2024-01-15T14:30:22Z", "stage": "detecting_ga", "level": "info", "message": "Detecting GA release for terraform-google-bigquery"},
    {"timestamp": "2024-01-15T14:30:24Z", "stage": "detecting_ga", "level": "info", "message": "Current provider version: 5.10.0"},
    ...
  ]
}
```

**Check the latest GA version only (no workflow, just detection):**

```bash
curl http://localhost:8000/api/ga/detect/terraform-google-bigquery
```

**Response:**

```json
{
  "repo_name": "terraform-google-bigquery",
  "current_version": "5.10.0",
  "latest_ga_version": "5.38.0",
  "upgrade_required": true,
  "breaking_changes": 2,
  "new_features": 5,
  "changelog_url": "..."
}
```

**Check workflow run status (poll during long runs):**

```bash
curl http://localhost:8000/api/ga/runs/run_20240115_143022_bigquery
```

**List all workflow runs:**

```bash
curl http://localhost:8000/api/ga/runs
```

**Dry-run (all stages except push + PR):**

```bash
curl -X POST http://localhost:8000/api/ga/workflow \
  -H "Content-Type: application/json" \
  -d '{
    "repo_name": "terraform-google-bigquery",
    "dry_run": true
  }'
```

### 6.3 Via the CLI

For scripting, cron jobs, or CI pipelines:

```bash
# Activate virtualenv first
source .venv/bin/activate          # Mac/Linux
# or: .venv\Scripts\activate       # Windows

# Run GA workflow for one repo
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery

# Run for all enabled repos
python -m backend.ga_workflow.ga_orchestrator --all

# Dry run (no push, no PR)
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --dry-run

# Override base branch
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --base-branch develop

# Just detect — print version comparison and exit
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --detect-only

# Auto-fix validation issues
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --auto-fix
```

**Expected CLI output:**

```
🚀 TerraScope GA Workflow
   Repo:    terraform-google-bigquery
   Product: bigquery
   Dry run: false
════════════════════════════════════════

[14:30:22] Stage 1/7 — Detecting GA release
  → Current version: 5.10.0
  → Latest GA:       5.38.0
  → Upgrade needed:  YES (28 minor versions behind)

[14:30:28] Stage 2/7 — Analyzing changes
  → Fetched changelog (4,821 chars)
  → LLM analysis: 7 changes identified
  → Breaking: 2  |  New features: 5

[14:30:41] Stage 3/7 — Creating branch
  → Branch: terrascope/ga-upgrade-v5-38-0
  → Based on: main
  → Status: CREATED

[14:30:41] Stage 4/7 — Implementing changes
  → main.tf: ✓ generated (312 → 338 lines)
  → variables.tf: ✓ generated (84 → 91 lines)
  → versions.tf: ✓ generated (deterministic bump)
  → CHANGELOG.md: ✓ updated
  → Commit: feat: upgrade google provider to v5.38.0

[14:31:18] Stage 5/7 — Validating code
  → HCL syntax:      ✅ PASSED
  → Required attrs:  ⚠  1 warning (auto-fixed)
  → Naming convs:    ✅ PASSED
  → Variable types:  ✅ PASSED

[14:31:22] Stage 6/7 — Provider compatibility
  → Checking 7 changes against schema v5.38.0
  → All compatible ✅

[14:31:24] Stage 7/7 — PR management
  → Checking for existing PR on terrascope/ga-upgrade-v5-38-0...
  → No existing PR found
  → Pushing branch to origin...
  → Creating PR...
  → PR #47: https://github.com/your-org/terraform-google-bigquery/pull/47

════════════════════════════════════════
✅ Workflow complete (3m 02s)
   PR: https://github.com/your-org/terraform-google-bigquery/pull/47
```

---

## 7. Stage-by-Stage Deep Dive

### Stage 1 — GA Release Detection

**File:** `backend/ga_workflow/ga_detector.py`  
**Entry point:** `detect_ga_release(repo_name, run, github_token)`

This stage answers two questions: "what version is the module currently on?" and "what is the latest stable GA version available?"

**Getting the current version** — reads `versions.tf` (or `main.tf` as fallback) via GitPython at the latest tag, parses with `python-hcl2`, and extracts the `google` provider version constraint. The constraint string `">= 5.10, < 6.0"` is parsed to extract `5.10.0` as the effective floor version.

```hcl
# versions.tf — what the detector reads
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.10, < 6.0"   # ← extracts "5.10.0"
    }
  }
}
```

**Getting the latest GA version** — calls the Terraform Registry REST API:

```
GET https://registry.terraform.io/v1/providers/hashicorp/google/versions
```

The response lists every published version. The detector filters to stable releases only — any version matching `^\d+\.\d+\.\d+$` exactly (no `-alpha`, `-beta`, `-rc` suffixes). The highest semver is selected.

**Fallback behavior** — if the Registry API is unreachable (e.g., offline/firewall), the detector logs a warning and continues with `latest_version = current_version` so the workflow can still complete partially.

### Stage 2 — Change Analysis

**File:** `backend/ga_workflow/ga_detector.py`  
**Entry point:** `analyze_changes_with_llm()` + `parse_changelog_to_changes()`

The changelog is fetched from GitHub raw content:

```
GET https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/CHANGELOG.md
```

The `_extract_changelog_range()` function splits the full changelog (often 50,000+ characters) into only the version range relevant to this upgrade, keeping the LLM context window manageable.

**LLM analysis prompt structure:**

```
You are a Terraform module expert. Analyze this Google provider changelog
and identify all changes relevant to this Terraform module.

MODULE SUMMARY: {json of variables, resources, provider requirements}

CHANGELOG EXCERPT: {relevant changelog section, max 4000 chars}

Return a JSON array of change objects with fields:
change_type, resource_type, attribute_name, description,
provider_version, breaking, migration_guide
```

The LLM returns structured JSON at `temperature=0.0` (deterministic). If JSON parsing fails or the LLM returns nothing, the detector falls back to a regex-based parser that catches common changelog patterns:

```python
# Regex patterns used in fallback
r"resource[/`]?(google[\w_]+)[`]?[:\s]+added.*?argument.*?[`]?([\w_]+)[`]?"
r"\*\*New Resource[:\*\*]+\s*[`]?(google[\w_]+)[`]?"
r"resource[/`]?(google[\w_]+)[`]?[:\s]+[`]?([\w_]+)[`]?\s+is\s+deprecated"
```

**Resource relevance filtering** — changes are filtered to only those affecting resource types actually present in the module. If the module doesn't use `google_bigquery_connection`, changes to that resource are ignored. This keeps the implementation focused and avoids touching files unnecessarily.

### Stage 3 — Branch Creation

**File:** `backend/ga_workflow/ga_implementer.py`  
**Entry point:** `create_ga_branch(repo_name, ga_version, base_branch, run)`

Branch naming convention: `terrascope/ga-upgrade-v{major}-{minor}-{patch}`

Example: provider `5.38.0` → branch `terrascope/ga-upgrade-v5-38-0`

The `terrascope/` prefix groups all automated branches in GitHub's branch list, making them easy to identify and filter.

**Branch existence logic:**

```python
# Check order:
1. Local branches (git_repo.branches)
2. Remote branches (origin refs)
3. Neither → create new from base_branch

# If already exists locally  → checkout and reuse
# If exists only on remote   → checkout -b tracking remote
# If neither exists          → checkout -b new branch from base
```

This means running the workflow twice for the same version never creates duplicate branches — it reuses the existing one and continues from where it left off. The PR manager also detects the existing PR in this case and updates it rather than creating a duplicate.

### Stage 4 — Code Implementation

**File:** `backend/ga_workflow/ga_implementer.py`  
**Entry points:** `generate_code_changes()` → `apply_code_changes()`

**Code generation** uses a file-by-file approach. For each `.tf` file in `change_set.files_to_modify`:

1. Read current content from Git at the latest tag.
2. Filter changes relevant to that file (see `_changes_for_file()`).
3. Send current content + change descriptions to Ollama.
4. LLM returns the complete updated file content (not a diff — the full file).
5. If content differs from original, a `CodeChange` object is created.

**The LLM prompt for code generation:**

```
You are a Terraform HCL expert implementing Google provider GA changes.

FILE: main.tf
CURRENT CONTENT:
```hcl
{current file content, max 6000 chars}
```

CHANGES TO IMPLEMENT:
- [new_argument] google_bigquery_dataset.max_time_travel_hours: added new argument
- [deprecated_argument] google_bigquery_dataset.time_partitioning.expiration_ms: deprecated

Instructions:
1. Generate the COMPLETE updated file content with all changes applied
2. For new arguments: add them with sensible defaults or as optional variables
3. For deprecated arguments: add comment "# Deprecated in provider vX.Y.Z"
4. Add a comment above each change: "# GA vX.Y.Z: <description>"
5. Preserve all existing code
6. Follow HCL2 syntax exactly

Return ONLY the complete updated file content as valid HCL.
```

**Deterministic version bump** — `_generate_version_bump()` uses pure regex to update the provider version constraint in `versions.tf`, bypassing the LLM entirely for this simple and critical change:

```python
# Before:
version = ">= 5.10, < 6.0"

# After (regex substitution):
version = ">= 5.38, < 6.0"
```

**CHANGELOG.md entry format:**

```markdown
## [Unreleased] — GA Provider Upgrade v5.38.0 (2024-01-15)

### Provider
- Upgraded `hashicorp/google` from `v5.10.0` to `v5.38.0`

### Added
- `google_bigquery_dataset`.`max_time_travel_hours`: added new argument for time travel window
- `google_bigquery_table`.`table_constraints`: added table constraints support

### Breaking Changes
- ⚠️ `google_bigquery_dataset`.`time_partitioning.expiration_ms`: field renamed to expiration_hours

### Files Modified
- `main.tf`: GA v5.38.0: added max_time_travel_hours; deprecated expiration_ms
- `variables.tf`: GA v5.38.0: added var.max_time_travel_hours
- `versions.tf`: Bump Google provider version constraint to >= 5.38, < 6.0

_Generated by TerraScope GA Workflow on 2024-01-15_
```

**Commit message convention:**

```
feat: upgrade google provider to v5.38.0

- Implements 7 GA changes for bigquery
- Provider: 5.10.0 → 5.38.0
- Breaking changes: 2
- New features: 5

Generated by TerraScope GA Workflow
```

### Stage 5 — Validation

**File:** `backend/ga_workflow/ga_validators.py`  
**Entry point:** `validate_all(repo_name, branch_name, changed_files, run)`

Four independent validators run in sequence. Each returns a `ValidatorReport`.

**Validator 1 — HCL Syntax** (`validate_hcl_syntax`)

Reads each modified `.tf` file from disk (the working tree, not Git) and parses with `python-hcl2`. A parse exception is an `ERROR`. This catches:
- Missing closing braces
- Invalid attribute syntax
- Unclosed string literals
- Invalid HCL type expressions

```python
# Example finding:
ValidationIssue(
    severity=ValidationSeverity.ERROR,
    file_path="main.tf",
    line=47,
    rule="hcl_syntax",
    message="Unexpected token at line 47: expected '=' got '{'",
    suggestion="Check for missing '=' in attribute assignment"
)
```

**Validator 2 — Required Attributes** (`validate_required_attributes`)

Cross-references `google_*` resources against a built-in schema of required arguments per resource type. Catches cases where the LLM added a resource block but omitted a required field.

```python
REQUIRED_ATTRS = {
    "google_bigquery_dataset": ["dataset_id", "project"],
    "google_storage_bucket":   ["name", "location"],
    "google_bigquery_table":   ["dataset_id", "table_id", "project"],
    "google_pubsub_topic":     ["name", "project"],
    # ... full GCP resource coverage
}
```

**Validator 3 — Naming Conventions** (`validate_naming_conventions`)

Enforces your curation team's standards. Configurable in `terrascope.config.yaml`:
- Variable names must be `snake_case`
- Resource names must not start with digits
- Output names must not have `-` (hyphen) characters
- Description fields must be non-empty on all variables

**Validator 4 — Variable Types** (`validate_variable_types`)

Checks that variable `type` fields use valid Terraform type expressions:
- Primitive: `string`, `number`, `bool`
- Complex: `list(string)`, `map(string)`, `set(string)`, `object({...})`
- Catches: misspelled types like `str` or `boolean`, empty type blocks

**Auto-fix** — when `auto_fix=True`, the workflow attempts to fix `WARNING`-level issues automatically:
- Adds missing `description = ""` to variables
- Converts hyphen-separated output names to underscore
- Removes trailing whitespace

`ERROR`-level issues are never auto-fixed — they require human review.

### Stage 6 — Provider Compatibility

**File:** `backend/ga_workflow/ga_compat.py`  
**Entry point:** `check_provider_compatibility(change_set, run)`

Verifies that every new attribute and resource type in the `GAChangeSet` actually exists in the target provider version by querying the Terraform Registry schema API:

```
GET https://registry.terraform.io/v1/providers/hashicorp/google/{version}/schema
```

For each `GAChange`:
- `new_argument` → confirms `attribute_name` exists in `resource_type` schema
- `new_resource` → confirms `resource_type` exists at all
- `deprecated_argument` → confirms `deprecated_in` version is ≤ target version

A `ProviderCompatCheck` is returned per change with `supported: true/false`.

**If incompatibility is found**, the workflow logs an error and marks the relevant `CodeChange` as needing manual review rather than failing the entire workflow. This lets the team review the specific incompatibility in context of the PR.

### Stage 7 — PR Management

**File:** `backend/ga_workflow/ga_pr_manager.py`  
**Entry point:** `create_or_update_pr(repo_cfg, branch_result, code_changes, run, ...)`

**Step 1 — Push the branch:**

```python
git_repo.remotes.origin.push(
    f"{branch_name}:{branch_name}",
    force_with_lease=True  # Safe force push (fails if remote has new commits)
)
```

`force_with_lease` is used instead of `--force` to protect against accidentally overwriting commits pushed by another team member on the same branch.

**Step 2 — Check for existing PR:**

```
GET https://api.github.com/repos/{owner}/{repo}/pulls
    ?state=open&head={owner}:{branch_name}
```

This returns all open PRs whose head branch matches `terrascope/ga-upgrade-vX-Y-Z`. If any are returned, the workflow goes into **update** mode instead of **create** mode.

**Step 3a — Create PR (no existing PR):**

```
POST https://api.github.com/repos/{owner}/{repo}/pulls
{
  "title": "feat: upgrade google provider to v5.38.0 [GA]",
  "body": "<full PR template — see Section 10>",
  "head": "terrascope/ga-upgrade-v5-38-0",
  "base": "main",
  "draft": false
}
```

After creating, the workflow also adds labels and requests reviewers:

```
POST https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels
POST https://api.github.com/repos/{owner}/{repo}/pulls/{number}/requested_reviewers
```

**Step 3b — Update PR (existing PR found):**

```
PATCH https://api.github.com/repos/{owner}/{repo}/pulls/{number}
{
  "body": "<updated body with new run's changes appended>"
}
```

The body update **appends** a new section rather than replacing — this preserves review comments and manual notes already in the PR body. The appended section is titled `### Update — {date} (TerraScope Re-run)` and lists what changed in the new run.

---

## 8. Code Module Reference

### 8.1 `ga_models.py`

All Pydantic data contracts. Nothing in the pipeline passes untyped dicts between stages — every inter-stage transfer goes through one of these models.

| Model | Stage | Purpose |
|-------|-------|---------|
| `GAChange` | 1→2 | One discrete provider changelog entry |
| `GARelease` | 1 | Version metadata: current, latest, upgrade_required |
| `GAChangeSet` | 1→2→3→4 | Full analysis: release + all changes + files to modify |
| `BranchResult` | 3 | Branch name, created/reused, error |
| `CodeChange` | 4 | One file edit: old content, new content, GA change back-ref |
| `CodeChangeSet` | 4→5 | All file edits + changelog entry + commit message |
| `ValidationIssue` | 5 | One finding: severity, file, rule, message, suggestion |
| `ValidatorReport` | 5 | One validator's output: passed, issues list |
| `ValidationResult` | 5→6 | All validators aggregated: overall pass/fail, counts |
| `ProviderCompatCheck` | 6 | One resource/attribute compatibility check |
| `ProviderCompatibility` | 6→7 | All checks: all_compatible, versions.tf update |
| `ExistingPR` | 7 | An already-open PR from GitHub API |
| `PRResult` | 7 | Outcome: action (created/updated/skipped), PR number, URL |
| `WorkflowRun` | All | Master state: all stage outputs + logs + stage enum |
| `WorkflowLog` | All | One log entry: timestamp, stage, level, message |
| `GAWorkflowRequest` | API | Request body: repo_name, dry_run, auto_fix, github_token |

**`WorkflowRun` state machine transitions:**

```
IDLE → DETECTING → ANALYZING → BRANCHING → IMPLEMENTING
     → VALIDATING → CHECKING_PR → CREATING_PR/UPDATING_PR → DONE
     
     At any stage: → FAILED (on unrecoverable error)
```

### 8.2 `ga_detector.py`

**Key functions:**

```python
async def fetch_latest_ga_version() -> Optional[str]
```
Queries `registry.terraform.io`. Returns `"5.38.0"` or `None` on network failure.

```python
async def fetch_provider_changelog(from_version: str, to_version: str) -> str
```
Downloads the raw CHANGELOG.md and extracts only the section between the two versions. Caps at 8,000 characters.

```python
def get_current_provider_version(repo_name: str, tag: str) -> Optional[str]
```
Reads `versions.tf` at a Git tag, returns the effective minimum version from the constraint.

```python
async def analyze_changes_with_llm(changelog_text, module_summary, run) -> list[GAChange]
```
Sends changelog + module context to Ollama. Parses JSON response into `GAChange` objects. Falls back to regex on failure.

```python
def parse_changelog_to_changes(changelog_text, resource_types_in_module, target_version) -> list[GAChange]
```
Pure regex fallback. Covers: new arguments, deprecations, new resources, IAM changes.

```python
async def detect_ga_release(repo_name, run, github_token) -> Optional[GAChangeSet]
```
Full Stage 1+2 pipeline. The only function called by the orchestrator.

### 8.3 `ga_implementer.py`

**Key functions:**

```python
def create_ga_branch(repo_name, ga_version, base_branch, run) -> BranchResult
```
GitPython branch creation with remote awareness and idempotency.

```python
async def generate_code_changes(change_set, run) -> list[CodeChange]
```
File-by-file LLM code generation. Returns `CodeChange` objects for each modified file.

```python
def apply_code_changes(change_set, code_changes, branch_result, run, dry_run) -> CodeChangeSet
```
Writes files to disk, updates CHANGELOG.md, commits. Skips all writes if `dry_run=True`.

```python
def _generate_version_bump(repo_name, tag, new_version, ga_change) -> Optional[CodeChange]
```
Deterministic (no LLM) regex-based `versions.tf` version constraint update.

---

## 9. Worked Examples

### Example A — BigQuery New Argument

**Scenario:** Provider `v5.38.0` adds `max_time_travel_hours` to `google_bigquery_dataset`.  
Your module is on provider `v5.10.0` and uses `google_bigquery_dataset`.

**What the detector finds:**

```json
{
  "change_type": "new_argument",
  "resource_type": "google_bigquery_dataset",
  "attribute_name": "max_time_travel_hours",
  "description": "added new argument max_time_travel_hours to configure the time travel window",
  "provider_version": "5.38.0",
  "breaking": false
}
```

**What the implementer generates for `main.tf`:**

```hcl
# BEFORE (current main.tf excerpt):
resource "google_bigquery_dataset" "dataset" {
  dataset_id                  = var.dataset_id
  location                    = var.location
  delete_contents_on_destroy  = var.delete_contents_on_destroy
}

# AFTER (generated by TerraScope):
resource "google_bigquery_dataset" "dataset" {
  dataset_id                  = var.dataset_id
  location                    = var.location
  delete_contents_on_destroy  = var.delete_contents_on_destroy

  # GA v5.38.0: added new argument for time travel window (0 = disable, default 168 = 7 days)
  max_time_travel_hours = var.max_time_travel_hours
}
```

**Generated addition to `variables.tf`:**

```hcl
# GA v5.38.0: new optional variable for dataset time travel configuration
variable "max_time_travel_hours" {
  description = "Number of hours for the time travel window on this dataset. Set to 0 to disable. Defaults to 168 (7 days)."
  type        = number
  default     = 168
}
```

### Example B — Provider Version Bump

**Scenario:** `versions.tf` currently has `">= 5.10, < 6.0"`, upgrading to `v5.38.0`.

**Before:**

```hcl
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.10, < 6.0"
    }
  }
  required_version = ">= 1.3"
}
```

**After (deterministic regex substitution — no LLM involved):**

```hcl
# Updated by TerraScope GA workflow — provider v5.38.0
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.38, < 6.0"
    }
  }
  required_version = ">= 1.3"
}
```

Note: the upper bound `< 6.0` is preserved. The lower bound is updated to `5.38` (the GA version's major.minor). This keeps the module compatible with any future `5.x` patch release.

### Example C — Breaking Change with Migration

**Scenario:** Provider `v5.25.0` renames `time_partitioning.expiration_ms` to `time_partitioning.expiration_hours` in `google_bigquery_table`.

**Detected change:**

```json
{
  "change_type": "deprecated_argument",
  "resource_type": "google_bigquery_table",
  "attribute_name": "time_partitioning.expiration_ms",
  "description": "expiration_ms renamed to expiration_hours; value in hours not milliseconds",
  "provider_version": "5.25.0",
  "breaking": true,
  "migration_guide": "Replace: expiration_ms = 86400000\nWith:    expiration_hours = 24"
}
```

**Generated code change:**

```hcl
# BEFORE:
resource "google_bigquery_table" "table" {
  dataset_id = var.dataset_id
  table_id   = var.table_id

  time_partitioning {
    type          = "DAY"
    expiration_ms = var.partition_expiration_ms   # 86400000 = 24 hours
  }
}

# AFTER:
resource "google_bigquery_table" "table" {
  dataset_id = var.dataset_id
  table_id   = var.table_id

  time_partitioning {
    type             = "DAY"
    # Deprecated in provider v5.25.0 — remove in next major module version
    # expiration_ms = var.partition_expiration_ms
    # GA v5.25.0: renamed to expiration_hours (value in hours, not milliseconds)
    expiration_hours = var.partition_expiration_hours
  }
}
```

The old field is **commented out** (not deleted) to preserve the value for teams that haven't yet migrated state. A new variable `partition_expiration_hours` is added to `variables.tf`.

**PR breaking change warning section:**

```markdown
## ⚠️ Breaking Changes — Manual Review Required

### `google_bigquery_table.time_partitioning.expiration_ms` → `expiration_hours`
**Provider version:** 5.25.0  
**Impact:** All deployments using `time_partitioning.expiration_ms` must be updated.

**Migration:**
```hcl
# OLD (remove this):
expiration_ms = 86400000   # 24 hours in milliseconds

# NEW (use this):
expiration_hours = 24
```

**State migration:** Run `terraform state rm` and re-import if needed.
```

### Example D — Existing PR Update

**Scenario:** GA workflow ran last week and created PR #47. Since then, another GA version was released. The workflow runs again and finds PR #47 already open.

**Existing PR #47 body (truncated):**

```markdown
## feat: upgrade google provider to v5.38.0 [GA]

| Field | Value |
|-------|-------|
| Current version | 5.10.0 |
| Target version  | 5.38.0 |
| Changes         | 7 (2 breaking, 5 new) |
| Branch          | terrascope/ga-upgrade-v5-38-0 |

...original content...
```

**After re-run (PR #47 body updated):**

```markdown
## feat: upgrade google provider to v5.38.0 [GA]

...original content preserved...

---

### Update — 2024-01-22 (TerraScope Re-run)

**New GA version detected:** v5.40.0  
**Additional changes since last run:** 3

| Change | Resource | Breaking |
|--------|----------|---------|
| new_argument | google_bigquery_dataset.external_data_configuration.parquet_options | No |
| new_argument | google_bigquery_dataset.table_replication_info | No |
| deprecated_argument | google_bigquery_routine.determinism_level | Yes ⚠️ |

**Files updated in this run:** main.tf, variables.tf, versions.tf  
**New commit:** `abc1234` — feat: incorporate v5.40.0 changes  

_Updated by TerraScope GA Workflow on 2024-01-22_
```

---

## 10. PR Structure and Templates

Every PR created by TerraScope follows this structure:

```markdown
## feat: upgrade google provider to v{version} [GA]

> 🤖 This PR was automatically generated by **TerraScope GA Workflow**.
> Review all changes carefully before merging, especially breaking changes.

---

## Summary

| Field | Value |
|-------|-------|
| **Module** | terraform-google-bigquery |
| **GCP Product** | BigQuery |
| **Current provider version** | 5.10.0 |
| **Target provider version** | 5.38.0 |
| **Total changes** | 7 |
| **Breaking changes** | 2 |
| **New features** | 5 |
| **Branch** | `terrascope/ga-upgrade-v5-38-0` |
| **Workflow run** | `run_20240115_143022_bigquery` |

---

## Changes Implemented

### New Features

| Resource | Attribute | Description |
|----------|-----------|-------------|
| `google_bigquery_dataset` | `max_time_travel_hours` | Configure time travel window |
| `google_bigquery_table` | `table_constraints` | Primary/foreign key constraints |

### ⚠️ Breaking Changes

| Resource | Attribute | Migration Required |
|----------|-----------|-------------------|
| `google_bigquery_dataset` | `time_partitioning.expiration_ms` | Renamed to `expiration_hours` |

---

## Files Modified

| File | Changes |
|------|---------|
| `main.tf` | Added max_time_travel_hours; deprecated expiration_ms |
| `variables.tf` | Added var.max_time_travel_hours; updated var.partition_expiration |
| `versions.tf` | Bumped provider constraint to >= 5.38, < 6.0 |
| `CHANGELOG.md` | Prepended GA upgrade entry |

---

## Validation Results

| Validator | Status | Issues |
|-----------|--------|--------|
| HCL Syntax | ✅ PASSED | 0 errors |
| Required Attributes | ✅ PASSED | 0 errors, 1 warning (auto-fixed) |
| Naming Conventions | ✅ PASSED | 0 errors |
| Variable Types | ✅ PASSED | 0 errors |

---

## Provider Compatibility

All 7 changes verified against provider schema v5.38.0. ✅

---

## Reviewer Checklist

- [ ] Provider version constraint range is correct
- [ ] Breaking changes have migration notes for downstream consumers
- [ ] New variables have descriptions and appropriate defaults
- [ ] CHANGELOG.md entry is accurate
- [ ] No existing tests are broken by the variable renames

---

## Changelog Entry

```markdown
## [Unreleased] — GA Provider Upgrade v5.38.0 (2024-01-15)
...
```

---

_Generated by [TerraScope](https://github.com/your-org/terrascope) on 2024-01-15 14:33:47 UTC_
```

---

## 11. Validation Rules Reference

| Rule Name | Validator | Severity | Description | Auto-fixable |
|-----------|-----------|----------|-------------|-------------|
| `hcl_syntax` | HCL Syntax | ERROR | File fails python-hcl2 parse | No |
| `hcl_empty_file` | HCL Syntax | WARNING | .tf file is empty after changes | No |
| `required_attr_missing` | Required Attrs | ERROR | `google_*` resource missing required field | No |
| `required_attr_variable` | Required Attrs | WARNING | Required field set to hardcoded value (not a var) | No |
| `naming_snake_case` | Naming | WARNING | Variable/output name not snake_case | Yes |
| `naming_no_hyphen` | Naming | ERROR | Resource name contains hyphen | No |
| `naming_empty_description` | Naming | WARNING | Variable missing `description` field | Yes (`description = ""`) |
| `naming_digit_start` | Naming | ERROR | Resource logical name starts with digit | No |
| `type_invalid` | Variable Types | ERROR | `type` field is not a valid Terraform type | No |
| `type_missing` | Variable Types | INFO | Variable has no `type` (implicit `any`) | No |
| `type_complex_untyped` | Variable Types | WARNING | `object({})` type with no attribute types | No |

---

## 12. Provider Compatibility Checks

The compatibility checker queries the Terraform Registry schema API:

```
GET https://registry.terraform.io/v1/providers/hashicorp/google/{version}/schema
```

This returns the full JSON schema for all resources at the target provider version — thousands of resources and attributes. The checker scans this schema for each proposed change:

```python
# Compatibility check logic:

# new_argument check:
schema["resource_schemas"][resource_type]["block"]["attributes"][attribute_name]
# → exists? supported=True : supported=False

# new_resource check:
schema["resource_schemas"][resource_type]
# → exists? supported=True : supported=False

# deprecated_argument check:
schema["resource_schemas"][resource_type]["block"]["attributes"][attribute_name]["deprecated"]
# → deprecated=True and deprecated_in <= target_version?
```

**If a check fails** (attribute not in schema at target version):

```
[Stage 6] ⚠ Incompatibility: google_bigquery_dataset.max_time_travel_hours
          not found in provider schema v5.38.0.
          Marking code change as requires_manual_review=True.
          The PR will include a warning note for this attribute.
```

The workflow continues — it doesn't abort on compatibility warnings. The PR body will include a dedicated incompatibility section for the team to investigate.

---

## 13. API Reference — GA Endpoints

All endpoints at `http://localhost:8000/api/ga/`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/ga/workflow` | Run the full 7-stage pipeline |
| `GET` | `/api/ga/detect/{repo_name}` | Detect GA version only (no branch/PR) |
| `GET` | `/api/ga/runs` | List all workflow run records |
| `GET` | `/api/ga/runs/{run_id}` | Get a specific run's full state |
| `DELETE` | `/api/ga/runs/{run_id}` | Delete a run record |
| `GET` | `/api/ga/changelog/{repo_name}` | Fetch raw provider changelog (cached 1hr) |
| `POST` | `/api/ga/validate/{repo_name}` | Run validators only on current branch |
| `GET` | `/api/ga/compat/{repo_name}` | Check provider compatibility for current changes |

**`POST /api/ga/workflow` request body:**

```json
{
  "repo_name": "terraform-google-bigquery",   // required
  "base_branch": "main",                       // optional, default: "main"
  "github_token": null,                        // optional, default: $GITHUB_TOKEN env var
  "pr_labels": ["ga-release", "automated"],   // optional
  "dry_run": false,                            // optional, default: false
  "auto_fix": true                             // optional, default: true
}
```

**`GET /api/ga/detect/{repo_name}` response:**

```json
{
  "repo_name": "terraform-google-bigquery",
  "current_version": "5.10.0",
  "latest_ga_version": "5.38.0",
  "upgrade_required": true,
  "breaking_changes": 2,
  "new_features": 5,
  "changelog_url": "https://github.com/hashicorp/terraform-provider-google/blob/main/CHANGELOG.md",
  "fetched_at": "2024-01-15T14:30:22Z"
}
```

---

## 14. Configuration Reference

Full `terrascope.config.yaml` with all GA workflow options:

```yaml
terrascope:
  llm:
    provider: ollama
    base_url: http://localhost:11434
    model: gemma3:4b
    embedding_model: nomic-embed-text
    temperature: 0.0
    max_tokens: 2048
    context_window: 8192

  grounding:
    mode: strict
    require_source_citation: true
    min_confidence_threshold: 0.65
    max_retrieval_chunks: 8
    chunk_overlap_tokens: 50

  vector_store:
    type: chromadb
    persist_path: ./data/chromadb

  server:
    host: 127.0.0.1
    port: 8000
    reload: true

  ui:
    port: 5173
    theme: dark

  # ── GA Workflow Settings ─────────────────────────────────────
  ga_workflow:
    # Terraform Registry API (no auth needed)
    registry_api: https://registry.terraform.io/v1/providers/hashicorp/google
    # GitHub API (uses GITHUB_TOKEN env var)
    github_api: https://api.github.com
    # HTTP timeout for external API calls (seconds)
    http_timeout_seconds: 15
    # Maximum changelog size to send to LLM (characters)
    max_changelog_chars: 4000
    # Branch name prefix for GA branches
    branch_prefix: terrascope/ga-upgrade-v
    # Whether to use force_with_lease on push
    safe_push: true
    # Cache provider schema responses (avoids repeated Registry calls)
    cache_schema: true
    cache_ttl_minutes: 60

repos:
  - name: terraform-google-bigquery
    display_name: BigQuery
    local_path: ./repos/terraform-google-bigquery
    gcp_product: bigquery
    description: "Terraform modules for Google BigQuery datasets, tables, IAM"
    enabled: true
    # ── Per-repo GitHub settings ───────────────────────────────
    github_owner: your-org               # GitHub org or user
    github_repo: terraform-google-bigquery  # GitHub repo name
    default_base_branch: main            # Override global default_base_branch
    pr_labels:                           # Override global pr_labels
      - ga-release
      - bigquery
      - automated
    pr_reviewers:                        # Auto-request review from these users
      - alice
      - bob
    pr_assignees: []                     # Auto-assign PR to these users

  - name: terraform-google-gcs
    display_name: Cloud Storage
    local_path: ./repos/terraform-google-gcs
    gcp_product: storage
    description: "Terraform modules for Google Cloud Storage buckets"
    enabled: true
    github_owner: your-org
    github_repo: terraform-google-gcs
```

---

## 15. Troubleshooting

### "No GitHub token — cannot create PR"

```
[Stage 7] Error: No GitHub token available.
          Set GITHUB_TOKEN environment variable or pass github_token in request.
```

**Fix:**
```bash
# Mac/Linux
export GITHUB_TOKEN=ghp_your_token_here

# Windows PowerShell
$env:GITHUB_TOKEN = "ghp_your_token_here"
```
Then restart the backend so it picks up the new env var.

### "404 when pushing branch"

```
[Stage 7] Push error: remote: Repository not found.
```

Your Git remote `origin` in the local repo doesn't match the `github_owner`/`github_repo` in config, or the token doesn't have `repo` scope.

**Check:**
```bash
cd repos/terraform-google-bigquery
git remote -v
# Should show: origin https://github.com/your-org/terraform-google-bigquery.git
```

If the remote is SSH and your environment doesn't have SSH keys configured, switch to HTTPS:
```bash
git remote set-url origin https://github.com/your-org/terraform-google-bigquery.git
```

### "Terraform Registry API unreachable"

```
[Stage 1] Registry API error: httpx.ConnectError
          Could not determine latest GA version — defaulting to current version.
```

The workflow continues in offline mode — it will use the current version as the latest, skip changelog analysis, and proceed without changes (the PR will note it was run in offline mode). This is expected in air-gapped environments.

To test offline mode explicitly:
```bash
curl -X POST http://localhost:8000/api/ga/workflow \
  -d '{"repo_name": "terraform-google-bigquery", "dry_run": true}'
```

### "Branch already exists with different base"

```
[Stage 3] Warning: Branch terrascope/ga-upgrade-v5-38-0 already exists.
          It was created from 'develop', but base_branch='main' was requested.
          Reusing existing branch.
```

The workflow always reuses an existing branch rather than failing. If you need a fresh branch from a different base, delete the existing branch first:

```bash
cd repos/terraform-google-bigquery
git branch -D terrascope/ga-upgrade-v5-38-0
git push origin --delete terrascope/ga-upgrade-v5-38-0
```

### "HCL syntax validation failed after code generation"

```
[Stage 5] ERROR: main.tf — hcl_syntax
          Unexpected token at line 87: expected '=' got '{'
```

The LLM generated invalid HCL. The workflow doesn't abort — it marks the issue in the PR body and continues. The team must fix the syntax manually on the branch before merging.

To inspect the generated file:
```bash
cd repos/terraform-google-bigquery
git checkout terrascope/ga-upgrade-v5-38-0
cat main.tf | grep -n "" | head -100
```

### "PR creation failed — 422 Unprocessable Entity"

Usually means a PR already exists for the branch but the API returned it as closed. The workflow's check only looks for `state=open` PRs. Check GitHub for closed/merged PRs on the same branch name.

**Fix:** Either reopen the existing PR manually, or delete the branch and re-run the workflow to get a fresh branch and PR.

### Workflow is very slow (>10 minutes)

The bottleneck is almost always the LLM code generation step (Stage 4), which calls Ollama once per modified `.tf` file. Factors that improve speed:

- **GPU acceleration** — if Ollama is using CPU only, enable GPU (CUDA on Windows/Linux, Metal on Mac M-series). Check with `ollama ps`.
- **Smaller model** — switch to `gemma3:2b` for faster generation at slightly lower quality: `terrascope.config.yaml → llm.model: gemma3:2b`
- **Fewer files** — the workflow only processes files listed in `change_set.files_to_modify`. If many files are being processed, the changes may be too broad. Use `--detect-only` to review the change set first.

---

## 16. FAQ

**Q: Does the workflow modify my `main` branch directly?**  
No. The workflow always creates or reuses a `terrascope/ga-upgrade-vX-Y-Z` branch. The `main` branch is only read (for checkout as the branch base) and never written to.

**Q: What happens if Ollama is offline when the workflow runs?**  
The workflow will fail at Stage 2 (LLM analysis) and fall back to the regex-based parser. If the regex parser also returns no changes, the workflow continues with zero changes — it will create a branch and a PR that notes "no changes identified, manual review recommended." The PR body will include the raw changelog section for the team to review manually.

**Q: Can I run the workflow for multiple repos simultaneously?**  
Yes, via the API. Each workflow run is independent and tracked by its own `run_id`. The UI runs them serially by default (one at a time), but the API supports concurrent requests. Note that concurrent git operations on the same repo can cause conflicts — the workflow uses `force_with_lease` to detect this.

**Q: What if the module repo doesn't have a `versions.tf`?**  
The current provider version detector falls back to scanning `main.tf` for provider constraints. If no constraint is found anywhere, it defaults to `"4.0.0"` and logs a warning. The generated `versions.tf` will be created as a new file in this case.

**Q: Can I customize the PR title format?**  
Yes. The PR title template is configurable in `terrascope.config.yaml` under `ga_workflow.pr_title_template`. The default is `"feat: upgrade google provider to v{version} [GA]"`. Available placeholders: `{version}`, `{product}`, `{repo_name}`, `{date}`.

**Q: Does the workflow run `terraform init` or `terraform validate`?**  
No — this is by design. Running `terraform init` requires downloading provider plugins (hundreds of MB) and network access. TerraScope uses `python-hcl2` for HCL syntax checking and the Registry schema API for provider compatibility — both work without Terraform CLI installed.

**Q: How does the PR update avoid overwriting human edits?**  
When updating an existing PR body, the workflow appends a new section (`### Update — {date}`) rather than replacing the entire body. All original content (including inline reviewer comments added via the GitHub UI to the PR description) is preserved. Only the PR body is updated — comments on individual lines of code (review comments) are never affected.

**Q: Can I disable the GA workflow and use only the base Q&A agent?**  
Yes. The GA workflow routes are separate from the core query routes. If `github_owner` and `github_repo` are not set in the repo config, the workflow's PR stage will skip PR creation and complete as a dry run. You can also simply not use the GA tab in the UI.
