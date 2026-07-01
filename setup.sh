#!/usr/bin/env bash
#
# setup.sh — One script to set up, configure, and run TerraScope.
#
# It handles everything from a fresh clone:
#   1. Verifies prerequisites (python, node/npm, git; ollama optional)
#   2. Creates the Python virtualenv (.venv) and installs requirements.txt
#   3. Installs the frontend npm dependencies
#   4. Pulls the Ollama models named in terrascope.config.yaml (best-effort)
#   5. Creates a .env from a template if one doesn't exist
#   6. Launches the app (`npm run dev`, which auto-starts the FastAPI backend)
#
# Works on Git Bash (Windows), macOS, and Linux.
#
# Usage:
#   ./setup.sh                 # full setup, then run
#   ./setup.sh --no-run        # set up only, don't launch
#   ./setup.sh --skip-models   # don't pull Ollama models
#   ./setup.sh --skip-frontend # skip `npm install`
#   ./setup.sh --run-only      # skip setup, just launch the app
#   ./setup.sh -h | --help
#
set -euo pipefail

# ── Resolve project root (dir of this script) ────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── Flags ────────────────────────────────────────────────────────────────────
RUN=1; SKIP_MODELS=0; SKIP_FRONTEND=0; RUN_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-run)        RUN=0 ;;
    --skip-models)   SKIP_MODELS=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --run-only)      RUN_ONLY=1 ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

# ── Pretty logging ───────────────────────────────────────────────────────────
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── Platform-aware paths ─────────────────────────────────────────────────────
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;;
  *)                    IS_WIN=0 ;;
esac
if [ "$IS_WIN" = 1 ]; then
  VENV_PY="$ROOT/.venv/Scripts/python.exe"
else
  VENV_PY="$ROOT/.venv/bin/python"
fi

# Pick a base python interpreter for creating the venv.
pick_python() {
  for c in python3 python py; do
    if have "$c"; then echo "$c"; return 0; fi
  done
  return 1
}

if [ "$RUN_ONLY" = 1 ]; then
  say "Run-only mode — skipping setup"
else
  # ── 1. Prerequisites ───────────────────────────────────────────────────────
  say "Checking prerequisites"
  BASE_PY="$(pick_python)" || die "Python 3.11+ not found on PATH. Install from python.org."
  ok "python: $("$BASE_PY" --version 2>&1)"
  have node || die "Node.js 18+ not found on PATH. Install from nodejs.org."
  ok "node: $(node --version)"
  have npm  || die "npm not found on PATH (ships with Node.js)."
  ok "npm: $(npm --version)"
  have git  && ok "git: $(git --version)" || warn "git not found — repo cloning features will be limited."
  if have ollama; then ok "ollama: present"; else warn "ollama not found — install from ollama.com; model pull will be skipped."; SKIP_MODELS=1; fi

  # ── 2. Python venv + deps ──────────────────────────────────────────────────
  say "Python virtual environment"
  if [ ! -x "$VENV_PY" ]; then
    "$BASE_PY" -m venv .venv || die "Failed to create .venv"
    ok "created .venv"
  else
    ok ".venv already exists"
  fi
  [ -x "$VENV_PY" ] || die "venv python missing at $VENV_PY"
  say "Installing Python dependencies (requirements.txt)"
  "$VENV_PY" -m pip install --upgrade pip >/dev/null
  "$VENV_PY" -m pip install -r requirements.txt
  ok "backend dependencies installed"

  # ── 3. Frontend deps ───────────────────────────────────────────────────────
  if [ "$SKIP_FRONTEND" = 0 ]; then
    say "Installing frontend dependencies (npm install)"
    ( cd frontend && npm install )
    ok "frontend dependencies installed"
  else
    warn "Skipping frontend install (--skip-frontend)"
  fi

  # ── 4. Ollama models (from terrascope.config.yaml) ─────────────────────────
  if [ "$SKIP_MODELS" = 0 ]; then
    say "Pulling Ollama models"
    # Read model + embedding_model from the YAML without needing a YAML lib.
    MODEL="$(grep -E '^\s*model:' terrascope.config.yaml | head -1 | sed -E 's/.*model:\s*//; s/["\x27]//g' | tr -d '[:space:]')"
    EMB="$(grep -E '^\s*embedding_model:' terrascope.config.yaml | head -1 | sed -E 's/.*embedding_model:\s*//; s/["\x27]//g' | tr -d '[:space:]')"
    MODEL="${MODEL:-qwen2.5-coder:7b}"
    EMB="${EMB:-nomic-embed-text}"
    for m in "$MODEL" "$EMB"; do
      if ollama list 2>/dev/null | grep -q "${m%%:*}"; then
        ok "$m already pulled"
      else
        echo "    pulling $m ..."
        ollama pull "$m" || warn "Could not pull $m (is the Ollama service running?)"
      fi
    done
  else
    warn "Skipping Ollama model pull"
  fi

  # ── 5. .env template ───────────────────────────────────────────────────────
  say "Configuration (.env)"
  if [ -f .env ]; then
    ok ".env already exists — leaving it untouched"
  else
    cat > .env <<'ENVEOF'
# TerraScope — local secrets & overrides (git-ignored).
# Confluence Docgen credentials (optional — leave blank to disable publishing).
CONFLUENCE_BASE_URL=
CONFLUENCE_SPACE_KEY=
# Basic auth: set EMAIL + API_TOKEN.  Bearer auth: set only CONFLUENCE_API_TOKEN.
CONFLUENCE_EMAIL=
CONFLUENCE_API_TOKEN=

# Optional LLM overrides (defaults come from terrascope.config.yaml):
# OLLAMA_BASE_URL=http://localhost:11434
ENVEOF
    ok "created .env template — fill in Confluence values if you use Docgen"
  fi

  # ── Optional tooling notice (used by the accuracy pipeline) ────────────────
  say "Optional Terraform tooling (for validate/repair + tflint layers)"
  have terraform && ok "terraform: $(terraform version | head -1)" || warn "terraform not found — generation still works; the validate/fmt layers are skipped."
  have tflint    && ok "tflint: $(tflint --version | head -1)"     || warn "tflint not found — the tflint validation layer is skipped."

  ok "Setup complete."
fi

# ── 6. Run ───────────────────────────────────────────────────────────────────
if [ "$RUN" = 1 ]; then
  [ -x "$VENV_PY" ] || die "No .venv found — run setup first (omit --run-only)."
  say "Starting TerraScope (frontend + auto-started backend)"
  echo "    UI:      http://localhost:5173"
  echo "    Backend: http://localhost:8000"
  echo "    (Ctrl+C to stop; the Vite plugin manages the backend process.)"
  cd frontend
  exec npm run dev
else
  say "Done. To start the app later:"
  echo "    cd frontend && npm run dev        # or: ./setup.sh --run-only"
fi
