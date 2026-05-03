# 🔭 TerraScope

> **AI-powered agent for Google Cloud Terraform module curation.**  
> Ask natural-language questions about any tag, version, variable, resource, or issue across your locally cloned Terraform module repositories. Answers are strictly grounded in your repo code — no hallucinations.

---

## Table of Contents

1. [What TerraScope Does](#1-what-terrascope-does)
2. [How It Works — Architecture](#2-how-it-works--architecture)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
   - [Windows Setup](#41-windows-setup)
   - [Mac Setup](#42-mac-setup)
5. [Configuration](#5-configuration)
   - [Adding Repos](#51-adding-repos)
   - [LLM Settings](#52-llm-settings)
   - [Anti-Hallucination Settings](#53-anti-hallucination-settings)
6. [Project Structure](#6-project-structure)
7. [Indexing Your Repos](#7-indexing-your-repos)
8. [Running TerraScope](#8-running-terrascope)
9. [Using the UI](#9-using-the-ui)
10. [API Reference](#10-api-reference)
11. [Code Deep Dive](#11-code-deep-dive)
    - [Config Loader](#111-config-loader-backendconfigpy)
    - [PydanticAI Agent](#112-pydanticai-agent-backendagentterrascope_agentpy)
    - [Git Tools](#113-git-tools-backendagenttoolsgit_toolspy)
    - [HCL Parser](#114-hcl-parser-backendagenttoolshcl_toolspy)
    - [Search / RAG](#115-search--rag-backendagenttoolssearch_toolspy)
    - [Issue Knowledge Base](#116-issue-knowledge-base-backendagenttoolsissue_toolspy)
    - [Repo Indexer](#117-repo-indexer-backendindexerrepo_indexerpy)
    - [FastAPI Backend](#118-fastapi-backend-backendmainpy)
    - [React Frontend](#119-react-frontend-frontendsrcappjsx)
12. [Anti-Hallucination Design](#12-anti-hallucination-design)
13. [Supported GCP Products](#13-supported-gcp-products)
14. [Troubleshooting](#14-troubleshooting)
15. [FAQ](#15-faq)

---

## 1. What TerraScope Does

TerraScope is an internal tool for Terraform module curation teams working with Google Cloud. It:

- **Reads your local Git repos** — no cloud sync, no upload, everything stays on your machine.
- **Indexes every Git tag/release** — each version of your module is independently searchable.
- **Understands Terraform HCL code** — parses `.tf` files with a real AST parser (`python-hcl2`), not regex. It knows the difference between a `resource` block and a `variable` block.
- **Answers questions about any tag** — "What variables are required in v1.3?" or "What changed between v1.5 and v2.0?"
- **Diagnoses issues** — matches error messages against a GCP-specific knowledge base and returns structured remediation steps.
- **Runs 100% locally** — uses Ollama + Gemma 3 4B. No API keys, no data leaving your network, no cloud LLM costs.

### Example Questions

| Question | Query Type |
|----------|-----------|
| `What GCP resources does this module create at v2.1?` | Resource |
| `What variables are required in v1.3.0?` | Variable |
| `What changed between v1.5.0 and v2.0.0?` | Comparison |
| `Why does terraform apply fail with Error 403 on BigQuery?` | Issue |
| `Does this module support CMEK encryption?` | General |
| `Which tag first added VPC Service Controls support?` | General |
| `Show me all IAM bindings in v2.0` | Security |
| `What provider version does v1.4 require?` | Dependency |

---

## 2. How It Works — Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Your Machine                                 │
│                                                                       │
│  ┌────────────────┐    HTTP     ┌──────────────────────────────────┐ │
│  │  React UI      │ ◄────────► │  FastAPI Backend  :8000          │ │
│  │  :5173         │            │                                  │ │
│  └────────────────┘            │  ┌────────────────────────────┐  │ │
│                                │  │   PydanticAI Agent         │  │ │
│                                │  │                            │  │ │
│                                │  │  Tools:                    │  │ │
│                                │  │  • git_tools    (read repo)│  │ │
│                                │  │  • hcl_tools    (parse HCL)│  │ │
│                                │  │  • search_tools (RAG)      │  │ │
│                                │  │  • issue_tools  (KB match) │  │ │
│                                │  └──────────┬─────────────────┘  │ │
│                                │             │ prompts             │ │
│                                │  ┌──────────▼─────────────────┐  │ │
│                                │  │  Ollama   :11434           │  │ │
│                                │  │  Model: gemma3:4b          │  │ │
│                                │  │  Embed: nomic-embed-text   │  │ │
│                                │  └────────────────────────────┘  │ │
│                                │                                  │ │
│                                │  ┌────────────────────────────┐  │ │
│                                │  │  ChromaDB (local files)    │  │ │
│                                │  │  ./data/chromadb/          │  │ │
│                                │  │  Per-tag vector collections│  │ │
│                                │  └────────────────────────────┘  │ │
│                                └──────────────────────────────────┘ │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Your Cloned Repos  (read-only, never modified)                │  │
│  │  ./repos/terraform-google-bigquery   (all tags accessible)     │  │
│  │  ./repos/terraform-google-gcs                                  │  │
│  │  ./repos/terraform-google-dataflow                             │  │
│  └────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

**Data flow for a query:**

1. User types a question in the React UI, selects a repo and tag.
2. UI `POST /api/query` to FastAPI.
3. FastAPI calls `run_query()` in the PydanticAI agent.
4. Agent pre-fetches: module summary (HCL parse), top semantic search results, known issue KB match.
5. Agent sends all context + question to Gemma 3 4B via Ollama.
6. Gemma produces a structured `AgentResponse` (typed Pydantic model).
7. Post-processing: confidence threshold check, source grounding validation, disclaimer injection.
8. FastAPI returns typed JSON to the UI.
9. UI renders the answer, source citations (with expandable code snippets), and issue cards.

---

## 3. Prerequisites

### All Platforms

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | 3.12 recommended |
| Node.js | 18+ | For the React UI |
| Git | Any | Must be in PATH |
| Ollama | Latest | [ollama.com](https://ollama.com) |
| Free disk space | ~5 GB | Models (~2.5 GB) + ChromaDB index |
| RAM | 8 GB min | 16 GB recommended for Gemma 3 4B |

### Windows-specific
- Git for Windows: [git-scm.com](https://git-scm.com/download/win) — installs Git in PATH
- Python from [python.org](https://www.python.org/downloads/) — check "Add Python to PATH" during install
- Windows Terminal (recommended) — available from Microsoft Store

### Mac-specific
- Homebrew: [brew.sh](https://brew.sh) — used to install dependencies
- Xcode Command Line Tools: `xcode-select --install`

---

## 4. Installation

### 4.1 Windows Setup

Open **Windows Terminal** (or PowerShell as Administrator for the Ollama install).

**Step 1 — Install Ollama**

Download and run the Windows installer from [ollama.com/download](https://ollama.com/download).  
After install, Ollama runs as a background service on `http://localhost:11434`.

**Step 2 — Pull the required models**

```powershell
# Pull the LLM (Gemma 3 4B — ~2.5 GB download)
ollama pull gemma3:4b

# Pull the embedding model (~274 MB download)
ollama pull nomic-embed-text

# Verify both are available
ollama list
```

**Step 3 — Clone TerraScope**

```powershell
git clone https://github.com/your-org/terrascope.git
cd terrascope
```

**Step 4 — Create a Python virtual environment**

```powershell
python -m venv .venv
.venv\Scripts\activate
```

> You'll see `(.venv)` in your prompt confirming the venv is active.  
> Run `.venv\Scripts\activate` again each time you open a new terminal.

**Step 5 — Install backend dependencies**

```powershell
pip install -r requirements.txt
```

> If you see a `Microsoft Visual C++ 14.0 is required` error, install  
> [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/)  
> and re-run the pip install.

**Step 6 — Install frontend dependencies**

```powershell
cd frontend
npm install
cd ..
```

**Step 7 — Clone your Terraform module repos**

```powershell
# Create a repos directory inside terrascope
mkdir repos
cd repos

# Clone each module repo
git clone https://github.com/your-org/terraform-google-bigquery.git
git clone https://github.com/your-org/terraform-google-gcs.git
# ... add more as needed

cd ..
```

---

### 4.2 Mac Setup

Open **Terminal**.

**Step 1 — Install Homebrew (if not already installed)**

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Step 2 — Install Ollama**

```bash
brew install ollama

# Start Ollama as a background service
brew services start ollama
```

Alternatively, download the Mac app from [ollama.com/download](https://ollama.com/download).

**Step 3 — Pull the required models**

```bash
ollama pull gemma3:4b
ollama pull nomic-embed-text
ollama list
```

**Step 4 — Install Python 3.11+**

```bash
brew install python@3.12
python3 --version  # should show 3.12.x
```

**Step 5 — Install Node.js**

```bash
brew install node
node --version  # should show 18+
```

**Step 6 — Clone TerraScope**

```bash
git clone https://github.com/your-org/terrascope.git
cd terrascope
```

**Step 7 — Create a Python virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

> You'll see `(.venv)` in your prompt. Run `source .venv/bin/activate` again in each new terminal session.

**Step 8 — Install backend dependencies**

```bash
pip install -r requirements.txt
```

**Step 9 — Install frontend dependencies**

```bash
cd frontend
npm install
cd ..
```

**Step 10 — Clone your Terraform module repos**

```bash
mkdir -p repos
cd repos
git clone https://github.com/your-org/terraform-google-bigquery.git
git clone https://github.com/your-org/terraform-google-gcs.git
cd ..
```

---

## 5. Configuration

All configuration lives in one file: **`terrascope.config.yaml`** in the project root.  
This is the only file you need to edit to add repos, change models, or tune behavior.

### 5.1 Adding Repos

Each repo entry under `repos:` is one Terraform module repository. Copy and paste a block to add more:

```yaml
repos:
  - name: terraform-google-bigquery       # Internal identifier (no spaces)
    display_name: BigQuery                 # Label shown in the UI
    local_path: ./repos/terraform-google-bigquery  # Path to your cloned repo

    # Windows absolute path example:
    # local_path: C:\Users\yourname\repos\terraform-google-bigquery

    # Mac absolute path example:
    # local_path: /Users/yourname/repos/terraform-google-bigquery

    gcp_product: bigquery                  # Used for GCP context (see supported products)
    description: "BigQuery datasets, tables, IAM"
    enabled: true                          # Set to false to temporarily hide from UI
```

**Supported values for `gcp_product`:** `bigquery`, `storage`, `dataflow`, `pubsub`, `dataproc`, `composer`, `spanner`, `bigtable`, or any custom string (the agent will still work, just without product-specific GCP context enrichment).

**Path rules:**
- Relative paths (e.g., `./repos/...`) are resolved from the `terrascope/` project root.
- Both Windows backslash (`C:\Users\...`) and forward slash (`C:/Users/...`) paths work on Windows.
- Environment variables are expanded: `$HOME/repos/...` works on Mac/Linux.
- Tilde expansion works: `~/repos/...` is valid everywhere.

To **disable** a repo without deleting its config entry, set `enabled: false`. The UI will hide it and the indexer will skip it.

### 5.2 LLM Settings

```yaml
terrascope:
  llm:
    provider: ollama
    base_url: http://localhost:11434   # Change if Ollama runs on a different port
    model: gemma3:4b                   # Must match an installed ollama model name
    embedding_model: nomic-embed-text  # Must match an installed ollama model name
    temperature: 0.0                   # 0.0 = deterministic. Do not increase for fact tasks.
    max_tokens: 2048
    context_window: 8192
```

**To use a different model** (e.g., if you have more RAM):

```yaml
model: gemma3:12b        # More accurate, needs ~8 GB RAM
model: llama3.2:3b       # Faster, less accurate
model: mistral:7b        # Alternative option
```

Pull the model first: `ollama pull gemma3:12b`

### 5.3 Anti-Hallucination Settings

```yaml
terrascope:
  grounding:
    mode: strict           # strict = only repo code. balanced = repo code + LLM knowledge
    require_source_citation: true
    min_confidence_threshold: 0.65   # Answers below this confidence show a disclaimer
    max_retrieval_chunks: 8          # How many code chunks to retrieve per query
    chunk_overlap_tokens: 50
```

**`mode: strict`** (recommended for curation teams) — the agent refuses to answer from general knowledge. If a question cannot be answered from your indexed repo code, it says "I don't know" rather than guessing.

**`mode: balanced`** — supplements repo code with LLM general knowledge, but labels general knowledge clearly as `[General knowledge — not from repo code]`.

**`min_confidence_threshold`** — when the agent's self-assessed confidence falls below this value, a yellow disclaimer is shown in the UI. Useful for catching low-quality answers. Range: 0.0–1.0.

---

## 6. Project Structure

```
terrascope/
│
├── terrascope.config.yaml          ← THE ONE CONFIG FILE. Edit this to add repos.
├── requirements.txt                ← Python backend dependencies
│
├── backend/
│   ├── main.py                     ← FastAPI app entry point + all API routes
│   ├── config.py                   ← Typed config loader (Pydantic Settings)
│   │
│   ├── agent/
│   │   ├── models.py               ← All Pydantic I/O models (AgentResponse, etc.)
│   │   ├── terrascope_agent.py     ← PydanticAI agent definition + run_query()
│   │   │
│   │   └── tools/
│   │       ├── git_tools.py        ← List tags, read files, diff tags via GitPython
│   │       ├── hcl_tools.py        ← Parse .tf files: variables, resources, outputs, IAM
│   │       ├── search_tools.py     ← ChromaDB semantic search, per-tag scoping
│   │       └── issue_tools.py      ← Pattern-matched GCP issue KB + solution lookup
│   │
│   └── indexer/
│       └── repo_indexer.py         ← Walk repos at every tag, chunk HCL, embed to ChromaDB
│
├── frontend/
│   └── src/
│       └── App.jsx                 ← Full React UI (single file, all components)
│
├── repos/                          ← Put your cloned Terraform module repos here
│   ├── terraform-google-bigquery/
│   ├── terraform-google-gcs/
│   └── ...
│
└── data/
    └── chromadb/                   ← Auto-created. ChromaDB vector index (persistent)
```

**Key principle: `terrascope.config.yaml` is the only file you ever need to edit.**  
Adding a new repo = adding 6 lines of YAML. No code changes required.

---

## 7. Indexing Your Repos

Indexing reads every Git tag in your repos, parses all `.tf` files at each tag, and stores vector embeddings in ChromaDB. This must be done before querying.

**Activate your virtual environment first (every terminal session):**

```bash
# Mac/Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

**Index all enabled repos:**

```bash
python -m backend.indexer.repo_indexer
```

**Index a specific repo only:**

```bash
python -m backend.indexer.repo_indexer --repo terraform-google-bigquery
```

**Force re-index (overwrite existing index):**

```bash
python -m backend.indexer.repo_indexer --force
```

**Index only the 5 most recent tags (useful for large repos with many old tags):**

```bash
python -m backend.indexer.repo_indexer --tags-limit 5
```

**Combine flags:**

```bash
python -m backend.indexer.repo_indexer --repo terraform-google-bigquery --force --tags-limit 10
```

**What happens during indexing:**

```
Indexing repo: terraform-google-bigquery
====================================================
  Tags to index: ['v2.1.0', 'v2.0.0', 'v1.5.2', ...]
  → Indexing terraform-google-bigquery@v2.1.0...
     Batch 1: 100 chunks ✓
     Batch 2: 47 chunks ✓
  ✅ terraform-google-bigquery@v2.1.0: 147 chunks indexed → collection 'terraform_google_bigquery__v2_1_0'
  ✓ terraform-google-bigquery@v2.0.0 already indexed (139 chunks). Skipping.
  ...
✅ All repos indexed. Total chunks: 1247
   Time: 84.3s
```

**Indexing is incremental** — already-indexed tags are skipped on subsequent runs. Only new tags need embedding. Embed time depends on the number of `.tf` files and available hardware (GPU via Ollama accelerates this).

**You can also trigger indexing from the UI** using the "Index Repos" button in the top bar — this runs indexing in the background and the UI shows progress.

---

## 8. Running TerraScope

You need two processes running simultaneously: the backend API and the frontend dev server.

### Terminal 1 — Backend API

```bash
# Mac/Linux
source .venv/bin/activate
python -m backend.main

# Windows
.venv\Scripts\activate
python -m backend.main
```

Expected output:
```
🔭 TerraScope API starting...
   LLM: gemma3:4b via http://localhost:11434
   Repos: ['terraform-google-bigquery', 'terraform-google-gcs']
   Grounding: strict
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

### Terminal 2 — Frontend UI

```bash
cd frontend
npm run dev
```

Expected output:
```
  VITE v5.x.x  ready in 312 ms
  ➜  Local:   http://localhost:5173/
```

### Open the UI

Navigate to **http://localhost:5173** in your browser.

### Verifying Everything Is Running

Check the health endpoint:

```bash
# Mac/Linux
curl http://localhost:8000/api/health

# Windows PowerShell
Invoke-WebRequest -Uri http://localhost:8000/api/health | Select-Object -ExpandProperty Content
```

Expected response:
```json
{
  "status": "ok",
  "ollama": "running",
  "model": "gemma3:4b",
  "repos_configured": 3,
  "grounding_mode": "strict"
}
```

---

## 9. Using the UI

### Layout

```
┌─────────────────────────────────────────────────────────┐
│  🔭 TerraScope  v1.0   [STRICT GROUNDING]  [⟳ Index]  │  ← Top bar
├──────────────┬──────────────────────────────────────────┤
│  REPOS │ TAGS│  CONTEXT BAR: BigQuery › v2.1.0  ●      │
├──────────────┤                                          │
│  ● BigQuery  │  [welcome screen / conversation]         │
│  ○ Storage   │                                          │
│  ○ Dataflow  │                                          │
│              │                                          │
│  [tag list   │                                          │
│   when TAGS  │                                          │
│   tab open]  │                                          │
│              │  ┌──────────────────────────────────┐   │
│              │  │  Ask about BigQuery @ v2.1.0...  │   │
│              │  └──────────────────────────────────┘   │
└──────────────┴──────────────────────────────────────────┘
```

### Workflow

1. **Select a repo** from the left sidebar (REPOS tab). The color dot indicates the GCP product.
2. **Select a tag** (switch to TAGS tab). Green dot = indexed, grey = not yet indexed.
3. **Type your question** in the input box and press Enter (or Shift+Enter for a new line).
4. The agent responds with:
   - A **query type badge** (Resource, Issue, Variable, Comparison, etc.)
   - A **confidence meter** — green (>80%), yellow (60–80%), red (<60%)
   - A **"✓ grounded"** badge when the answer is backed by cited code
   - The **answer text**
   - **Source citations** — expandable chips showing the exact `.tf` file, line numbers, and code snippet
   - An **Issue Card** (for error/issue questions) — root cause + numbered steps + gcloud commands + optional Terraform fix

### Tips

- If you see a **yellow disclaimer**, the agent didn't find enough indexed code to be confident. Try re-indexing or checking your tag selection.
- Click **source chips** to expand and see the exact code the agent used to form its answer.
- The **"Index Repos"** button in the top bar indexes any new tags added since last run — you don't need to restart anything.
- Use the **TAGS tab** to verify which tags are indexed (green dot) before querying an older version.

---

## 10. API Reference

All endpoints are at `http://localhost:8000`.

### `GET /api/health`
Returns current system status including Ollama availability and configured repos.

### `GET /api/repos`
Lists all enabled repos with their tags, indexed tags, and current indexing status.

**Response:**
```json
[
  {
    "name": "terraform-google-bigquery",
    "display_name": "BigQuery",
    "gcp_product": "bigquery",
    "tags": ["v2.1.0", "v2.0.0", "v1.5.2"],
    "latest_tag": "v2.1.0",
    "indexed_tags": ["v2.1.0", "v2.0.0"],
    "indexing_status": "idle"
  }
]
```

### `GET /api/repos/{repo_name}/tags`
Lists all Git tags for a specific repo, with indexed status per tag.

### `POST /api/query`
Main query endpoint. Runs the PydanticAI agent and returns a typed response.

**Request:**
```json
{
  "question": "What variables are required in v1.3.0?",
  "repo_name": "terraform-google-bigquery",
  "tag": "v1.3.0",
  "strict_mode": true
}
```
`repo_name` and `tag` are optional — defaults to the first enabled repo and latest tag.

**Response shape:**
```json
{
  "query_type": "variable",
  "answer": "At v1.3.0, the following variables are required: ...",
  "confidence": 0.87,
  "grounded": true,
  "sources": [
    {
      "repo_name": "terraform-google-bigquery",
      "file_path": "variables.tf",
      "tag": "v1.3.0",
      "line_start": 12,
      "line_end": 28,
      "snippet": "variable \"project_id\" { ... }",
      "relevance": 0.92
    }
  ],
  "issue_solution": null,
  "variables": [...],
  "resources": [...],
  "tags_analyzed": ["v1.3.0"],
  "repo_name": "terraform-google-bigquery",
  "disclaimer": null
}
```

### `POST /api/index`
Triggers background indexing. Returns immediately; indexing runs asynchronously.

**Request:**
```json
{
  "repo_name": "terraform-google-bigquery",  // null = all repos
  "force": false
}
```

### `GET /api/index/status`
Returns indexing status for all repos: chunks indexed, tags covered, last indexed time.

---

## 11. Code Deep Dive

### 11.1 Config Loader (`backend/config.py`)

The config system uses Pydantic v2 models to validate `terrascope.config.yaml` at startup. Every field has a type, default, and validator.

Key behaviors:
- **Path normalization** — `RepoConfig.normalize_path()` is a `@field_validator` that expands `~`, environment variables, and normalizes backslashes. This is what makes Windows paths like `C:\Users\...` work transparently.
- **Singleton** — `get_config()` loads the YAML once and caches it. All modules import `get_config()`.
- **Startup validation** — On load, it checks that every `enabled: true` repo exists on disk and prints a warning (not an error) for missing repos, so one bad path doesn't block the whole system.

```python
# Any module that needs config:
from backend.config import get_config
cfg = get_config()
cfg.llm.model          # "gemma3:4b"
cfg.enabled_repos      # List[RepoConfig] - only repos with enabled=true
cfg.get_repo("name")   # Returns RepoConfig | None
```

### 11.2 PydanticAI Agent (`backend/agent/terrascope_agent.py`)

The agent wraps Gemma 3 4B (served by Ollama) using PydanticAI's `OpenAIModel` with Ollama's OpenAI-compatible endpoint at `/v1`.

**Why OpenAIModel for Ollama?** Ollama exposes a fully OpenAI-compatible REST API at `http://localhost:11434/v1`. PydanticAI's `OpenAIModel` connects to it with `api_key="ollama"` (any non-empty string works as the key).

**The `run_query()` function** is the main pipeline:

```
1. Resolve repo_name + tag (handle defaults, validate against config)
2. Check if tag is indexed in ChromaDB — return early with instructions if not
3. Pre-fetch context:
   a. summarize_module() → HCL parse summary (variables, resources, providers)
   b. semantic_search()  → top-K relevant code chunks from ChromaDB
   c. match_known_issue() → check error KB (deterministic, no LLM)
4. Build a rich context block combining all pre-fetched data
5. agent.run(context + question) → PydanticAI calls Gemma 3 4B
6. Post-process:
   a. Inject pre-retrieved sources if agent left sources empty
   b. Inject KB issue solution if agent didn't produce one
   c. Check confidence threshold, add disclaimer if below 0.65
   d. Set grounded=True only if sources[] is non-empty
7. Return AgentResponse
```

**Why pre-fetch before the agent call?** Rather than letting the agent call tools dynamically (which requires multiple LLM round-trips), we pre-fetch deterministic structured data (HCL parse, vector search) and give it to the agent as context. This is faster, cheaper (fewer Ollama calls), and more reliable.

### 11.3 Git Tools (`backend/agent/tools/git_tools.py`)

Uses `GitPython` to read repo content without modifying the working tree. All reads are done via Git object access (`commit.tree / "path/to/file"`), so the repo never has to be checked out to a different branch.

Key functions:
- `list_tags_for_repo(repo_name)` — returns tags sorted by semantic version (v1.2.3 parsed as tuple (1,2,3), not lexically). This matters because `v1.10.0` must sort after `v1.9.0`.
- `get_file_at_tag(repo_name, tag, file_path)` — reads a single file at any tag. File paths are normalized to forward slashes before passing to Git.
- `diff_tags(repo_name, tag_a, tag_b)` — runs `git diff` filtered to `*.tf` files only. Output truncated to 6,000 characters to fit context window.

### 11.4 HCL Parser (`backend/agent/tools/hcl_tools.py`)

Uses `python-hcl2` which is a real HCL2 parser (not regex). It converts `.tf` files into Python dicts using the same grammar Terraform uses.

Key functions:
- `get_all_variables(repo_name, tag)` — reads `variables.tf` (and all other `.tf` files as fallback), returns `List[VariableInfo]` with `name`, `type`, `description`, `default`, `required`, `file_path`, `line`.
- `get_all_resources(repo_name, tag)` — walks all `.tf` files, returns every `resource` block as `ResourceInfo`. Includes `resource_type` (e.g., `google_bigquery_dataset`), `resource_name`, `file_path`, `line_start`, and extracted key attributes.
- `get_iam_bindings(repo_name, tag)` — filters resources to IAM-related types (`_iam_binding`, `_iam_member`, `_iam_policy`).
- `summarize_module(repo_name, tag)` — calls all the above and returns a compact dict summary used as the agent's initial code understanding context.

**Line number tracking** — `_find_block_line()` scans the raw file line by line to find the declaration line for each block, since `python-hcl2` doesn't preserve line numbers in its parsed output.

### 11.5 Search / RAG (`backend/agent/tools/search_tools.py`)

ChromaDB stores one collection per repo-tag combination. Collection names are sanitized versions of `{repo_name}__{tag}` (dashes and dots replaced with underscores, max 63 characters).

Key behaviors:
- **Per-tag scoping** — `semantic_search(query, repo_name, tag)` only queries the collection for that specific tag. A search for `v1.3.0` will never return results from `v2.0.0`.
- **Embedding function** — uses `OllamaEmbeddingFunction` from ChromaDB's built-in integration, pointing to the `nomic-embed-text` model via Ollama. Falls back to ChromaDB's default embeddings if Ollama is unavailable.
- **Relevance scoring** — ChromaDB returns L2 distances; the search tool converts to `relevance = 1 - distance` so higher relevance = better match.
- **`is_indexed()`** — quickly checks if a collection exists and is non-empty, used by the agent to give early "not indexed" guidance before attempting a search.

### 11.6 Issue Knowledge Base (`backend/agent/tools/issue_tools.py`)

A list of hardcoded issue patterns in `ISSUES_KB`. Each issue has:
- `patterns` — list of regex patterns to match against the user's question or error text
- `root_cause` — one sentence explaining why this happens
- `solution_steps` — ordered list of remediation steps
- `gcloud_commands` — exact commands the user can run
- `terraform_fix` — HCL code snippet that fixes the issue, if applicable

`match_known_issue(error_text)` runs all patterns against the input and returns the first match as a fully structured `IssueSolution` Pydantic model. This is called **before** the LLM, so common errors (403s, provider conflicts, state locks) get fast, deterministic, hallucination-free answers.

Current KB covers:
- BigQuery API 403 errors
- Cloud Storage API 403 errors
- Dataflow API 403 errors
- Google provider version conflicts
- Terraform state lock
- State drift detection
- GCP API not enabled (any API)
- Resource already exists (409)
- CMEK / Cloud KMS permission errors

### 11.7 Repo Indexer (`backend/indexer/repo_indexer.py`)

The indexer is the one-time (and incremental) setup step that makes queries possible.

**Chunking strategy — why resource-block level?**

| Chunk level | Problem |
|-------------|---------|
| Line-by-line | Splits resource blocks across chunks, loses context |
| Whole file | Too large for context window, dilutes search relevance |
| Resource block | ✅ One `resource`, `variable`, or `output` per chunk — optimal |

`chunk_tf_file()` finds block start positions using a regex on the first word of each top-level line (`resource`, `variable`, `output`, `data`, etc.), then slices the file content between consecutive block starts. Each chunk gets a rich text prefix:

```
File: main.tf | Tag: v2.1.0 | Type: resource | Name: google_bigquery_dataset my_dataset
resource "google_bigquery_dataset" "my_dataset" {
  dataset_id = var.dataset_id
  ...
}
```

This prefix is embedded alongside the code, so semantic searches for "BigQuery dataset" or "dataset_id variable" find these chunks reliably.

**Batching** — ChromaDB is called in batches of 100 chunks to avoid timeouts on large repos.

**Incremental indexing** — `is_indexed()` is checked before processing each tag. Already-indexed tags are skipped with a "✓ already indexed" message. Running the indexer again after adding new tags is fast.

### 11.8 FastAPI Backend (`backend/main.py`)

The API uses FastAPI's `lifespan` context manager for startup logging. All endpoints are async. CORS is configured to allow the Vite dev server at `localhost:5173`.

The indexing endpoint (`POST /api/index`) uses FastAPI's `BackgroundTasks` to run indexing in a thread pool executor, so the API response returns immediately while indexing continues in the background. The `_indexing_jobs` dict tracks status per repo and is readable via `GET /api/index/status`.

### 11.9 React Frontend (`frontend/src/App.jsx`)

A single-file React app (no complex build setup). Uses only React hooks — no Redux, no router. All API calls use `fetch` directly against `http://localhost:8000/api`.

Component breakdown:
- `TerraScope` — root component, owns all state (repos, selected repo/tag, messages, loading)
- `Message` — renders one conversation turn (user or agent), decides which sub-components to show
- `IssueCard` — collapsible card for issue solutions with root cause, numbered steps, gcloud commands, HCL fix
- `SourceChip` — expandable citation chip showing file path, line range, relevance score, and raw code snippet
- `ConfidenceMeter` — horizontal progress bar colored green/yellow/red based on value
- `RepoCard` — sidebar entry for a repo with colored GCP product dot
- `Tag` — sidebar entry for a tag with indexed/not-indexed status dot
- `StatusDot` — tiny animated dot used for Ollama status, indexing status, indexed status

---

## 12. Anti-Hallucination Design

TerraScope uses five independent layers to prevent hallucinated answers:

**Layer 1 — `temperature: 0.0`**  
Gemma 3 4B is called with zero temperature, making output deterministic. No creative generation, no probabilistic word sampling.

**Layer 2 — Strict system prompt**  
The agent's system prompt in strict mode explicitly forbids using general training knowledge. Every instruction in the prompt is `MUST`, `NEVER`, or `ONLY`. The LLM is instructed that if tools return no relevant code, the correct answer is "I cannot find this in the indexed repository code."

**Layer 3 — Pre-fetched structured context**  
Rather than asking the LLM to recall facts, TerraScope feeds the LLM structured facts retrieved by deterministic code:
- HCL-parsed variable names, types, and line numbers (from `python-hcl2`)
- Semantically retrieved code chunks (from ChromaDB)
- KB-matched issue solutions (from regex pattern matching)

The LLM's job is to synthesize and explain — not to recall.

**Layer 4 — Confidence threshold + disclaimer**  
The agent self-reports confidence (0.0–1.0). When below `min_confidence_threshold` (default 0.65), the UI shows a yellow disclaimer. Below 0.5, the system returns "I don't know."

**Layer 5 — Typed Pydantic output**  
`AgentResponse` is a strict Pydantic model. Every field has a type. The agent cannot return a field that doesn't exist in the model. Source citations require `file_path`, `tag`, `line_start`, `line_end` — no vague references.

---

## 13. Supported GCP Products

The `gcp_products` section in `terrascope.config.yaml` provides product-specific context (required APIs, common IAM roles, expected provider resource types). This enriches agent answers without hallucination because the facts come from your config file, not from the LLM's memory.

| `gcp_product` value | Product | Key Resource Types |
|---------------------|---------|-------------------|
| `bigquery` | BigQuery | `google_bigquery_dataset`, `google_bigquery_table` |
| `storage` | Cloud Storage | `google_storage_bucket`, `google_storage_bucket_object` |
| `dataflow` | Dataflow | `google_dataflow_job`, `google_dataflow_flex_template_job` |
| `pubsub` | Pub/Sub | `google_pubsub_topic`, `google_pubsub_subscription` |
| `dataproc` | Dataproc | `google_dataproc_cluster`, `google_dataproc_job` |
| `composer` | Cloud Composer | `google_composer_environment` |
| `spanner` | Spanner | `google_spanner_instance`, `google_spanner_database` |
| `bigtable` | Bigtable | `google_bigtable_instance`, `google_bigtable_table` |

To add a new product, add an entry under `gcp_products:` in the config file — no code changes needed.

---

## 14. Troubleshooting

### "Ollama offline" shown in the UI

```bash
# Check if Ollama is running
ollama list

# Mac — start as service
brew services start ollama

# Windows — open the Ollama app from the Start Menu
# Or run: ollama serve
```

### `ModuleNotFoundError` when starting the backend

Your virtual environment is not active:
```bash
# Mac/Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### Repo not found warning during config load

```
⚠  Repo 'terraform-google-bigquery' not found at: /path/to/repos/terraform-google-bigquery
```

The `local_path` in your config does not match where the repo is cloned. Either:
- Clone the repo to the expected path: `git clone ... ./repos/terraform-google-bigquery`
- Or update `local_path` in `terrascope.config.yaml` to point to where you cloned it.

### "Repository not indexed" response from agent

Run the indexer first:
```bash
python -m backend.indexer.repo_indexer --repo YOUR_REPO_NAME
```

### `Microsoft Visual C++ 14.0 is required` on Windows

Install [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/) — select "C++ build tools" workload. Then re-run `pip install -r requirements.txt`.

### ChromaDB `sqlite3` error on older Python

ChromaDB requires SQLite 3.35+. If you see a SQLite version error:
```bash
pip install pysqlite3-binary
```
And add to the top of `backend/agent/tools/search_tools.py`:
```python
__import__('pysqlite3')
import sys
sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
```

### Port already in use

```bash
# Backend on a different port
python -m backend.main  # Edit terrascope.config.yaml: server.port: 8001

# Frontend on a different port
cd frontend && npm run dev -- --port 5174
```

---

## 15. FAQ

**Q: Does TerraScope modify my repos in any way?**  
No. All Git operations are read-only. TerraScope uses GitPython's object access (`commit.tree / path`) which reads Git objects directly without touching the working tree or index.

**Q: How long does indexing take?**  
Depends on repo size and hardware. A typical module repo with 10 tags and 20 `.tf` files takes 30–90 seconds. Ollama uses your GPU if available (CUDA on Windows/Linux, Metal on Mac), which speeds up embedding significantly. Subsequent runs are nearly instant since only new tags are re-indexed.

**Q: Can I run TerraScope on a shared team server?**  
Yes. Deploy the backend on a server accessible to your team, change `server.host` from `127.0.0.1` to `0.0.0.0`, and update the API URL in the frontend from `localhost:8000` to your server's address. Ollama can also be run as a server (it listens on all interfaces by default when `OLLAMA_HOST=0.0.0.0` is set).

**Q: Why Gemma 3 4B and not a larger model?**  
4B parameters is sufficient for HCL code reasoning tasks because the agent doesn't need to generate creative text — it synthesizes structured facts. The real intelligence is in the pre-fetched context (HCL parse + semantic search), not in the LLM's weights. A larger model (12B, 27B) would improve answer phrasing but not factual accuracy, since facts come from your code.

**Q: Can I add new issue patterns to the KB without code changes?**  
Currently the KB is in `backend/agent/tools/issue_tools.py`. A future version will load from `backend/knowledge/issues_kb.json` (already referenced in the code). You can move the `ISSUES_KB` list to that JSON file and add your team's custom patterns there.

**Q: What if a `.tf` file has syntax errors and can't be parsed?**  
`parse_hcl_content()` catches all exceptions and returns `{}`. The file is skipped for structured parsing but still embedded as raw text via the chunker, so semantic search still works on it. The agent will note when it can't extract structured data.

**Q: How do I update TerraScope when new code is pushed?**  
```bash
git pull
pip install -r requirements.txt  # in case dependencies changed
# Re-index if new tags were added to your module repos
python -m backend.indexer.repo_indexer
```
