#!/usr/bin/env bash
# push_and_pr.sh — Run this ONCE from your local machine to push the
# feature/docgen-enhancement-v2.5 branch and open a GitHub PR.
#
# Usage:
#   chmod +x push_and_pr.sh
#   GITHUB_TOKEN=ghp_YOUR_PAT ./push_and_pr.sh

set -e

if [ -z "$GITHUB_TOKEN" ]; then
  echo "ERROR: set GITHUB_TOKEN=ghp_... before running"
  exit 1
fi

OWNER="madhurmishra9"
REPO="terra_scope"
BRANCH="feature/docgen-enhancement-v2.5"
BASE="master"

echo "==> Pushing branch..."
git push "https://${GITHUB_TOKEN}@github.com/${OWNER}/${REPO}.git" "${BRANCH}"

echo "==> Creating Pull Request..."
PR=$(curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/${OWNER}/${REPO}/pulls" \
  -d @- << JSON
{
  "title": "feat: docgen v2.5 — Google docs enrichment, single-command startup, in-UI settings",
  "head": "${BRANCH}",
  "base": "${BASE}",
  "body": "## Summary\n\nThis PR implements the full docgen v2.5 enhancement plan. See the [DOCGEN_ENHANCEMENT_PLAN.md] committed in a prior session for the architecture overview.\n\n## What's Changed\n\n### 📄 Docs tab — two-phase doc generation\n- New `📄 Docs` tab in the UI — works for any product, curated or not\n- Phase 1 **Preview**: fetches Google official docs, fills template, renders HTML locally for verification\n- Phase 2 **Publish**: sends to Confluence and returns clickable page URLs\n- Works without a curation session — just enter a product name\n\n### 🌐 Google Docs Enrichment\n- New \`gcp_docs_fetcher.py\`: pulls product overview, key features, security notes from \`cloud.google.com\` + Terraform registry\n- LLM-synthesised sections at temperature 0.0 (grounded, no hallucination)\n- Deterministic fallback when offline\n\n### ⚡ Single-command startup\n- \`npm run dev\` in \`frontend/\` now **auto-starts the FastAPI backend** via a Vite plugin\n- Detects \`.venv\` on Windows/Unix; falls back to system Python\n- Set \`TERRASCOPE_NO_BACKEND=1\` to skip (e.g. running backend in a debugger)\n\n### ⚙ Settings tab\n- New \`⚙ Settings\` tab — edit LLM model/URL, Confluence credentials, grounding thresholds, server config\n- Confluence settings saved to \`.env\` (never to the YAML); takes effect immediately\n- **Test Connections** button for Ollama and Confluence\n- Any panel that needs missing config shows a direct link to Settings\n\n### 🐛 Bug fixes\n- Input/output tables were rendering empty (Pydantic model not treated as dict)\n- Tables in rendered Confluence output now use real \`<table>\` HTML instead of pipe-separated text\n- HCL quote/interpolation artefacts (\`\"var_name\"\`, \`\${string}\`) stripped from all rendered values\n\n### Docs\n- README updated to v2.5: new What's New section, single-command startup guide, Docs tab guide, complete doc-gen rewrite\n\n## Testing\n- \`py_compile\` clean on all modified backend modules\n- \`esbuild\` bundles \`App.jsx\` with no errors\n- End-to-end dry-run: 4 doc types, un-curated product, all \`status=ok\`\n- Tables populate correctly with type/description when module path supplied\n\n## Breaking changes\nNone — all existing API routes preserved. New routes are purely additive."
}
JSON
)

URL=$(echo "$PR" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_url', d))" 2>/dev/null || echo "$PR")
echo ""
echo "==> Pull Request: $URL"
