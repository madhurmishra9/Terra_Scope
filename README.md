# 🔭 TerraScope

> **AI-powered Terraform module curation for GCP, AWS, and Azure.**  
> Query any module version in natural language, generate new modules from scratch, curate existing ones, and automate GA upgrades — all running 100% locally with Ollama.  
> **v2.5** — Google-doc-enriched documentation generator, single-command startup, and in-UI settings editor.

---

## What's New in v2.5

| Feature | Description |
|---------|-------------|
| **🌐 Ask AI (General Chat)** | New default landing tab — ask anything; automatically searches ALL indexed repos, falls back to general LLM knowledge with clear grounded/mixed/general labelling, keeps conversation history |
| **📄 Enhanced Doc-Gen** | Two-phase preview → publish flow with Google official docs enrichment; works for any product even without curation |
| **🔍 Google Docs Fetch** | Pulls product overview, key features, security notes and official URLs from `cloud.google.com` + the Terraform registry; synthesises with local LLM (temperature 0.0) |
| **📋 Rich Default Template** | Full document generated even without a `.docx` template: overview → features → APIs → IAM roles → resources → inputs → outputs → versions → security → official links |
| **🌐 Confluence URLs** | Publish step returns clickable page URLs per doc type |
| **⚡ Single-command startup** | `npm run dev` in `frontend/` starts both the React UI **and** the FastAPI backend automatically via a Vite plugin; set `TERRASCOPE_NO_BACKEND=1` to skip |
| **⚙ In-UI Settings editor** | New **Settings tab** — edit LLM model/URL, Confluence credentials (saved to `.env`), grounding thresholds, and server config directly from the browser; test connections with one click |
| **🔗 Cross-panel settings link** | Any panel that depends on a missing config shows a direct "Configure in Settings" link |
| **🐛 Table rendering fix** | Input/output tables now populate type and description columns (pydantic model fix + real `<table>` HTML instead of pipe-separated text) |

---

## What's New in v2.4

| Feature | Description |
|---------|-------------|
| **📄 Confluence Docgen** | After curation, automatically publish HLD, CPSD, Architectural Design, and Highly Confidential Assessment pages to Confluence — cloned from a reference `.docx` template, fields bound from real module metadata, idempotent re-runs update in place |
| **🔑 `.env` for secrets** | Confluence credentials live in `.env` (separate from `terrascope.config.yaml`); Basic or Bearer auth auto-detected |
| **🖼️ Diagram resolution** | Auto-generates architecture diagrams via `terraform graph | dot`; falls back to folder-supplied images or labelled SVG placeholders |
| **🏷️ Idempotency** | `data/docgen_manifest.json` tracks page IDs per product — re-running updates existing pages instead of creating duplicates; pages are also labelled `terrascope:<product>` |
| **🧪 Dry-run mode** | `--dry-run` writes the would-be Confluence XHTML to `output/docgen_dry_run/` for review without any API calls |
| **🖥️ CLI** | `python -m backend.module_curator.docgen.pipeline --product bigquery --module-path output/... --dry-run` |

---

## What's New in v2.3

| Feature | Description |
|---------|-------------|
| **🌐 Multi-cloud GA Workflow** | Auto-detects GCP, AWS, or Azure provider from `versions.tf` — no config changes needed |
| **📰 Cloud release note feeds** | Reads AWS What's New RSS, Azure Updates RSS, and GCP release notes per-service |
| **🔁 Incremental updates** | Persists applied changes in `./data/ga_state/{repo}.json`; re-runs skip already-applied changes |
| **⚠️ Breaking change classification** | Detects and labels WHY a change is breaking: `removed`, `renamed`, `type_changed`, `required_now`, `behavior_changed`, `deprecated_removed` |
| **📝 Migration notes** | Each breaking change includes a plain-English migration guide in the PR |
| **🏷️ Provider badge** | GA Workflow tab shows GCP / AWS / Azure provider chip next to version numbers |
| **🔍 Module Troubleshooter** | New tab — detect logical and syntactic bugs in any module at any Git tag, with safe upgrade path |

---

## What's New in v2.0

| Feature | Description |
|---------|-------------|
| **🔧 Module Curation** | Generate complete Terraform modules via LLM Q&A — from a service name, a document, or an existing module |
| **📄 Document-based generation** | Upload a PDF, Word (.docx), or text spec and generate a module from it |
| **🐙 GitHub / local / ZIP source** | Load any existing Terraform module from GitHub URL, local path, ZIP archive, or direct `.tf` upload |
| **🏷️ Self-curation with new tag** | Modify an indexed repo with LLM assistance and create a new Git tag automatically |
| **🌐 Multi-cloud** | GCP (`google`), AWS (`aws`), Azure (`azurerm`) — full provider support |
| **📚 Registry doc fetching** | Scrapes and caches Terraform provider docs locally; smart offline/online detection |
| **🔗 Cross-module references** | Resolves `module {}` sources from ChromaDB + Terraform Registry when generating code |
| **⚡ 3-pass LLM generation** | Each file group gets a dedicated LLM call with a full token budget: Pass A → `main.tf`, Pass B → `variables.tf` + `outputs.tf`, Pass C → `versions.tf` + `README.md` + examples |
| **📁 7-file complete module** | Every curation produces: `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, `README.md`, `examples/complete/main.tf`, `terraform.tfvars.example` |
| **🎯 Provider-specific Q&A** | 7 targeted questions per provider (GCP / AWS / Azure) covering workload, networking, IAM, encryption, HA, tagging, and cross-service integrations |

---

## 🚀 GA Release Workflow (v2.3 — Multi-Cloud)

TerraScope includes a full **GA Release Workflow** that automates upgrading your Terraform modules to the latest provider GA release. In v2.3 it supports **GCP, AWS, and Azure** — the provider is auto-detected from each repo's `versions.tf`, with no configuration changes required.

| Capability | Description |
|-----------|-------------|
| **Multi-cloud provider detection** | Auto-detects `hashicorp/google`, `hashicorp/aws`, or `hashicorp/azurerm` from `versions.tf` |
| **Cloud release note feeds** | GCP: Google release notes · AWS: What's New RSS · Azure: Updates RSS |
| **Incremental updates** | Skips changes already applied in a previous run (state in `./data/ga_state/`) |
| **Breaking change classification** | Flags whether each change is breaking and why: `removed`, `renamed`, `type_changed`, `required_now`, `behavior_changed`, `deprecated_removed` |
| **Migration notes** | Each breaking change includes a plain-English migration note in the PR description |
| **Branch creation** | Creates `terrascope/ga-upgrade-vX.Y.Z` automatically |
| **HCL code generation** | LLM generates updated `.tf` files for all new changes |
| **4-layer validation** | HCL syntax · required attributes · naming conventions · type checking |
| **Provider compat check** | Verifies every new attribute exists in the target provider schema |
| **PR create / update** | Opens a GitHub PR with full change summary and breaking-change annotations |

```bash
# Detect latest GA version (no changes made)
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --detect-only
python -m backend.ga_workflow.ga_orchestrator --repo terraform-aws-s3 --detect-only

# Run the full pipeline (provider auto-detected from versions.tf)
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery
python -m backend.ga_workflow.ga_orchestrator --repo terraform-aws-s3
python -m backend.ga_workflow.ga_orchestrator --repo terraform-azurerm-aks
```

### Breaking Change Classification

Every change detected by the GA workflow is classified as safe or breaking. If breaking, the reason is one of:

| Reason | Meaning |
|--------|---------|
| `removed` | A resource, block, or attribute was removed from the provider |
| `renamed` | A resource or attribute was renamed (requires `moved {}` block or variable rename) |
| `type_changed` | An attribute's type changed (e.g. `string` → `list`, `bool` → `object`) |
| `required_now` | A previously optional argument is now required |
| `behavior_changed` | Default value or runtime behavior changed without a signature change |
| `deprecated_removed` | A previously deprecated attribute has been removed |

Each breaking change also includes a `migration_note` — a plain-English instruction for the module consumer.

### Incremental State

The workflow persists applied changes to `./data/ga_state/{repo_name}.json`. On the next run:
- Changes already applied (by SHA-256 hash of their key) are skipped.
- Only genuinely new changes trigger code generation and a PR.
- State is written only after a successful PR creation — aborted runs don't pollute state.

📖 **Full GA docs:** [GA_WORKFLOW_README.md](backend/ga_workflow/GA_WORKFLOW_README.md)

---

## Table of Contents

1. [What TerraScope Does](#1-what-terrascope-does)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
   - [Windows Setup](#41-windows-setup)
   - [Mac Setup](#42-mac-setup)
5. [Configuration](#5-configuration)
   - [terrascope.config.yaml](#51-terrascopeconfigyaml)
   - [.env — Confluence Docgen](#52-env--confluence-docgen)
6. [Project Structure](#6-project-structure)
7. [Indexing Your Repos](#7-indexing-your-repos)
8. [Running TerraScope](#8-running-terrascope)
9. [Using the UI](#9-using-the-ui)
   - [Chat — Query Existing Modules](#91-chat--query-existing-modules)
   - [Curate — Generate New Modules](#92-curate--generate-new-modules)
   - [GA Workflow](#93-ga-workflow)
   - [Troubleshoot](#94-troubleshoot)
10. [Module Curation — Detailed Guide](#10-module-curation--detailed-guide)
    - [Mode 1: New Product](#101-mode-1-new-product)
    - [Mode 2: From Document](#102-mode-2-from-document)
    - [Mode 3: From Module](#103-mode-3-from-module)
    - [Mode 4: Self-Curation](#104-mode-4-self-curation)
11. [Confluence Documentation Generator](#11-confluence-documentation-generator)
    - [How It Works](#111-how-it-works)
    - [Template Setup](#112-template-setup)
    - [Running Docgen](#113-running-docgen)
    - [Dry-Run Mode](#114-dry-run-mode)
12. [Registry Doc Fetching](#12-registry-doc-fetching)
13. [API Reference](#13-api-reference)
14. [Code Deep Dive](#14-code-deep-dive)
15. [Anti-Hallucination Design](#15-anti-hallucination-design)
16. [Supported Products](#16-supported-products)
17. [Troubleshooting](#17-troubleshooting)
18. [FAQ](#18-faq)

---

## 1. What TerraScope Does

TerraScope is a local AI tool for Terraform module curation teams. It covers two distinct workflows:

### Query (Chat View)
- Reads your locally cloned Git repos — nothing leaves your machine.
- Indexes every Git tag/release — each version is independently searchable.
- Parses `.tf` files with a real AST parser (not regex).
- Answers natural-language questions: variables, resources, IAM, diffs, issues.
- Matches errors against a GCP-specific knowledge base.

### Generate (Curate View — New in v2.0)
- Generates complete Terraform modules from a service name, uploaded document, or existing module.
- Asks LLM-driven clarifying questions before generating.
- Fetches provider documentation from the Terraform Registry (cached locally for offline use).
- Resolves cross-module references from ChromaDB and the Registry.
- Writes output to `./output/{service}_{timestamp}/` and displays it in-browser for copy-paste.
- Supports GCP, AWS, and Azure.

### Troubleshoot (New in v2.3)
- Analyses any module at any Git tag for logical and syntactic bugs — no `terraform init` or plan needed.
- Three-stage pipeline: static analysis → LLM review → version recommendation.
- Detects: undefined variable references, security misconfigurations, deprecated resources, type mismatches, missing provider constraints, anti-patterns.
- Recommends the minimum provider version that fixes detected issues without introducing breaking changes for the module's resource types.
- Works for GCP, AWS, and Azure modules.

### Example Chat Questions

| Question | Type |
|----------|------|
| `What GCP resources does this module create at v2.1?` | Resource |
| `What variables are required in v1.3.0?` | Variable |
| `What changed between v1.5.0 and v2.0.0?` | Comparison |
| `Why does terraform apply fail with Error 403 on BigQuery?` | Issue |
| `Does this module support CMEK encryption?` | General |
| `Show me all IAM bindings in v2.0` | Security |

### Example Curation Prompts

| Goal | Mode |
|------|------|
| Create a Cloud Run module from scratch | New Product → GCP → "Cloud Run" |
| Turn a Word spec into a Lambda module | From Document → AWS → upload `.docx` |
| Modernise an existing GCS module | From Module → GitHub URL |
| Add a new feature and tag as v2.1.0 | Self-Curation → select repo → "v2.1.0" |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Your Machine                                    │
│                                                                              │
│  ┌─────────────────┐    HTTP     ┌────────────────────────────────────────┐ │
│  │  React UI        │ ◄────────► │  FastAPI Backend  :8000                │ │
│  │  :5173           │            │                                        │ │
│  │                  │            │  ┌──────────────────────────────────┐  │ │
│  │  Views:          │            │  │   PydanticAI Query Agent         │  │ │
│  │  • 💬 Chat       │            │  │   git_tools · hcl_tools          │  │ │
│  │  • 🔧 Curate     │            │  │   search_tools · issue_tools     │  │ │
│  │  • 🚀 GA Workflow│            │  └────────────────┬─────────────────┘  │ │
│  │  • 🧪 Scenarios  │            │                   │                    │ │
│  │  • 🔍 Troubleshoot            │  ┌────────────────▼─────────────────┐  │ │
│  └─────────────────┘            │  │  Troubleshooter Pipeline          │  │ │
│                                 │  │  static_analysis · llm_review     │  │ │
│                                 │  │  version_recommendation           │  │ │
│  └─────────────────┘            │                   │                    │ │
│                                 │  ┌────────────────▼─────────────────┐  │ │
│                                 │  │  Module Curation Pipeline         │  │ │
│                                 │  │  curator → question_engine        │  │ │
│                                 │  │  code_generator → module_fetcher  │  │ │
│                                 │  └────────────────┬─────────────────┘  │ │
│                                 │                   │                    │ │
│                                 │  ┌────────────────▼─────────────────┐  │ │
│                                 │  │  Ollama   :11434                  │  │ │
│                                 │  │  LLM: gemma4:12b                   │  │ │
│                                 │  │  Embeddings: nomic-embed-text     │  │ │
│                                 │  └──────────────────────────────────┘  │ │
│                                 │                                        │ │
│                                 │  ┌──────────────────────────────────┐  │ │
│                                 │  │  ChromaDB (local)                 │  │ │
│                                 │  │  ./data/chromadb/                 │  │ │
│                                 │  └──────────────────────────────────┘  │ │
│                                 │                                        │ │
│                                 │  ┌──────────────────────────────────┐  │ │
│                                 │  │  Registry Doc Cache               │  │ │
│                                 │  │  ./data/registry_cache/           │  │ │
│                                 │  │  google/ · aws/ · azurerm/        │  │ │
│                                 │  └──────────────────────────────────┘  │ │
│                                 └────────────────────────────────────────┘ │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Your Cloned Repos (read-only for queries; writable for self-curation)│  │
│  │  ./repos/terraform-google-bigquery    ./repos/terraform-google-gcs   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Generated Output (new in v2.0)                                       │  │
│  │  ./output/cloud_run_20250509_143022/main.tf                           │  │
│  │  ./output/cloud_run_20250509_143022/variables.tf   ...               │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | 3.12 recommended |
| Node.js | 18+ | For the React UI |
| Git | Any | Must be in PATH |
| Ollama | Latest | [ollama.com](https://ollama.com) |
| Free disk | ~5 GB | Models + ChromaDB index |
| RAM | 8 GB min | 16 GB recommended |

### Windows
- Git for Windows from [git-scm.com](https://git-scm.com/download/win)
- Python from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"**
- Windows Terminal (recommended)

### Mac
- Homebrew: [brew.sh](https://brew.sh)
- Xcode Command Line Tools: `xcode-select --install`

---

## 4. Installation

### 4.1 Windows Setup

**Step 1 — Install Ollama**

Download and run from [ollama.com/download](https://ollama.com/download). Ollama starts as a background service on `http://localhost:11434`.

**Step 2 — Pull models**

```powershell
ollama pull gemma4:12b          # LLM (~2.5 GB)
ollama pull nomic-embed-text   # Embeddings (~274 MB)
ollama list                    # Verify both appear
```

**Step 3 — Clone TerraScope**

```powershell
git clone https://github.com/your-org/terrascope.git
cd terrascope
```

**Step 4 — Python virtual environment**

```powershell
python -m venv .venv
.venv\Scripts\activate
```

**Step 5 — Install dependencies**

```powershell
pip install -r requirements.txt
```

> If you see `Microsoft Visual C++ 14.0 is required`, install  
> [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and re-run.

**Step 6 — Frontend**

```powershell
cd frontend
npm install
cd ..
```

**Step 7 — Clone your module repos**

```powershell
mkdir repos
cd repos
git clone https://github.com/your-org/terraform-google-bigquery.git
git clone https://github.com/your-org/terraform-google-gcs.git
cd ..
```

---

### 4.2 Mac Setup

```bash
# Install Homebrew, Ollama, Python, Node
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install ollama
brew services start ollama
brew install python@3.12 node

# Pull models
ollama pull gemma4:12b
ollama pull nomic-embed-text

# Clone TerraScope
git clone https://github.com/your-org/terrascope.git
cd terrascope

# Python env
python3 -m venv .venv
source .venv/bin/activate

# Dependencies
pip install -r requirements.txt
cd frontend && npm install && cd ..

# Clone module repos
mkdir -p repos && cd repos
git clone https://github.com/your-org/terraform-google-bigquery.git
cd ..
```

---

## 5. Configuration

### 5.1 terrascope.config.yaml

All core configuration lives in **`terrascope.config.yaml`** at the project root.

### Adding Repos

```yaml
repos:
  - name: terraform-google-bigquery       # Internal identifier (no spaces)
    display_name: BigQuery                 # Label shown in the UI
    local_path: ./repos/terraform-google-bigquery
    # Windows: C:\Users\yourname\repos\terraform-google-bigquery
    # Mac:     /Users/yourname/repos/terraform-google-bigquery
    gcp_product: bigquery
    description: "BigQuery datasets, tables, IAM"
    enabled: true
```

### LLM Settings

```yaml
terrascope:
  llm:
    provider: ollama
    base_url: http://localhost:11434
    model: gemma4:12b              # Change to gemma3:12b for better quality (needs 8 GB RAM)
    embedding_model: nomic-embed-text
    temperature: 0.0              # Keep at 0.0 for deterministic, fact-only answers
    max_tokens: 2048
    context_window: 8192          # Used as max output for code generation
```

### Anti-Hallucination Settings

```yaml
terrascope:
  grounding:
    mode: strict          # strict = only repo code  |  balanced = repo + LLM general knowledge
    min_confidence_threshold: 0.65
    max_retrieval_chunks: 8
```

---

### 5.2 .env — Confluence Docgen

The documentation generator reads Confluence credentials from a **`.env`** file in the project root.  
A fully-annotated template is provided at [`.env.example`](.env.example).

```bash
# Copy the template
cp .env.example .env      # Mac / Linux
copy .env.example .env    # Windows
```

Then open `.env` and fill in your values:

```env
# Required
CONFLUENCE_BASE_URL=https://yourcompany.atlassian.net/wiki
CONFLUENCE_API_TOKEN=your_api_token_here

# Cloud — also required (Basic auth: email + token)
CONFLUENCE_EMAIL=you@yourcompany.com

# Optional — leave blank to publish to your private space
CONFLUENCE_SPACE_KEY=ENG

# Optional — root page under which all product pages nest
CONFLUENCE_PARENT_TITLE=TerraScope Modules
```

**Auth modes:**

| `CONFLUENCE_EMAIL` set? | Auth used | When to use |
|-------------------------|-----------|-------------|
| Yes | HTTP Basic (`email:token`) | Confluence Cloud (always required) |
| No  | Bearer (`Authorization: Bearer <token>`) | DC / Server with PAT |

**Verify the connection:**
```bash
curl http://localhost:8000/api/docgen/config
```
```json
{ "configured": true, "message": "Confluence OK: https://... (basic auth, space=ENG)" }
```

---

## 6. Project Structure

```
terrascope/
├── terrascope.config.yaml              ← Core config (LLM, repos, grounding)
├── .env.example                        ← Confluence credential template (copy → .env)
├── .env                                ← Your credentials (git-ignored)
├── requirements.txt
│
├── templates/                          ← Docgen reference templates (NEW v2.4)
│   └── example/
│       └── field_map.yaml              ← Token → metadata mapping (copy per product)
│
├── backend/
│   ├── main.py                         ← FastAPI app + all API routes (query + curate + registry)
│   ├── config.py                       ← Typed config loader
│   │
│   ├── agent/                          ← Query pipeline (existing)
│   │   ├── models.py
│   │   ├── terrascope_agent.py
│   │   └── tools/
│   │       ├── git_tools.py
│   │       ├── hcl_tools.py
│   │       ├── search_tools.py
│   │       └── issue_tools.py
│   │
│   ├── indexer/
│   │   └── repo_indexer.py
│   │
│   ├── document_processor/             ← NEW: PDF / Word / text extraction
│   │   └── processor.py
│   │
│   ├── registry_fetcher/               ← NEW: Terraform provider doc fetching & cache
│   │   ├── registry_api.py             ← GitHub raw → registry.terraform.io scrape fallback
│   │   └── cache_manager.py            ← JSON cache at ./data/registry_cache/
│   │
│   ├── module_curator/                 ← Module generation pipeline
│   │   ├── models.py                   ← CurationSession, GenerationResult, SessionView
│   │   ├── curator.py                  ← Session orchestrator (in-memory store)
│   │   ├── question_engine.py          ← LLM Q&A (7 provider-specific questions, JSON output)
│   │   ├── code_generator.py           ← 3-pass LLM generation → 7 output files per module
│   │   ├── module_fetcher.py           ← GitHub clone / local dir / ZIP / .tf upload
│   │   ├── local_repo_scanner.py       ← Scans ./repos/ for local Terraform modules (no ChromaDB needed)
│   │   ├── dependency_resolver.py      ← Recursively resolves module {} sources (local → repos → ChromaDB → registry)
│   │   └── docgen/                     ← Confluence doc generator (NEW v2.4)
│   │       ├── config.py               ← .env → ConfluenceSettings (fail-fast validation)
│   │       ├── ir.py                   ← DocumentIR + 5 node types (the pipeline contract)
│   │       ├── manifest.py             ← Idempotency store (data/docgen_manifest.json)
│   │       ├── pipeline.py             ← Orchestrator + DocgenRequest/Result; also a CLI
│   │       ├── template/
│   │       │   ├── parser.py           ← .docx XML walk → IR (preserves diagram order)
│   │       │   └── fields.py           ← field_map.yaml loader; binds {{ token }} in IR
│   │       ├── metadata/
│   │       │   └── extractor.py        ← python-hcl2 → TFModuleMetadata
│   │       ├── diagrams/
│   │       │   └── resolver.py         ← terraform graph|dot / folder / SVG placeholder
│   │       ├── render/
│   │       │   └── storage_format.py   ← IR → Confluence storage-format XHTML
│   │       └── confluence/
│   │           ├── client.py           ← httpx REST client (create/update page, attachments)
│   │           └── space.py            ← resolve or create private (~) space
│   │
│   ├── ga_workflow/                    ← GA Release automation (v2.3 multi-cloud)
│   │   ├── ga_models.py                ← Pydantic models (CloudProvider, BreakingReason, IncrementalState)
│   │   ├── ga_detector.py              ← Multi-cloud provider detection + changelog parsing
│   │   ├── cloud_service_scanner.py    ← AWS What's New + Azure Updates + GCP release note feeds
│   │   ├── gcp_service_detector.py     ← Compat shim — routes to cloud_service_scanner
│   │   ├── ga_orchestrator.py          ← 7-stage pipeline + incremental state tracking
│   │   └── ga_router.py                ← FastAPI endpoints for GA workflow
│   │
│   └── troubleshooter/                 ← Module bug detection + version recommendation (NEW v2.3)
│       ├── models.py                   ← TroubleshootIssue, VersionRecommendation, TroubleshootResult
│       └── troubleshooter.py           ← Static analysis · LLM review · changelog-based version suggestion
│
├── frontend/
│   └── src/
│       └── App.jsx                     ← React UI — Chat + Curate + GA Workflow views
│
├── repos/                              ← Your cloned Terraform repos
├── data/
│   ├── chromadb/                       ← Vector index (auto-created)
│   ├── registry_cache/                 ← Provider doc cache (auto-created)
│   │   ├── google/
│   │   ├── aws/
│   │   └── azurerm/
│   ├── ga_state/                       ← GA workflow incremental state (auto-created)
│   │   └── {repo_name}.json            ← Applied change hashes per repo
│   └── docgen_manifest.json            ← Docgen idempotency store (auto-created)
└── output/                             ← Generated modules (auto-created)
    └── cloud_run_20250509_143022/
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        ├── versions.tf
        ├── README.md
        ├── terraform.tfvars.example
        └── examples/
            └── complete/
                └── main.tf
```

---

## 7. Indexing Your Repos

Indexing is required before using the **Chat** view. It is **not** required for the **Curate** view.

```bash
# Mac/Linux — activate venv first
source .venv/bin/activate

# Windows
.venv\Scripts\activate

# Index all enabled repos
python -m backend.indexer.repo_indexer

# Index one repo
python -m backend.indexer.repo_indexer --repo terraform-google-bigquery

# Force re-index
python -m backend.indexer.repo_indexer --force

# Only the 5 most recent tags
python -m backend.indexer.repo_indexer --tags-limit 5
```

Indexing is **incremental** — already-indexed tags are skipped. You can also click **"⟳ Index Repos"** in the UI.

---

## 8. Running TerraScope

### ⚡ Single command (recommended — v2.5+)

```bash
cd frontend
npm run dev
```

That's it. The Vite dev server **automatically spawns the FastAPI backend** alongside itself using the backend plugin in `vite.config.js`.

**Python resolution order** (first match wins):
1. `../.venv/Scripts/python.exe` — Windows virtual environment
2. `../.venv/bin/python3` — Unix/Mac virtual environment
3. System `python` / `python3`

**To skip auto-start** (e.g. you launched the backend manually or in a debugger):
```bash
TERRASCOPE_NO_BACKEND=1 npm run dev
```

Both processes share a single terminal window — backend logs appear inline. Stopping Vite (`Ctrl+C`) automatically kills the backend.

---

### Manual two-terminal startup (alternative)

**Terminal 1 — Backend**
```bash
# Mac/Linux
source .venv/bin/activate && python -m backend.main

# Windows
.venv\Scripts\activate && python -m backend.main
```

**Terminal 2 — Frontend**
```bash
cd frontend && TERRASCOPE_NO_BACKEND=1 npm run dev
# → http://localhost:5173
```

### Verify
```bash
curl http://localhost:8000/api/health
```
```json
{ "status": "ok", "ollama": "running", "model": "gemma4:12b", "repos_configured": 3, "grounding_mode": "strict" }
```

---

## 9. Using the UI

The top bar has **eight views** (hover any tab for a description):

```
🔭 TerraScope v2.5  [🌐 Ask AI] [💬 Repo Chat] [🔧 Curate] [📄 Docs] [🚀 GA Workflow] [🧪 Scenarios] [🔍 Troubleshoot] [⚙ Settings]
```

### 9.0 Ask AI — General Chat *(v2.5, default tab)*

Ask **anything** — no repo or tag selection needed:

- Every question automatically searches **all indexed repos** (top hits across every repo, ranked by relevance)
- Questions your repos can answer get a <span style="color:green">✓ from your repos</span> badge with expandable per-repo source citations
- General Terraform/cloud questions are answered from LLM knowledge with a <span style="color:orange">○ general knowledge</span> badge
- Mixed answers are labelled <span style="color:blue">◐ repos + general</span>
- Conversation history is kept, so follow-up questions work naturally
- Endpoint: `POST /api/chat/general` with `{question, history}`

Use **Repo Chat** instead when you want strict grounding against one specific repo + version.

### 9.1 Repo Chat — Query Existing Modules

1. Select a **repo** in the left sidebar (REPOS tab).
2. Select a **tag** (TAGS tab) — green dot = indexed.
3. Type a question and press **Enter**.
4. The response shows: query type badge · confidence meter · `✓ grounded` badge · answer · expandable source citations.

### 9.2 Curate — Generate New Modules

The Curate view has a **left config panel** and a **right Q&A + code panel**.

**Left panel controls:**
- **Curation Mode** — 4 modes (see [Section 10](#10-module-curation--detailed-guide))
- **Cloud Provider** — GCP / AWS / Azure
- **Service / Product Name** — e.g. "Cloud Run", "Lambda", "AKS"
- **Initial Description** — optional seed text
- **Start Session →** — begins the session

After clicking Start, the right panel enters **Q&A mode** — the LLM asks up to 7 clarifying questions tailored to your chosen provider (GCP / AWS / Azure). Answer each in the chat box and press Enter. After all questions are answered, the **⚡ Generate Terraform Code** button appears.

Generated files appear in a **tabbed code viewer** with per-file Copy buttons. The output directory path is shown at the top.

### 9.3 Docs — Documentation Generator *(v2.5)*

A dedicated panel for generating Google-doc-enriched Confluence pages for **any product**, curated or not.

**Left panel:**
- **Product / Service Name** — e.g. "BigQuery", "Cloud Run", "Memorystore"
- **Cloud Provider** — GCP / AWS / Azure
- **Module Path** (optional) — path to a generated module directory; populates inputs/outputs/resources tables
- **Fetch Google official docs** — toggle: pulls product overview, features and security notes from `cloud.google.com` + Terraform registry; synthesises with local LLM
- **Document Types** — checkboxes for HLD, CPSD, Architectural Design, Highly Confidential Assessment

**Two-phase flow:**
1. **① Generate Preview (local)** — renders each document in a white-page HTML view; no Confluence calls
2. **② Publish to Confluence** — enabled only after a successful preview and only when Confluence is configured; returns clickable page URLs per doc type

A direct link to the Settings tab appears when Confluence is not yet configured.

### 9.4 GA Workflow

Select a repo in the sidebar, switch to the **🚀 GA Workflow** view, and you'll see:

- **Provider badge** — GCP / AWS / Azure chip auto-detected from `versions.tf`
- **Version info** — current vs latest GA version, with "UPGRADE AVAILABLE" or "UP TO DATE" badge
- **Breaking changes count** — red badge when breaking changes are present
- **GA Workflow tab** — configure base branch, dry run, auto-fix, then click **🚀 Run GA Workflow**
  - After completion: pipeline stage list, changes breakdown with breaking reason and migration notes, logs
- **Cloud Scan tab** — scan the cloud service's release notes for new GA features not yet in the module

See [GA_WORKFLOW_README.md](./GA_WORKFLOW_README.md) for full details.

### 9.5 Troubleshoot

Switch to the **🔍 Troubleshoot** tab to analyse any module for bugs without running `terraform plan`.

**Left panel:**
- **Repository** — dropdown of all configured repos
- **Tag / Branch** — dropdown of all Git tags for the selected repo (type freely if not yet cloned)
- **Problem / Error** — optional: paste an error message or describe the symptom to guide the LLM analysis
- **Scan Module** — starts the three-stage pipeline

**Right panel — results:**

| Section | What it shows |
|---------|---------------|
| **Summary banner** | Error / warning / info counts at a glance |
| **Version Recommendation** | Current vs recommended provider version · `SAFE UPGRADE` or `N BREAKING` badge · list of relevant fixes in the recommended version · link to changelog |
| **Issue list** | Each issue has a severity icon (✗ / ⚠ / ℹ), category chip, file + line number, resource type, message, and expandable suggestion |

**Severity filter** buttons let you focus on errors only, warnings only, or info items.

**Example issues detected:**

| Category | Example |
|----------|---------|
| `security` | `allUsers` in IAM binding — grants public access |
| `security` | Hardcoded password in resource block |
| `deprecated` | `uniform_bucket_level_access = false` on GCS bucket |
| `undefined_ref` | `var.region` referenced but not declared in `variables.tf` |
| `provider` | No `required_version` constraint in `versions.tf` |
| `logic` | Missing `depends_on` for implicit resource dependency (LLM) |
| `best_practice` | Variable `name` has no description |

**Example version recommendation:**

```
Current: v4.0.0  →  Recommended: v5.12.0
SAFE UPGRADE — no breaking changes affect this module's resources.

Relevant fixes in v5.12.0:
• google_bigquery_dataset: fixed IAM binding propagation delay
• google_storage_bucket: corrected lifecycle rule type validation
```

---

## 10. Module Curation — Detailed Guide

### 10.1 Mode 1: New Product

**Goal:** Generate a Terraform module for a cloud service you don't have yet.

**Steps:**
1. Select **Cloud Provider**: GCP / AWS / Azure.
2. Enter the **Service Name**: `Cloud Run`, `S3`, `Azure Functions`, etc.
3. Optionally add an **Initial Description** to seed the LLM.
4. Click **Start Session →**.
5. TerraScope fetches provider documentation from the Terraform Registry (or uses its local cache if offline).
6. The LLM generates 5 clarifying questions. Answer each one.
7. Click **⚡ Generate Terraform Code**.
8. Files appear in the browser and are written to `./output/cloud_run_TIMESTAMP/`.

**Example — GCP Cloud Run module:**

```
Provider: GCP
Service:  Cloud Run
---
Q: What is the primary workload for the Cloud Run module, and what GCP region(s) should it target?
A: HTTP API for ML inference — us-central1 and europe-west1 via variables

Q: Should resources be in an existing Shared VPC or will the module create its own VPC?
A: Existing Shared VPC; accept var.vpc_connector_id

Q: Which GCP service accounts and IAM roles are needed? Should the module create them?
A: Create a dedicated SA with roles/run.invoker for var.invoker_principal

Q: Is CMEK required? If so, which Cloud KMS key ring and key name?
A: No CMEK for this module

Q: Should the module support multi-region deployments or regional failover?
A: Yes — deploy both regions via for_each over var.regions map

Q: What mandatory resource labels must every GCP resource carry?
A: team, environment, cost-center

Q: Which other GCP services does this module integrate with?
A: Secret Manager for env vars; Artifact Registry for the container image
---
→ Generates (7 files): main.tf · variables.tf · outputs.tf · versions.tf · README.md · examples/complete/main.tf · terraform.tfvars.example
→ Written to: ./output/cloud_run_20250509_153012/
```

**Generated `main.tf` excerpt:**
```hcl
resource "google_cloud_run_v2_service" "this" {
  name     = "${var.name_prefix}-${var.environment}"
  location = var.region
  project  = var.project_id

  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    vpc_access {
      connector = var.vpc_connector_id
      egress    = "ALL_TRAFFIC"
    }
    containers {
      image = var.container_image
    }
  }
}
```

---

### 10.2 Mode 2: From Document

**Goal:** Turn a written specification (PDF, Word, or plain text) into a Terraform module.

**Steps:**
1. Select **Cloud Provider** and enter the **Service Name**.
2. Click **Start Session →**.
3. The left panel shows **"📄 Upload PDF / DOCX / TXT"** — click it and pick your file.
4. TerraScope extracts the document text and sends it to the LLM alongside provider docs.
5. The LLM generates clarifying questions based on what's in the spec.
6. Answer all questions, then click **⚡ Generate Terraform Code**.

**Supported formats:**
- `.pdf` — extracted with `pdfplumber` (text-based PDFs work best; scanned images are not OCR'd)
- `.docx` / `.doc` — extracted with `python-docx`
- `.txt`, `.md`, or any plain text file

**Example — AWS Lambda from a Word spec:**

```
Provider:  AWS
Service:   Lambda
Document:  lambda_design_spec.docx  (uploaded)
---
Q: What aspects of the spec need clarification?
A: The spec mentions SQS trigger but doesn't specify batch size — use 10

Q: Which regions?
A: us-east-1 only, hardcoded in versions.tf provider block

Q: Dead-letter queue required?
A: Yes, use an SQS DLQ, name it "${var.function_name}-dlq"

... (5 questions total)
---
→ Generates Lambda module with SQS trigger, DLQ, IAM role, CloudWatch log group
```

---

### 10.3 Mode 3: From Module

**Goal:** Use an existing Terraform module as a starting point and generate an improved or adapted version.

**Source options:**

| Input | How |
|-------|-----|
| GitHub URL | Enter URL + optional tag/branch → TerraScope clones it |
| Local path | Enter absolute path on your machine |
| ZIP archive | Upload a `.zip` file containing `.tf` files |
| Single `.tf` file | Upload directly |

**Steps:**
1. Select **Cloud Provider** and **Service Name**.
2. Click **Start Session →**.
3. In the left panel under **"Provide Source"**, pick your input method and supply the source.
4. TerraScope loads all `.tf` files, resolves any `module {}` source references (checks ChromaDB first, then the Terraform Registry), and generates questions.
5. Answer questions → Generate.

**Example — Clone a public GCS module and adapt it:**

```
Provider:  GCP
Service:   Cloud Storage
Source:    GitHub → https://github.com/terraform-google-modules/terraform-google-cloud-storage
           Tag: v6.0.0
---
Q: Which existing resources should be modified?
A: Keep google_storage_bucket, add lifecycle rules for 90-day archival

Q: Should versioning always be enabled?
A: Yes, make it non-optional and remove the variable

... (5 questions)
---
→ Generates adapted GCS module based on v6.0.0 with requested changes
```

**Cross-module reference resolution:**  
If the loaded module has `source = "terraform-google-modules/network/google"`, TerraScope:
1. Searches all indexed repos in ChromaDB for matching snippets.
2. If not found locally, fetches the module's docs from the Terraform Registry.
3. Includes the resolved context in the generation prompt so the LLM understands module interfaces.

---

### 10.4 Mode 4: Self-Curation

**Goal:** Modify an already-indexed TerraScope repo, apply changes, commit, and create a new Git tag.

**Steps:**
1. Select **Self-Curation** mode.
2. Select an **Existing Repo** from the dropdown (populated from your `terrascope.config.yaml`).
3. Enter the **New Tag Name** (e.g. `v2.1.0`).
4. Optionally add an **Initial Description** of what needs to change.
5. Click **Start Session →**.
6. TerraScope loads the current `.tf` files from the repo's latest tag.
7. The LLM asks clarifying questions about the requested changes.
8. Click **⚡ Generate Terraform Code**.
9. Modified `.tf` files are written to `./output/` for review **and** committed + tagged in the repo's working tree.

**Example — Add lifecycle policies to the GCS module and tag as v3.0.0:**

```
Mode:    Self-Curation
Repo:    terraform-google-gcs
New Tag: v3.0.0
---
Q: What specific changes should v3.0.0 include?
A: Add lifecycle_rules variable supporting archive/delete conditions

Q: Should the new variable be optional (with no lifecycle rules by default)?
A: Yes, default to empty list []

Q: Are there breaking changes consumers must know about?
A: No breaking changes, purely additive

Q: Should lifecycle rules apply to all buckets or be per-bucket?
A: Per-bucket, each bucket entry gets its own lifecycle_rules

Q: Naming conventions to follow?
A: Follow existing snake_case, add lifecycle_enabled boolean alongside the rules list
---
→ Updated main.tf, variables.tf, outputs.tf written to:
     ./output/terraform_google_gcs_20250509_161200/
→ Changes committed to repo's working tree
→ Git tag v3.0.0 created in ./repos/terraform-google-gcs/
```

> **Note:** Self-curation writes directly to the repo's working tree and creates a new commit + tag. Ensure you have a clean working tree or have committed pending changes before running.

---

## 11. Confluence Documentation Generator *(v2.5)*

TerraScope generates a full documentation set — **HLD**, **CPSD**, **Architectural Design**, and **Highly Confidential Assessment** — for **any product**, curated or not. v2.5 adds Google official-docs enrichment, a rich built-in template, and a two-phase local-preview → Confluence-publish flow.

### 11.1 How It Works (v2.5 pipeline)

```
Product name (+ optional module path)
        │
        ▼
  gcp_docs_fetcher.py        fetch product overview, features, security notes,
        │                    official URLs from cloud.google.com + Terraform registry
        │                    → synthesise with local LLM (temperature 0.0)
        ▼
  metadata/extractor.py      parse .tf files → TFModuleMetadata
        │                    (resource types, variables, outputs, versions)
        ▼  merge_docs_bundle()
  Enriched TFModuleMetadata  (overview · key_features · security_considerations ·
        │                     apis_required · common_roles · official_doc_urls)
        ▼
  template/parser.py         parse reference .docx → DocumentIR
        │                    (OR use rich built-in default when no .docx exists)
        ▼
  template/fields.py         bind all {{ tokens }} from field_map.yaml
        │                    → real HTML tables for inputs/outputs (v2.5 fix)
        ▼
  render/storage_format.py   IR → Confluence storage-format XHTML
        │                    (pipe-delimited field values → real <table> elements)
        ▼
  ① dry_run=True             write HTML to output/docgen_dry_run/ + return inline
  ② dry_run=False            create/update Confluence pages + return page_url per doc
        ▼
  manifest.py                persist page IDs → idempotent re-runs update in place
```

### 11.2 Available Template Tokens (v2.5 — complete list)

| Token | Source | Format |
|-------|--------|--------|
| `product_name` | `metadata.service_name` | text |
| `provider_name` | literal.Google Cloud Platform | text |
| `module_description` | `metadata.description` | text |
| `terraform_version` | `metadata.terraform_version` | text |
| `provider_version` | `metadata.provider_version` | text |
| `resource_types_list` | `metadata.resource_types` | bullet_list |
| `module_calls_list` | `metadata.module_calls` | bullet_list |
| `required_inputs_table` | `metadata.required_inputs` | table (name\|type\|description) |
| `optional_inputs_table` | `metadata.optional_inputs` | table (name\|type\|default\|description) |
| `outputs_table` | `metadata.outputs` | table (name\|description) |
| `readme_excerpt` | `metadata.readme` | text |
| `product_overview` | `metadata.overview` (LLM-synthesised) | text |
| `key_features_list` | `metadata.key_features` (LLM-synthesised) | bullet_list |
| `security_considerations_list` | `metadata.security_considerations` | bullet_list |
| `apis_required_list` | `metadata.apis_required` | bullet_list |
| `common_roles_list` | `metadata.common_roles` | bullet_list |
| `official_docs_links` | `metadata.official_doc_urls` | bullet_list |

### 11.3 Setup

**Confluence credentials** — set once in the **⚙ Settings** tab or manually in `.env`:

```env
CONFLUENCE_BASE_URL=https://your-org.atlassian.net/wiki
CONFLUENCE_API_TOKEN=your_api_token
CONFLUENCE_EMAIL=you@your-org.com        # Cloud auth; omit for DC PAT
CONFLUENCE_SPACE_KEY=TF                   # blank = private space
CONFLUENCE_PARENT_TITLE=TerraScope Modules
```

Verify: `GET /api/docgen/config` or click **Test Connections** in Settings.

**Custom template (optional)** — if no `.docx` is provided the built-in template covers all sections. To use your own org template:

```
templates/
└── bigquery/
    ├── bigquery.docx   ← reference Word doc with {{ token }} placeholders
    └── field_map.yaml  ← copy + extend templates/example/field_map.yaml
```

### 11.4 Using the Docs Tab (UI — recommended)

1. Click **📄 Docs** in the top nav.
2. Enter a **Product / Service Name** (e.g. "BigQuery").
3. Optionally set a **Module Path** to a generated module directory.
4. Click **① Generate Preview (local)** — each doc type renders in a white-page HTML view.
5. Review. Then click **② Publish to Confluence** — page URLs appear when done.

### 11.5 API

```bash
# Phase 1 — preview (always safe, no Confluence calls)
curl -X POST http://localhost:8000/api/docgen/preview \
  -H "Content-Type: application/json" \
  -d '{"product_name":"bigquery","provider":"google","fetch_docs":true,"doc_types":["HLD","CPSD"]}'

# Phase 2 — publish (requires .env configured)
curl -X POST http://localhost:8000/api/docgen/publish \
  -H "Content-Type: application/json" \
  -d '{"product_name":"bigquery","provider":"google","fetch_docs":true,"doc_types":["HLD","CPSD"]}'

# After a completed curation session
curl -X POST http://localhost:8000/api/curate/{SESSION_ID}/docgen \
  -H "Content-Type: application/json" \
  -d '{"product_name":"bigquery"}'
```

**Response includes** `page_url` per doc type, `official_doc_urls`, and `sources_used`.

### 11.6 CLI

```bash
python -m backend.module_curator.docgen.pipeline \
    --product bigquery \
    --module-path output/bigquery_20260601_120000/ \
    --dry-run     # preview only; omit to publish
```


python -m backend.module_curator.docgen.pipeline \
    --product bigquery \
    --module-path output/bigquery_20260601_120000 \
    --dry-run
```

```
  [ok] HLD:                        output\docgen_dry_run\bigquery_hld.html
  [ok] CPSD:                       output\docgen_dry_run\bigquery_cpsd.html
  [ok] Architectural Design:       output\docgen_dry_run\bigquery_architectural_design.html
  [ok] Highly Confidential Assessment: output\docgen_dry_run\bigquery_highly_confidential_assessment.html
```

Open any of these in a browser to verify layout before publishing.

---

## 12. Registry Doc Fetching  <!-- anchor kept for ToC -->

TerraScope automatically fetches Terraform provider documentation for use in code generation.

### How It Works

```
Request for "Cloud Run" (GCP)
         │
         ▼
  Check local cache
  ./data/registry_cache/google/google_cloud_run_v2_service.json
         │
    Hit? │  Miss?
    Yes  │   No → Is network available?
         │              │
         │         Yes  │   No
         │          ▼   │    ▼
         │   Fetch from │  Return placeholder
         │   GitHub raw │  "[Offline — no cached docs]"
         │   (markdown) │
         │          │   │
         │    404?  │   │
         │      ▼   │   │
         │   Scrape registry.terraform.io
         │   (BeautifulSoup fallback)
         │          │
         │          ▼
         │   Save to cache (72h TTL)
         │          │
         └──────────┘
                   │
                   ▼
         Return documentation text
         (used in generation prompt)
```

### Manual Cache Priming

To pre-download docs before going offline:

```bash
# Via API (JSON body)
curl -X POST http://localhost:8000/api/registry/fetch \
  -H "Content-Type: application/json" \
  -d '{"provider": "google", "service_name": "Cloud Run"}'

curl -X POST http://localhost:8000/api/registry/fetch \
  -H "Content-Type: application/json" \
  -d '{"provider": "aws", "service_name": "Lambda"}'

curl -X POST http://localhost:8000/api/registry/fetch \
  -H "Content-Type: application/json" \
  -d '{"provider": "azurerm", "service_name": "AKS"}'

# Check cache status
curl http://localhost:8000/api/registry/status
```

```json
{
  "network_available": true,
  "cache": {
    "google": 12,
    "aws": 8,
    "azurerm": 3
  }
}
```

### Supported Service Names

Any of these are understood by TerraScope (case-insensitive, partial matching):

**GCP:** Cloud Run, BigQuery, Cloud Storage / GCS, Pub/Sub, Cloud SQL, GKE / Kubernetes Engine, Cloud Functions, Cloud Build, Dataflow, Spanner, Firestore, VPC, Compute Engine / GCE, Artifact Registry, Secret Manager, Memorystore / Redis, Dataproc, Composer, Bigtable, Vertex AI, AlloyDB, IAM, DNS, Load Balancer, Cloud Tasks, Cloud Scheduler, Cloud Armor

**AWS:** S3, EC2, Lambda, RDS, EKS, ECS, DynamoDB, SQS, SNS, VPC, IAM, CloudFront, API Gateway, ElastiCache, Kinesis, Glue, EMR, Redshift, MSK, Step Functions, EventBridge, Secrets Manager, CloudWatch, Route53, ALB, ECR

**Azure:** Azure Functions, Blob Storage / Storage, AKS, SQL, Cosmos DB, Service Bus, Event Hub, VNet, App Service, Container Apps, Key Vault, IAM, Data Factory, Synapse, Databricks, PostgreSQL, Redis, Container Registry, Monitor

---

## 13. API Reference

All endpoints at `http://localhost:8000`.

### Existing Endpoints

#### `GET /api/health`
```json
{
  "status": "ok",
  "ollama": "running",
  "model": "gemma4:12b",
  "repos_configured": 3,
  "grounding_mode": "strict",
  "network_available": true
}
```

#### `GET /api/repos`
Lists all enabled repos with tags, indexed tags, and indexing status.

#### `GET /api/repos/{repo_name}/tags`
Lists all Git tags for a specific repo with indexed status.

#### `POST /api/query`
```json
{
  "question": "What variables are required in v1.3.0?",
  "repo_name": "terraform-google-bigquery",
  "tag": "v1.3.0",
  "strict_mode": true
}
```
Returns `AgentResponse` with `query_type`, `answer`, `confidence`, `grounded`, `sources[]`, `variables[]`, `resources[]`, `issue_solution`.

#### `POST /api/index`
```json
{ "repo_name": "terraform-google-bigquery", "force": false }
```
Triggers background indexing. `repo_name: null` indexes all repos.

#### `GET /api/index/status`
Returns per-repo indexing status (chunks, tags, status).

---

### Curation Endpoints (New in v2.0)

#### `POST /api/curate/start`

Start a new curation session. For `new_product` and `self_curation` modes, Q&A begins immediately.

```json
{
  "mode": "new_product",
  "provider": "google",
  "service_name": "Cloud Run",
  "description": "HTTP API with VPC, auto-scaling, no public access",
  "repo_name": null,
  "new_tag": null
}
```

**For self_curation:**
```json
{
  "mode": "self_curation",
  "provider": "google",
  "service_name": "Cloud Storage",
  "repo_name": "terraform-google-gcs",
  "new_tag": "v3.0.0",
  "description": "Add lifecycle rules variable"
}
```

**Response** (`SessionView`):
```json
{
  "session_id": "a3f2c1d8-...",
  "mode": "new_product",
  "provider": "google",
  "service_name": "Cloud Run",
  "status": "asking",
  "questions": ["What is the primary workload?", "..."],
  "current_question_idx": 0,
  "current_question": "What is the primary workload?",
  "qa_pairs": [],
  "tf_files_loaded": [],
  "registry_docs_available": true,
  "all_questions_answered": false,
  "result": null
}
```

#### `GET /api/curate/{session_id}`
Poll session state at any time.

#### `POST /api/curate/{session_id}/upload-doc`
Multipart upload of a PDF, DOCX, or TXT document. Extracts text and triggers Q&A.

```bash
curl -X POST http://localhost:8000/api/curate/{SESSION_ID}/upload-doc \
  -F "file=@design_spec.pdf"
```

#### `POST /api/curate/{session_id}/upload-module`
Upload a `.zip` archive or a single `.tf` file.

```bash
curl -X POST http://localhost:8000/api/curate/{SESSION_ID}/upload-module \
  -F "file=@my_module.zip"
```

#### `POST /api/curate/{session_id}/set-source`
Set a GitHub URL or local path as the module source.

```json
{ "source_type": "github", "url": "https://github.com/org/repo", "tag": "v2.0.0" }
{ "source_type": "local",  "path": "C:\\Users\\me\\terraform-module" }
```

#### `POST /api/curate/{session_id}/answer`
Submit an answer to the current clarifying question.

```json
{ "answer": "us-central1, production-grade, VPC required" }
```

Returns the updated `SessionView`. When `all_questions_answered` becomes `true`, call `/generate`.

#### `POST /api/curate/{session_id}/generate`
Trigger code generation. Long-running (10–60s depending on model).

Returns `SessionView` with `result` populated:
```json
{
  "status": "done",
  "result": {
    "files": [
      { "filename": "main.tf",                    "content": "locals { ... } resource \"google_cloud_run_v2_service\" ..." },
      { "filename": "variables.tf",               "content": "variable \"project_id\" { type = string ... }" },
      { "filename": "outputs.tf",                 "content": "output \"service_url\" { ... }" },
      { "filename": "versions.tf",                "content": "terraform { required_version = \">= 1.9.0\" ... }" },
      { "filename": "README.md",                  "content": "# Terraform google Cloud Run Module ..." },
      { "filename": "examples/complete/main.tf",  "content": "module \"cloud_run\" { source = \"../../\" ... }" },
      { "filename": "terraform.tfvars.example",   "content": "project_id = \"my-gcp-project\" ..." }
    ],
    "summary": "Cloud Run module with VPC connectivity and no public access",
    "usage_example": "module \"cloud_run\" {\n  source = \"./\"\n  ...\n}",
    "output_dir": "C:\\Users\\me\\terrascope\\output\\cloud_run_20250509_153012",
    "git_tag_created": false,
    "git_tag_name": null
  }
}
```

---

### Docgen Endpoints (New in v2.4)

#### `GET /api/docgen/config`

Check whether Confluence credentials are configured correctly.

```json
{ "configured": true, "message": "Confluence OK: https://... (basic auth, space=ENG)" }
```

When `configured` is `false`, the message contains the specific missing key.

---

#### `POST /api/curate/{session_id}/docgen`

Generate Confluence documentation for a curation session that has reached `DONE` state. Fields not supplied in the request body are filled from the session's service name and output directory.

```json
{
  "product_name": "bigquery",
  "ref_template": "templates/bigquery/bigquery.docx",
  "field_map":    "templates/bigquery/field_map.yaml",
  "doc_types":    ["HLD", "CPSD", "Architectural Design", "Highly Confidential Assessment"],
  "assets_dir":   null,
  "dry_run":      false,
  "dry_run_dir":  "output/docgen_dry_run"
}
```

Response:

```json
{
  "product_name": "bigquery",
  "space_key": "ENG",
  "dry_run": false,
  "pages": [
    { "doc_type": "HLD",                        "page_id": "123456", "status": "ok" },
    { "doc_type": "CPSD",                       "page_id": "123457", "status": "ok" },
    { "doc_type": "Architectural Design",        "page_id": "123458", "status": "ok" },
    { "doc_type": "Highly Confidential Assessment", "page_id": "123459", "status": "ok" }
  ]
}
```

---

#### `POST /api/docgen/run`

Run the docgen pipeline directly without a curation session. Supply `module_path` (local directory) or `tf_files` (inline dict of filename → HCL content).

```json
{
  "product_name": "bigquery",
  "module_path":  "output/bigquery_20260601_120000",
  "ref_template": "templates/bigquery/bigquery.docx",
  "field_map":    "templates/bigquery/field_map.yaml",
  "dry_run": true
}
```

When `dry_run` is `true`, each page result contains `dry_run_path` instead of `page_id`.

---

### Registry Endpoints (New in v2.0)

#### `GET /api/registry/status`
```json
{
  "network_available": true,
  "cache": { "google": 12, "aws": 8, "azurerm": 3 }
}
```

#### `POST /api/registry/fetch`
Manually trigger doc fetch and cache for a provider+service. Accepts a JSON body:
```json
{ "provider": "google", "service_name": "Cloud Run" }
```
Response:
```json
{ "provider": "google", "service_name": "Cloud Run", "fetched": true, "preview": "# Terraform Docs: Cloud Run (google)..." }
```

---

### Troubleshoot Endpoint (New in v2.3)

#### `POST /api/troubleshoot`

Analyse a Terraform module at a specific tag for bugs and get a safe upgrade recommendation.

```json
{
  "repo_name": "terraform-google-bigquery",
  "tag": "v2.1.0",
  "problem_description": "terraform apply fails with 403 on dataset creation"
}
```

Response:
```json
{
  "repo_name": "terraform-google-bigquery",
  "tag": "v2.1.0",
  "scan_date": "2026-05-31T10:00:00Z",
  "summary": "Found 2 errors, 1 warning in terraform-google-bigquery at v2.1.0. Recommended upgrade: v4.0.0 → v5.12.0 (no breaking changes).",
  "error_count": 2,
  "warning_count": 1,
  "info_count": 3,
  "scanned_files": ["main.tf", "variables.tf", "outputs.tf", "versions.tf"],
  "version_recommendation": {
    "current_version": "4.0.0",
    "recommended_version": "5.12.0",
    "reason": "Upgrade is safe — no breaking changes affect this module's resources.",
    "breaking_changes": 0,
    "safe_to_upgrade": true,
    "changelog_url": "https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/CHANGELOG.md",
    "fixes_in_version": [
      "google_bigquery_dataset: fixed IAM binding propagation delay",
      "google_storage_bucket: corrected lifecycle rule type validation"
    ]
  },
  "issues": [
    {
      "severity": "error",
      "category": "security",
      "file_path": "main.tf",
      "line": 42,
      "resource_type": "google_project_iam_binding",
      "resource_name": "dataset_viewer",
      "message": "IAM member 'allUsers' grants public access to the project.",
      "suggestion": "Restrict to specific service accounts or groups.",
      "fixed_in_version": null
    },
    {
      "severity": "warning",
      "category": "deprecated",
      "file_path": "main.tf",
      "line": 17,
      "resource_type": "google_storage_bucket",
      "resource_name": "raw",
      "message": "uniform_bucket_level_access = false is deprecated and insecure.",
      "suggestion": "Set uniform_bucket_level_access = true for consistent IAM.",
      "fixed_in_version": null
    }
  ]
}
```

**Fields:**
- `tag` — omit to use the latest Git tag
- `problem_description` — optional symptom text that guides the LLM analysis

---

### GA Workflow Endpoints (New in v2.3)

#### `GET /api/ga/detect/{repo_name}`

Detect the latest GA provider version and compare to what the module uses. Auto-detects cloud provider.

```json
{
  "repo_name": "terraform-google-bigquery",
  "cloud_provider": "google",
  "current_version": "5.38.0",
  "latest_ga_version": "5.42.0",
  "upgrade_required": true,
  "breaking_changes": 1,
  "new_features": 3,
  "changes": [
    {
      "change_type": "removed",
      "resource_type": "google_bigquery_dataset",
      "attribute": "default_encryption_configuration",
      "description": "Attribute removed in 5.40",
      "breaking": true,
      "breaking_reason": "removed",
      "migration_note": "Remove 'default_encryption_configuration' from google_bigquery_dataset; use google_bigquery_dataset_iam_binding for encryption settings.",
      "cloud_provider": "google"
    }
  ]
}
```

#### `POST /api/ga/workflow`

Run the full 7-stage GA upgrade pipeline for a repo.

```json
{
  "repo_name": "terraform-google-bigquery",
  "base_branch": "main",
  "dry_run": false,
  "auto_fix": true
}
```

#### `GET /api/ga/scan/{repo_name}`

Scan the cloud service associated with this repo for new GA features not yet in the module code. Auto-detects provider from `versions.tf`.

#### `GET /api/ga/products`

List all supported cloud products across GCP, AWS, and Azure.

```json
{
  "total": 43,
  "by_cloud": { "gcp": 23, "aws": 10, "azure": 10 },
  "supported_products": { ... }
}
```

---

## 14. Code Deep Dive

### 13.1 Config Loader (`backend/config.py`)

Pydantic v2 model validates `terrascope.config.yaml` at startup. `get_config()` returns a singleton. `RepoConfig.normalize_path()` handles Windows/Mac paths transparently.

### 13.2 PydanticAI Query Agent (`backend/agent/terrascope_agent.py`)

`run_query()` pre-fetches: HCL module summary + semantic search results + issue KB match. Injects all context into a single prompt sent to Ollama. Post-processes: confidence threshold, source grounding, disclaimer.

### 13.3 Curation Session (`backend/module_curator/curator.py`)

In-memory session store (`dict[session_id → CurationSession]`). Sessions progress through: `GATHERING → ASKING → READY → GENERATING → DONE`. `create_session()` → `start_*()` → `answer_question()` → `generate()`.

### 13.4 Question Engine (`backend/module_curator/question_engine.py`)

Calls Ollama chat API directly (no pydantic-ai). Prompt asks for a JSON array of **7** strings, specifying all 7 topic areas (workload, networking, IAM, encryption, HA, tagging, integrations). Strips markdown fences before `json.loads()`. Falls back to **provider-specific** hardcoded questions (separate sets for GCP, AWS, Azure) if the LLM output cannot be parsed.

### 13.5 Code Generator (`backend/module_curator/code_generator.py`)

Uses **3 focused LLM passes** so each file group gets a dedicated full token budget (`max(3072, min(6144, context_window - 2048))` tokens per pass):

| Pass | Prompt focuses on | Output files |
|------|-------------------|--------------|
| A | resources, locals, data sources, security defaults | `main.tf` |
| B | all variables used in Pass A output + useful outputs | `variables.tf`, `outputs.tf` |
| C | support files referencing Pass A+B content | `versions.tf`, `README.md`, `examples/complete/main.tf`, `terraform.tfvars.example` |

Each pass uses `[FILE: name]...[/FILE]` markers. Missing files always fall back to provider-aware stubs (`_minimal_vars`, `_minimal_outputs`, `_minimal_example`, `_minimal_tfvars`). `_usage_from_files()` parses `variables.tf` to find required variables and generates a real module call with realistic example values per provider.

For `self_curation`: after writing output files, also writes root-level `.tf` files to the repo path, `git add`s them, commits, and calls `repo.create_tag()`.

### 13.6 Module Fetcher (`backend/module_curator/module_fetcher.py`)

- **GitHub**: `Repo.clone_from(url, dest, depth=1)` then `repo.git.checkout(tag)` if specified.
- **Local**: `Path(path).rglob("*.tf")` — recursive.
- **ZIP**: `zipfile.ZipFile.extractall()` to temp dir then same recursive walk.
- **Cross-module**: `extract_module_sources()` regex-extracts `source = "..."` values. Non-local sources are looked up in ChromaDB first, then the Registry API.

### 13.7 Registry API (`backend/registry_fetcher/registry_api.py`)

`resolve_resources(service_name, provider)` maps 80+ known service names to resource types via a lookup dict with substring fallback. `fetch_resource_docs()` checks cache → GitHub raw markdown → `registry.terraform.io` BeautifulSoup scrape. `is_network_available()` tries a 3-second TCP connect to `8.8.8.8:53`.

### 13.8 Document Processor (`backend/document_processor/processor.py`)

`extract_text(bytes, filename)` dispatches on extension: `.pdf` → `pdfplumber`, `.docx` → `python-docx`, everything else → UTF-8 decode.

### 13.9 Repo Indexer (`backend/indexer/repo_indexer.py`)

Chunks `.tf` files at HCL block boundaries (resource/variable/output/data). Each chunk gets a rich prefix (`File: ... | Tag: ... | Type: ... | Name: ...`) for better embedding relevance. Upserts to ChromaDB in batches of 100. Collection name: `{repo_name_underscored}__{tag_underscored}` (max 63 chars).

### 14.10 FastAPI Backend (`backend/main.py`)

All routes in one file. Curation endpoints are session-based (stateless HTTP, server-side session store). File uploads use `UploadFile` from `python-multipart`. Background indexing via `BackgroundTasks` + `run_in_executor`.

### 14.11 Confluence Docgen (`backend/module_curator/docgen/`)

Seven-module pipeline wired into two FastAPI routes and a standalone CLI.

| Stage | Module | Key design |
|-------|--------|-----------|
| Parse | `template/parser.py` | Walks `doc.element.body` children in XML order (not the high-level python-docx lists) so heading/table/diagram sequence is preserved exactly. Images extracted via `doc.part.related_parts[r:embed]`. |
| Bind | `template/fields.py` | `FieldMap.resolve()` dispatches on `source` type (`metadata.*`, `literal.*`), then formats the value as `text`, `bullet_list`, or `table`. Substitution covers whole-paragraph `FieldNode`s, inline `{{ token }}` in `ParagraphNode` text, and `TableNode` cells. |
| Metadata | `metadata/extractor.py` | Reads `variables.tf`, `outputs.tf`, `main.tf`, `versions.tf` via `python-hcl2`. Works from a local directory (`extract_from_path`) or an in-memory dict (`extract_from_files`). |
| Diagrams | `diagrams/resolver.py` | Resolution chain: `assets_dir/<product>/<slug>.<ext>` → `terraform graph\|dot -Tpng` (HLD / Arch only) → SVG placeholder. Never raises. |
| Render | `render/storage_format.py` | Maps IR nodes to Confluence storage-format XHTML. Diagrams become `<ac:image><ri:attachment …/>` when attachment map is provided, or italic placeholder text in dry-run. |
| Publish | `confluence/client.py` | Uses `httpx.BasicAuth` or `Authorization: Bearer` header per `auth_mode`. `create_page` / `update_page` / `upload_attachment` / `add_label`. |
| Idempotency | `manifest.py` + `confluence/space.py` | JSON manifest at `data/docgen_manifest.json` maps product → `{parent_page_id, pages, space_key}`. Private space resolved via `GET /rest/api/user/current` → `~<accountId>`. |

**IR discriminated union** — `DocumentIR.nodes` is `list[Annotated[Union[HeadingNode, ParagraphNode, TableNode, DiagramNode, FieldNode], Field(discriminator="kind")]]`. Each node carries a literal `kind` field so Pydantic round-trips cleanly to/from JSON for inspection (`ir.to_json()` / `DocumentIR.from_json()`).

### 14.12 Module Troubleshooter (`backend/troubleshooter/`)

Three-stage async pipeline, all reading from Git history — no working tree checkout needed:

**Stage 1 — Static analysis** (`_static_analysis`)

| Check | How |
|-------|-----|
| HCL parse errors | `parse_hcl_content()` on every `.tf` file; flags files that return `{}` on non-empty input |
| Undefined `var.X` / `local.X` | Regex scan of raw file content against declared variable/local names |
| Missing variable descriptions | Walk `variable {}` blocks in parsed AST |
| Deprecated resource patterns | 5 regex patterns covering GCP/AWS/Azure anti-patterns |
| Security anti-patterns | 5 regex patterns: public IAM, insecure ingress, weak SSL, missing prevent_destroy, hardcoded passwords |
| Provider constraints | `get_provider_requirements()` — flags missing `required_version`, missing version pins, overly permissive constraints |

**Stage 2 — LLM analysis** (`_llm_analysis`)

Sends a compact representation of the module (max 6000 chars, priority order: `main.tf` → `variables.tf` → rest) plus the optional user problem description to Ollama. Asks for a structured JSON array of issues covering: logic bugs, type mismatches, missing `depends_on`, anti-patterns, security, and deprecated usage. Response parsed with regex extraction + `json.loads()`.

**Stage 3 — Version recommendation** (`_version_recommendation`)

1. Detects provider from `versions.tf` via `detect_terraform_provider()`
2. Gets current version and latest GA version from Terraform Registry
3. Downloads full CHANGELOG.md for the provider
4. `_parse_changelog_versions()` splits the changelog into per-version sections
5. Walks versions ascending from current, extracting BUG FIXES and BREAKING CHANGES sections
6. Counts breaking changes that match the module's resource type prefixes (`_count_relevant()`)
7. Returns the minimum version with relevant fixes and zero breaking changes for the module's resources
8. If every candidate version has breaking changes, reports the first version with fixes and notes the breaking count

---

## 15. Anti-Hallucination Design

The query agent uses 5 layers to prevent hallucinations:

| Layer | Mechanism |
|-------|-----------|
| **Temperature 0.0** | No creative sampling — fully deterministic |
| **Strict system prompt** | Forbids using training knowledge in `strict` mode |
| **Pre-fetched context** | HCL parse + semantic search results injected before LLM call |
| **Confidence threshold** | Below 0.65 → yellow disclaimer; below 0.5 → "I don't know" |
| **Typed Pydantic output** | `AgentResponse` enforces schema — no free-form fields |

The **curation generator** trades some strictness for creativity (temperature 0.1 for generation) but is grounded by: provider documentation, existing module code, and explicit Q&A answers.

---

## 16. Supported Products

### GCP (Query + Curate)
BigQuery · Cloud Storage · Dataflow · Pub/Sub · Cloud SQL · GKE · Cloud Functions · Cloud Build · Spanner · Firestore · Bigtable · Cloud Composer · Dataproc · Vertex AI · Cloud Run · Artifact Registry · Secret Manager · Memorystore · Datastream · AlloyDB · VPC · Compute Engine · IAM · DNS · Load Balancer · Cloud Armor · Cloud Tasks · Cloud Scheduler

### AWS (Curate only)
S3 · EC2 · Lambda · RDS · EKS · ECS · DynamoDB · SQS · SNS · VPC · IAM · CloudFront · API Gateway · ElastiCache · Kinesis · Glue · EMR · Redshift · MSK · Step Functions · EventBridge · Secrets Manager · CloudWatch · Route53 · ALB · ECR

### Azure (Curate only)
Azure Functions · Blob Storage · AKS · SQL · Cosmos DB · Service Bus · Event Hub · VNet · App Service · Container Apps · Key Vault · IAM · Data Factory · Synapse · Databricks · PostgreSQL · Redis · Container Registry · Monitor

---

## 17. Troubleshooting

### Ollama offline
```
"ollama": "unreachable — start Ollama first"
```
- Windows: Check the Ollama icon in the system tray. Restart via Start Menu if absent.
- Mac: `brew services restart ollama` or reopen the Ollama app.

### Model not found during curation
```
Error code: 404 - {'error': {'message': "model 'gemma4:12b' not found"}}
```
Run `ollama pull gemma4:12b` and wait for the download to complete.

### PDF extraction returns blank
`pdfplumber` works on text-based PDFs. Scanned documents need OCR pre-processing (not included). Convert to text or DOCX first.

### GitHub clone fails during "From Module"
```
Cannot clone https://github.com/...: ...
```
- Ensure `git` is in your PATH.
- Private repos require credentials: use a personal access token in the URL: `https://token@github.com/org/repo`.
- On Windows with proxy, set `GIT_SSL_NO_VERIFY=true` if behind a corporate proxy.

### Self-curation: git tag creation fails
The working tree must be on a branch (not detached HEAD) and must not have uncommitted conflicts. Run `git status` in the repo dir to verify.

### Registry docs show "[Offline — no cached docs]"
Pre-populate the cache while online:
```bash
curl -X POST http://localhost:8000/api/registry/fetch \
  -H "Content-Type: application/json" \
  -d '{"provider": "google", "service_name": "Cloud Run"}'
```

### Docgen: `CONFLUENCE_BASE_URL is not set`

Copy `.env.example` to `.env` and fill in at minimum `CONFLUENCE_BASE_URL` and `CONFLUENCE_API_TOKEN`. The server must be restarted after editing `.env` (pydantic-settings caches the values at import time).

### Docgen: `401 Unauthorized` from Confluence

- **Cloud:** `CONFLUENCE_EMAIL` must be set (Cloud requires Basic auth: `email:token`).
- **DC/Server:** Leave `CONFLUENCE_EMAIL` blank and use a PAT as `CONFLUENCE_API_TOKEN`.
- Verify the token hasn't expired and has `Write` permission on the target space.

### Docgen: pages are created but diagrams are missing

The page is published first; attachments are uploaded afterwards. If the upload fails (non-fatal), the diagram renders as `[Diagram: <caption>]` placeholder text. Check the page labels — if `terrascope:<product>` is present the pipeline ran successfully; missing attachments are a separate issue.

### Docgen: `Space '...' not found`

`CONFLUENCE_SPACE_KEY` is set to a key that doesn't exist. Either correct the key or leave it blank to let TerraScope use your private space.

### ChromaDB corruption after hard shutdown
```bash
# Delete the index and re-index
rm -rf data/chromadb
python -m backend.indexer.repo_indexer --force
```

### `Microsoft Visual C++ 14.0 is required` on Windows
Install [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/), selecting "C++ build tools" workload, then re-run `pip install -r requirements.txt`.

---

## 18. FAQ

**Q: Does the curation pipeline require internet access?**  
A: No. If network is unavailable, it uses the local registry doc cache. Generation works 100% offline using Ollama. The first run of each service name fetches docs; subsequent runs use the cache (72h TTL).

**Q: How long does code generation take?**  
A: Typically 45–120 seconds with `gemma4:12b` (3 LLM passes). A larger model like `gemma3:12b` improves quality at the cost of 2–3× more time per pass.

**Q: Can I generate modules for services not in the known service map?**  
A: Yes. Enter any service name — TerraScope will construct a plausible resource name (e.g. `google_my_service`) and generate code based on the Q&A answers alone. For best results, prime the cache first or ensure network access so it can scrape the registry.

**Q: Does self-curation push to GitHub automatically?**  
A: No. It commits locally and creates a local tag. You push manually: `git push origin v2.1.0` after reviewing the generated changes.

**Q: Can I edit the generated files before they're committed (self-curation)?**  
A: Yes — click **⚡ Generate Terraform Code**, then review the files in the code viewer. The commit only happens during the generate step. If you want to edit first, use **New Product** or **From Module** mode instead, edit the files in `./output/`, and then manually copy them to the repo.

**Q: Why does the LLM ask the same 7 questions every time?**  
A: If Ollama returns a non-JSON response, the question engine uses provider-specific fallback questions (separate sets for GCP, AWS, Azure). This usually means the model is overloaded or the context was too long. Try a smaller prompt in the description field.

**Q: How do I add a new cloud service to the registry map?**  
A: Edit `SERVICE_TO_RESOURCE_PREFIX` in `backend/registry_fetcher/registry_api.py`. Add a lowercase service name key mapped to a list of Terraform resource type strings.

**Q: Is the Chat (query) view affected by the v2.0 changes?**  
A: No. The query pipeline (`/api/query`), indexer, and ChromaDB are unchanged. All v2.0 additions are additive.

**Q: Does the Troubleshooter require the repo to be cloned locally?**  
A: Yes — it reads `.tf` file contents from the local Git history via `git show`. The repo must be cloned under `./repos/` (or the path in `terrascope.config.yaml`) before troubleshooting works. If files can't be read, the static analysis returns a provider-constraint warning and the LLM stage is skipped.

**Q: Can I troubleshoot a specific old tag that no longer exists on the remote?**  
A: Yes. The troubleshooter reads from your local Git history, so any tag that exists in the local clone (even if deleted from the remote) is analysable. Select it from the Tag dropdown.

**Q: How accurate is the LLM analysis in the Troubleshooter?**  
A: The static analysis (undefined references, security patterns, deprecated resources, provider constraints) is fully deterministic. The LLM stage adds logical analysis — quality depends on the model. With `gemma4:12b` expect good coverage of obvious anti-patterns; a larger model (`gemma3:12b`) gives more thorough results. Always review LLM suggestions — they can occasionally flag false positives.

**Q: How does the version recommendation decide "safe to upgrade"?**  
A: It parses the CHANGELOG.md for every provider version between your current version and the latest GA. A version is considered "breaking for this module" only if its `### BREAKING CHANGES` section mentions resource types that are actually used in your module. If no such breaking changes appear in the recommended version's entry, it's marked `SAFE UPGRADE`.

**Q: Does docgen require a reference .docx file?**  
A: No. If the template file doesn't exist, the pipeline generates a minimal page (title + resource list + inputs table + outputs table) from metadata alone. For branded, properly structured documentation you should supply a reference `.docx`.

**Q: What happens if I run docgen twice for the same product?**  
A: The second run calls Confluence's update-page API on the existing pages rather than creating new ones. Page IDs are tracked in `data/docgen_manifest.json`. If the manifest is deleted, the pipeline falls back to a title search before creating new pages, so you won't get duplicates.

**Q: Can I generate only specific document types?**  
A: Yes — set `"doc_types": ["HLD"]` in the API request or `--doc-types HLD` on the CLI. The default is all four types.

**Q: Can I supply my own diagram images instead of the auto-generated ones?**  
A: Yes. Create `templates/<product>/` and put images named `hld.png`, `cpsd.png`, `architectural_design.png`, `highly_confidential_assessment.png` (or `.svg`/`.jpg`). Pass `"assets_dir": "templates/<product>"` in the request. These take priority over auto-generation.

**Q: Does docgen work with Confluence Data Center / Server?**  
A: Yes. Set `CONFLUENCE_BASE_URL` to your DC/Server URL (no trailing `/wiki`), set `CONFLUENCE_API_TOKEN` to a PAT, and leave `CONFLUENCE_EMAIL` blank. The client auto-detects the API path.

**Q: What Terraform providers does the Troubleshooter support?**  
A: All three: `hashicorp/google` (GCP), `hashicorp/aws`, and `hashicorp/azurerm`. The provider is auto-detected from `versions.tf`. Security and deprecated-usage patterns are provider-specific; the LLM analysis works for any provider.
