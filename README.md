# 🔭 TerraScope

> **AI-powered Terraform module curation for GCP, AWS, and Azure.**  
> Query any module version in natural language, generate new modules from scratch, curate existing ones, and automate GA upgrades — all running 100% locally with Ollama.

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

---

## 🚀 GA Release Workflow

TerraScope includes a full **GA Release Workflow** that automates upgrading your modules to the latest Google Cloud provider GA release **and** scans the GCP service for new GA features not yet in your Terraform code.

| Capability | Description |
|-----------|-------------|
| **Provider GA detection** | Queries Terraform Registry for latest stable provider version |
| **GCP service scan** | Reads Google Cloud release notes + API Discovery for new GA features |
| **Branch creation** | Creates `terrascope/ga-upgrade-vX.Y.Z` automatically |
| **HCL code generation** | LLM generates updated `.tf` files for all detected changes |
| **4-layer validation** | HCL syntax · required attributes · naming conventions · type checking |
| **Provider compat check** | Verifies every new attribute exists in the target provider schema |
| **PR create / update** | Opens a GitHub PR with full change summary |

```bash
# Detect latest GA version (no changes made)
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery --detect-only

# Run the full pipeline
python -m backend.ga_workflow.ga_orchestrator --repo terraform-google-bigquery
```

📖 **Full GA docs:** [GA_WORKFLOW_README.md](./GA_WORKFLOW_README.md)

---

## Table of Contents

1. [What TerraScope Does](#1-what-terrascope-does)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
   - [Windows Setup](#41-windows-setup)
   - [Mac Setup](#42-mac-setup)
5. [Configuration](#5-configuration)
6. [Project Structure](#6-project-structure)
7. [Indexing Your Repos](#7-indexing-your-repos)
8. [Running TerraScope](#8-running-terrascope)
9. [Using the UI](#9-using-the-ui)
   - [Chat — Query Existing Modules](#91-chat--query-existing-modules)
   - [Curate — Generate New Modules](#92-curate--generate-new-modules)
   - [GA Workflow](#93-ga-workflow)
10. [Module Curation — Detailed Guide](#10-module-curation--detailed-guide)
    - [Mode 1: New Product](#101-mode-1-new-product)
    - [Mode 2: From Document](#102-mode-2-from-document)
    - [Mode 3: From Module](#103-mode-3-from-module)
    - [Mode 4: Self-Curation](#104-mode-4-self-curation)
11. [Registry Doc Fetching](#11-registry-doc-fetching)
12. [API Reference](#12-api-reference)
13. [Code Deep Dive](#13-code-deep-dive)
14. [Anti-Hallucination Design](#14-anti-hallucination-design)
15. [Supported Products](#15-supported-products)
16. [Troubleshooting](#16-troubleshooting)
17. [FAQ](#17-faq)

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
│  └─────────────────┘            │                   │                    │ │
│                                 │  ┌────────────────▼─────────────────┐  │ │
│                                 │  │  Module Curation Pipeline         │  │ │
│                                 │  │  curator → question_engine        │  │ │
│                                 │  │  code_generator → module_fetcher  │  │ │
│                                 │  └────────────────┬─────────────────┘  │ │
│                                 │                   │                    │ │
│                                 │  ┌────────────────▼─────────────────┐  │ │
│                                 │  │  Ollama   :11434                  │  │ │
│                                 │  │  LLM: gemma3:4b                   │  │ │
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
ollama pull gemma3:4b          # LLM (~2.5 GB)
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
ollama pull gemma3:4b
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

All configuration lives in one file: **`terrascope.config.yaml`** at the project root.

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
    model: gemma3:4b              # Change to gemma3:12b for better quality (needs 8 GB RAM)
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

## 6. Project Structure

```
terrascope/
├── terrascope.config.yaml              ← THE ONE CONFIG FILE
├── requirements.txt
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
│   ├── module_curator/                 ← NEW: Module generation pipeline
│   │   ├── models.py                   ← CurationSession, GenerationResult, SessionView
│   │   ├── curator.py                  ← Session orchestrator (in-memory store)
│   │   ├── question_engine.py          ← LLM Q&A (5 questions, JSON output)
│   │   ├── code_generator.py           ← Prompt builder + LLM call + .tf writer
│   │   └── module_fetcher.py           ← GitHub clone / local dir / ZIP / .tf upload
│   │
│   └── ga_workflow/                    ← GA Release automation (unchanged)
│
├── frontend/
│   └── src/
│       └── App.jsx                     ← React UI — Chat + Curate + GA Workflow views
│
├── repos/                              ← Your cloned Terraform repos
├── data/
│   ├── chromadb/                       ← Vector index (auto-created)
│   └── registry_cache/                 ← Provider doc cache (auto-created)
│       ├── google/
│       ├── aws/
│       └── azurerm/
└── output/                             ← Generated modules (auto-created)
    └── cloud_run_20250509_143022/
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        └── versions.tf
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

You need two terminals running simultaneously.

### Terminal 1 — Backend

```bash
# Mac/Linux
source .venv/bin/activate && python -m backend.main

# Windows
.venv\Scripts\activate
python -m backend.main
```

Output:
```
TerraScope API starting...
   LLM: gemma3:4b via http://localhost:11434
   Repos: ['terraform-google-bigquery', ...]
   Grounding: strict
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### Terminal 2 — Frontend

```bash
cd frontend
npm run dev
# → http://localhost:5173
```

### Verify

```bash
curl http://localhost:8000/api/health
```

```json
{
  "status": "ok",
  "ollama": "running",
  "model": "gemma3:4b",
  "repos_configured": 3,
  "grounding_mode": "strict",
  "network_available": true
}
```

The `network_available` field is new in v2.0 — when `false`, the curation pipeline automatically uses its local doc cache.

---

## 9. Using the UI

The top bar now has **three views**:

```
🔭 TerraScope v2.0  [💬 Chat] [🔧 Curate] [🚀 GA Workflow]
```

### 9.1 Chat — Query Existing Modules

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

After clicking Start, the right panel enters **Q&A mode** — the LLM asks up to 5 clarifying questions. Answer each in the chat box and press Enter. After all questions are answered, the **⚡ Generate Terraform Code** button appears.

Generated files appear in a **tabbed code viewer** with per-file Copy buttons. The output directory path is shown at the top.

### 9.3 GA Workflow

Select a repo in the sidebar, switch to the **🚀 GA Workflow** view, configure the base branch, and click **🚀 Run GA Workflow**. See [GA_WORKFLOW_README.md](./GA_WORKFLOW_README.md) for full details.

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
Q: What is the primary workload for this Cloud Run module?
A: HTTP API serving ML inference results, needs auto-scaling

Q: Which regions should this deploy to?
A: us-central1 and europe-west1, both via variables

Q: What are the security requirements?
A: VPC connector, no public access, IAM invoker role required

Q: Multiple environments (dev/staging/prod)?
A: Yes, all via a single var.environment input

Q: Naming and tagging requirements?
A: Prefix all resources with var.name_prefix, add team and env labels
---
→ Generates: main.tf, variables.tf, outputs.tf, versions.tf
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

## 11. Registry Doc Fetching

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
# Via API
curl -X POST "http://localhost:8000/api/registry/fetch?provider=google&service_name=Cloud+Run"
curl -X POST "http://localhost:8000/api/registry/fetch?provider=aws&service_name=Lambda"
curl -X POST "http://localhost:8000/api/registry/fetch?provider=azurerm&service_name=AKS"

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

## 12. API Reference

All endpoints at `http://localhost:8000`.

### Existing Endpoints

#### `GET /api/health`
```json
{
  "status": "ok",
  "ollama": "running",
  "model": "gemma3:4b",
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
      { "filename": "main.tf", "content": "resource \"google_cloud_run_v2_service\" ..." },
      { "filename": "variables.tf", "content": "variable \"project_id\" ..." },
      { "filename": "outputs.tf", "content": "output \"service_url\" ..." },
      { "filename": "versions.tf", "content": "terraform { required_version ..." }
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

### Registry Endpoints (New in v2.0)

#### `GET /api/registry/status`
```json
{
  "network_available": true,
  "cache": { "google": 12, "aws": 8, "azurerm": 3 }
}
```

#### `POST /api/registry/fetch?provider=google&service_name=Cloud+Run`
Manually trigger doc fetch and cache for a provider+service.
```json
{ "provider": "google", "service": "Cloud Run", "chars": 14520 }
```

---

## 13. Code Deep Dive

### 13.1 Config Loader (`backend/config.py`)

Pydantic v2 model validates `terrascope.config.yaml` at startup. `get_config()` returns a singleton. `RepoConfig.normalize_path()` handles Windows/Mac paths transparently.

### 13.2 PydanticAI Query Agent (`backend/agent/terrascope_agent.py`)

`run_query()` pre-fetches: HCL module summary + semantic search results + issue KB match. Injects all context into a single prompt sent to Ollama. Post-processes: confidence threshold, source grounding, disclaimer.

### 13.3 Curation Session (`backend/module_curator/curator.py`)

In-memory session store (`dict[session_id → CurationSession]`). Sessions progress through: `GATHERING → ASKING → READY → GENERATING → DONE`. `create_session()` → `start_*()` → `answer_question()` → `generate()`.

### 13.4 Question Engine (`backend/module_curator/question_engine.py`)

Calls Ollama chat API directly (no pydantic-ai). Prompt asks for a JSON array of 5 strings. Strips markdown fences before `json.loads()`. Falls back to mode-specific hardcoded questions if the LLM output cannot be parsed.

### 13.5 Code Generator (`backend/module_curator/code_generator.py`)

Builds a ~6000-token prompt combining: provider docs + spec document + existing `.tf` files + referenced modules + Q&A pairs. Calls Ollama with `max_tokens = min(4096, context_window // 2)`. Parses JSON response `{files: {filename: content}, summary, usage_example}`. Falls back to HCL-block extraction by filename markers if JSON fails.

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

### 13.10 FastAPI Backend (`backend/main.py`)

All routes in one file. Curation endpoints are session-based (stateless HTTP, server-side session store). File uploads use `UploadFile` from `python-multipart`. Background indexing via `BackgroundTasks` + `run_in_executor`.

---

## 14. Anti-Hallucination Design

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

## 15. Supported Products

### GCP (Query + Curate)
BigQuery · Cloud Storage · Dataflow · Pub/Sub · Cloud SQL · GKE · Cloud Functions · Cloud Build · Spanner · Firestore · Bigtable · Cloud Composer · Dataproc · Vertex AI · Cloud Run · Artifact Registry · Secret Manager · Memorystore · Datastream · AlloyDB · VPC · Compute Engine · IAM · DNS · Load Balancer · Cloud Armor · Cloud Tasks · Cloud Scheduler

### AWS (Curate only)
S3 · EC2 · Lambda · RDS · EKS · ECS · DynamoDB · SQS · SNS · VPC · IAM · CloudFront · API Gateway · ElastiCache · Kinesis · Glue · EMR · Redshift · MSK · Step Functions · EventBridge · Secrets Manager · CloudWatch · Route53 · ALB · ECR

### Azure (Curate only)
Azure Functions · Blob Storage · AKS · SQL · Cosmos DB · Service Bus · Event Hub · VNet · App Service · Container Apps · Key Vault · IAM · Data Factory · Synapse · Databricks · PostgreSQL · Redis · Container Registry · Monitor

---

## 16. Troubleshooting

### Ollama offline
```
"ollama": "unreachable — start Ollama first"
```
- Windows: Check the Ollama icon in the system tray. Restart via Start Menu if absent.
- Mac: `brew services restart ollama` or reopen the Ollama app.

### Model not found during curation
```
Error code: 404 - {'error': {'message': "model 'gemma3:4b' not found"}}
```
Run `ollama pull gemma3:4b` and wait for the download to complete.

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
curl -X POST "http://localhost:8000/api/registry/fetch?provider=google&service_name=Cloud+Run"
```

### ChromaDB corruption after hard shutdown
```bash
# Delete the index and re-index
rm -rf data/chromadb
python -m backend.indexer.repo_indexer --force
```

### `Microsoft Visual C++ 14.0 is required` on Windows
Install [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/), selecting "C++ build tools" workload, then re-run `pip install -r requirements.txt`.

---

## 17. FAQ

**Q: Does the curation pipeline require internet access?**  
A: No. If network is unavailable, it uses the local registry doc cache. Generation works 100% offline using Ollama. The first run of each service name fetches docs; subsequent runs use the cache (72h TTL).

**Q: How long does code generation take?**  
A: Typically 15–60 seconds with `gemma3:4b`. A larger model like `gemma3:12b` improves quality at the cost of 2–3× more time.

**Q: Can I generate modules for services not in the known service map?**  
A: Yes. Enter any service name — TerraScope will construct a plausible resource name (e.g. `google_my_service`) and generate code based on the Q&A answers alone. For best results, prime the cache first or ensure network access so it can scrape the registry.

**Q: Does self-curation push to GitHub automatically?**  
A: No. It commits locally and creates a local tag. You push manually: `git push origin v2.1.0` after reviewing the generated changes.

**Q: Can I edit the generated files before they're committed (self-curation)?**  
A: Yes — click **⚡ Generate Terraform Code**, then review the files in the code viewer. The commit only happens during the generate step. If you want to edit first, use **New Product** or **From Module** mode instead, edit the files in `./output/`, and then manually copy them to the repo.

**Q: Why does the LLM ask the same 5 questions every time?**  
A: If Ollama returns a non-JSON response, the question engine uses mode-specific fallback questions. This usually means the model is overloaded or the context was too long. Try a smaller prompt in the description field.

**Q: How do I add a new cloud service to the registry map?**  
A: Edit `SERVICE_TO_RESOURCE_PREFIX` in `backend/registry_fetcher/registry_api.py`. Add a lowercase service name key mapped to a list of Terraform resource type strings.

**Q: Is the Chat (query) view affected by the v2.0 changes?**  
A: No. The query pipeline (`/api/query`), indexer, and ChromaDB are unchanged. All v2.0 additions are additive.
