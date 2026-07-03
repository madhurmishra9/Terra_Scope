# Changelog

All notable changes to TerraScope are documented here, newest first. See [README.md](README.md) for the full feature guide.

---

## v2.8

Full-feature validation pass with a live LLM — every tab exercised end-to-end, the remaining LLM call sites fixed, and document generation made genuinely detailed.

| Change | Description |
|---|---|
| **📏 Runtime context-window probe** | Ollama serves models with its own `num_ctx` (often 4096) regardless of `terrascope.config.yaml` — and prompts that exceed it are silently truncated from the FRONT, cutting away injected grounding material while keeping the trailing instruction. Docgen's 24 000-char grounding never fit; the model literally couldn't see most of the crawled docs. New `effective_context_tokens()` probes the live instance (`/api/ps`) and sizes grounding material to what actually fits |
| **📄 Two-tier doc synthesis** | Sections the crawled official material doesn't cover are no longer left as placeholder text ("Not covered in referenced documentation"). A second, clearly-labelled pass fills them from the model's domain knowledge with a "⚠ General guidance — verify against the official documentation" caveat — a detailed-but-flagged section instead of an empty one |
| **🧠 No-think coverage completed** | PR #8 wired 7 LLM call sites; three more were found and fixed: the pydantic_ai agents behind **Repo Chat** and **Curate**'s question engine (via `model_settings.extra_body`), and **Troubleshoot**'s native `/api/chat` call (`think: false`). All three also gained the `trust_env=False` proxy guard they were missing |
| **🔍 Troubleshoot actually works** | Static analysis crashed on real modules (`.keys()` on the list python-hcl2 returns) and, past that, quoted declared-variable names made every `var.x` reference read as "undefined". Fixed via a shared `iter_labelled_blocks()` normaliser in `hcl_tools.py`; provider version-constraint checks also un-quote values so they actually fire. Troubleshoot's native `/api/chat` LLM call was also missing both the proxy guard and the thinking switch |
| **💬 Repo Chat context sized to fit** | The query context (pretty-printed module summary + up to 8 code chunks) was unbounded — 15–25K chars into a 4096-token window meant multi-minute CPU prefill plus silent front-truncation. Now compact JSON, capped to the probed runtime budget, keeping the most relevant chunks. Also fixed pydantic_ai 1.x `result.data` deprecation |
| **📚 Smarter not-covered detection** | Grounded synthesis sometimes wrote a paragraph *about* the material not covering a topic instead of the exact sentinel — those sections now correctly route to the labelled general-knowledge tier instead of shipping meta-commentary |
| **🔌 APIs/roles never say "None documented"** | When the crawled material doesn't name the service's `*.googleapis.com` APIs or `roles/*` IAM roles (landing/pricing pages rarely do), a second pass fills them from model knowledge — every value still pattern-validated before use |

---

## v2.7.2

| Change | Description |
|---|---|
| **🧠 Disable thinking mode** (`llm.disable_thinking`, default on) | For thinking models (qwen3 family, deepseek-r1): skip the reasoning trace on every LLM call. Beyond minutes of latency per call on CPU, reasoning traces were routed to `message.reasoning` and regularly exhausted `max_tokens` — so `message.content` came back **empty**. Verified: 16s/`'OK'` with the switch vs 60s+/empty without. Toggle in the Settings tab |
| **⬇ Download documents** | New button in the Docs tab: save each previewed document as a styled standalone `.html` file — the offline alternative to Confluence publishing. No credentials, no second pipeline run |
| **🔗 Terraform provider references** | Generated documents now include a "Terraform Provider References" section linking every resource *and data source* to its registry.terraform.io docs page and the provider's GitHub source doc. Data sources are now extracted from module HCL |
| **🛠 GA validators fixed** | All four GA-workflow validators crashed (`AttributeError`) on every real module due to the python-hcl2 list shape; normalised and functionally tested |
| **🏷 Extractor label quoting fixed** | Docgen metadata keys were literally `'"project_id"'` (quotes included); labels and version values now cleaned at source |

---

## v2.7.1

Found and fixed while validating v2.7 end-to-end against real cloned repos and a live Ollama instance:

| Fix | Description |
|---|---|
| **🔁 Indexing survives the dev-server reloader** | `uvicorn --reload` was watching the entire project tree, including `data/` — every chunk written during indexing triggered a full server restart, silently killing the indexing job. Reload is now scoped to `backend/` only |
| **🚀 GA Workflow no longer crashes** | `python-hcl2`'s string-mode parser wraps `variable`/`resource`/`output`/`required_providers` blocks in list-wrapped, quote-labeled dicts; four functions in `hcl_tools.py` assumed plain dicts and threw `AttributeError`/`TypeError` on every repo. All four fixed |
| **💬 Repo Chat no longer crashes on an empty collection** | Querying a tag that's mid-indexing (or was interrupted) hit `ChromaDB`'s `n_results=0` rejection as an unhandled `TypeError`. Now returns no results gracefully instead of crashing the request |
| **🔗 Docgen never fabricates a documentation URL** | Typing an acronym or product name outside the small hardcoded slug map fell back to a naive guess (e.g. "Cloud NAT" → `/cloud-nat/docs`, a real 404) that was presented as an "official documentation reference" regardless of whether it existed. Guessed URLs are now verified (`HEAD`/`GET`) before ever being shown; unverifiable guesses are omitted rather than fabricated. Also filters out locale-duplicate and off-topic crawl results |

See [README §17 Troubleshooting](README.md#17-troubleshooting) for the two most common remaining symptoms (indexing progress and LLM response speed) and how to diagnose them.

---

## v2.7

| Feature | Description |
|---------|-------------|
| **📊 Diagram engine replacement** | Architecture diagrams are now parsed from the module's real HCL (`hcl_graph.py`) into Mermaid source, rendered deterministically via Kroki/mmdc, and carry a `publishable` flag — Confluence publish is **blocked** if the diagram didn't render, never silently swapped for a placeholder. Curated modules also get the Mermaid diagram auto-embedded in their README (idempotent, GitHub-native) |
| **✅ Doc verification guards** | Every generated document is checked post-render for structure conformance (every template section present, in order) and content preservation (no dropped or invented resource/input/output/API facts) before it's allowed to publish |
| **⚠️ Escalation as a first-class outcome** | The repair loop now returns a typed `CurationOutcome` — `candidate_ready`, `escalated`, or `failed` — instead of silently shipping whatever survived the round cap. Escalated output is written to a `-ESCALATED` folder and the UI shows a banner with the outstanding issues instead of a false "success" screen |
| **🌐 Bounded documentation crawler** | Google/Terraform-registry research now follows sublinks (domain-allowlisted, depth ≤2, content-hash deduped, disk-cached) instead of fetching one fixed page — broader grounding material without an unbounded crawl |
| **🛡️ Policy-as-code gate (checkov)** | A new validator layer runs checkov after `validate`/`tflint` pass, with org-approved suppressions configurable via `terrascope.config.yaml`'s `policy.skip_checks` |
| **🧪 `terraform test` gate** | Curated modules now include a deterministically generated `tests/defaults.tftest.hcl` smoke test (mocked provider, `command = plan`, output assertions), executed as the final validation layer |
| **🧹 Proxy-safety hygiene** | New `backend/http_clients.py` (`local_client()` / `external_client()`) makes proxy trust explicit per destination — fixes a real bug where Ollama calls weren't proxy-bypassed like the rest of the local-LLM traffic |

---

## v2.6

| Feature | Description |
|---------|-------------|
| **📐 Schema-grounded generation** | Before Pass A, TerraScope plans the resource types the module needs and validates them against the real provider schema — hallucinated resource types are dropped, and a compact "authoritative schema" block (required args + types, optional arg names, nested blocks) is injected so the model generates against the actual schema instead of guessing |
| **🧩 Deterministic modular layout** | The LLM emits one HCL body; a brace-depth-aware splitter routes every top-level block to its canonical file (`variables.tf`, `outputs.tf`, `locals.tf`, `data.tf`, `main.tf`, `versions.tf`, `providers.tf`), then `terraform fmt` canonicalises it — the modular structure no longer depends on the model getting file placement right |
| **🔁 Validate→repair loop** | After generation, `terraform validate`/schema/tflint errors are fed back to the model with only the offending files, regenerated, re-split, and re-validated — repeating (up to 3 rounds) until the module passes or stops improving |
| **🧹 tflint validation (Layer 5)** | A new tflint layer catches deprecated arguments, invalid enum values, and provider-rule violations that `terraform validate` misses, via a shipped provider-aware `.tflint.hcl` (google/aws/azurerm rulesets) |

---

## v2.5

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

## v2.4

| Feature | Description |
|---------|-------------|
| **📄 Confluence Docgen** | After curation, automatically publish HLD, CPSD, Architectural Design, and Highly Confidential Assessment pages to Confluence — cloned from a reference `.docx` template, fields bound from real module metadata, idempotent re-runs update in place |
| **🔑 `.env` for secrets** | Confluence credentials live in `.env` (separate from `terrascope.config.yaml`); Basic or Bearer auth auto-detected |
| **🖼️ Diagram resolution** | Auto-generates architecture diagrams via `terraform graph | dot`; falls back to folder-supplied images or labelled SVG placeholders |
| **🏷️ Idempotency** | `data/docgen_manifest.json` tracks page IDs per product — re-running updates existing pages instead of creating duplicates; pages are also labelled `terrascope:<product>` |
| **🧪 Dry-run mode** | `--dry-run` writes the would-be Confluence XHTML to `output/docgen_dry_run/` for review without any API calls |
| **🖥️ CLI** | `python -m backend.module_curator.docgen.pipeline --product bigquery --module-path output/... --dry-run` |

---

## v2.3

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

## v2.0

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
