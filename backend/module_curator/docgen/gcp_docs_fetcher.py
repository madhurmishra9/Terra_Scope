"""
gcp_docs_fetcher.py — Fetch Google Cloud official documentation and synthesise
a structured, product-specific narrative for documentation generation.

Why this exists
---------------
The original docgen pipeline filled templates using ONLY the Terraform module's
own metadata (variables, outputs, resource types). That produces an accurate but
dry, code-centric document. For a real product page we also want:

  • a plain-English product overview
  • the product's key features / capabilities
  • security & compliance considerations
  • the canonical Google Cloud documentation URL(s)

This module gathers raw source material from three places and then asks the local
LLM to synthesise the narrative sections — strictly grounded in what was fetched
(temperature 0.0), so it does not hallucinate features the product doesn't have.

Sources (best-effort, each is optional and degrades gracefully)
---------------------------------------------------------------
  1. Config metadata          — apis, common_roles, provider_resources from
                                 terrascope.config.yaml (always available offline)
  2. Terraform registry docs  — via registry_fetcher.fetch_service_docs (cached)
  3. Google Cloud docs page   — best-effort fetch of cloud.google.com/<slug>/docs

If the network is unavailable, sources 2 and 3 are skipped and the synthesis
runs on config metadata + whatever the Terraform module itself exposed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from backend.config import get_config
from backend.registry_fetcher.registry_api import (
    fetch_service_docs,
    is_network_available,
)


# ── Product slug map (GCP service name → cloud.google.com docs path) ───────────
# Only the common ones; unknown products fall back to a slugified guess.
_GCP_DOC_SLUGS: dict[str, str] = {
    "bigquery":        "bigquery",
    "storage":         "storage",
    "cloud storage":   "storage",
    "gcs":             "storage",
    "dataflow":        "dataflow",
    "pubsub":          "pubsub",
    "pub/sub":         "pubsub",
    "cloud sql":       "sql",
    "cloudsql":        "sql",
    "spanner":         "spanner",
    "bigtable":        "bigtable",
    "dataproc":        "dataproc",
    "composer":        "composer",
    "cloud run":       "run",
    "gke":             "kubernetes-engine",
    "kubernetes engine": "kubernetes-engine",
    "cloud functions": "functions",
    "memorystore":     "memorystore",
    "redis":           "memorystore/docs/redis",
    "vertex ai":       "vertex-ai",
    "artifact registry": "artifact-registry",
    "secret manager":  "secret-manager",
    "alloydb":         "alloydb",
    "datastream":      "datastream",
    "firestore":       "firestore",
}


@dataclass
class GCPDocsBundle:
    """Raw + synthesised documentation material for one product."""
    product_name:            str
    provider:                str = "google"
    overview:                str = ""
    key_features:            list[str] = field(default_factory=list)
    security_considerations: list[str] = field(default_factory=list)
    apis_required:           list[str] = field(default_factory=list)
    common_roles:            list[str] = field(default_factory=list)
    official_doc_urls:       list[str] = field(default_factory=list)
    raw_excerpt:             str = ""           # truncated raw material (for audit)
    sources_used:            list[str] = field(default_factory=list)


# ── Public entry point ─────────────────────────────────────────────────────────

async def fetch_gcp_docs(
    product_name: str,
    provider: str = "google",
    *,
    use_llm: bool = True,
) -> GCPDocsBundle:
    """
    Gather and synthesise documentation material for *product_name*.

    Always returns a GCPDocsBundle (never raises) so the pipeline can proceed
    even if every network source fails.
    """
    bundle = GCPDocsBundle(product_name=product_name, provider=provider)

    # 1. Config metadata — offline, always available
    _load_config_metadata(product_name, bundle)

    # 2. + 3. Network sources (best-effort)
    raw_parts: list[str] = []
    online = is_network_available()

    if online:
        try:
            tf_docs = await fetch_service_docs(provider, product_name)
            if tf_docs and not tf_docs.lower().startswith("no documentation"):
                raw_parts.append(tf_docs)
                bundle.sources_used.append("terraform-registry")
        except Exception:
            pass

        gcp_url, gcp_text = await _fetch_gcp_overview(product_name)
        if gcp_url:
            bundle.official_doc_urls.insert(0, gcp_url)
            bundle.sources_used.append("cloud.google.com")
        if gcp_text:
            raw_parts.append(gcp_text)

    # Always include the canonical guessed doc URL even if the fetch failed
    canonical = _canonical_doc_url(product_name)
    if canonical and canonical not in bundle.official_doc_urls:
        bundle.official_doc_urls.append(canonical)

    raw_material = "\n\n".join(raw_parts).strip()
    bundle.raw_excerpt = raw_material[:4000]

    # 4. Synthesise narrative sections with the LLM (grounded in raw_material)
    if use_llm and raw_material:
        await _synthesise_sections(bundle, raw_material)
    else:
        # Deterministic fallback when no LLM / no raw material
        _fallback_sections(bundle)

    return bundle


# ── Source 1: config metadata ──────────────────────────────────────────────────

def _load_config_metadata(product_name: str, bundle: GCPDocsBundle) -> None:
    try:
        cfg = get_config()
    except Exception:
        return
    key = _normalise(product_name)
    meta = None
    # Direct key match, then substring match against gcp_products keys
    for prod_key, prod_meta in cfg.gcp_products.items():
        if _normalise(prod_key) == key or key in _normalise(prod_key) or _normalise(prod_key) in key:
            meta = prod_meta
            break
    if meta:
        bundle.apis_required = list(meta.apis)
        bundle.common_roles = list(meta.common_roles)
        if meta.provider_resources:
            bundle.sources_used.append("config-metadata")


# ── Source 3: Google Cloud overview page ────────────────────────────────────────

def _canonical_doc_url(product_name: str) -> str:
    slug = _GCP_DOC_SLUGS.get(_normalise(product_name))
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", _normalise(product_name)).strip("-")
    # If slug already contains a path, use as-is, else append /docs
    if "/" in slug:
        return f"https://cloud.google.com/{slug}"
    return f"https://cloud.google.com/{slug}/docs"


async def _fetch_gcp_overview(product_name: str) -> tuple[str, str]:
    """Best-effort fetch of the GCP docs overview page. Returns (url, text)."""
    url = _canonical_doc_url(product_name)
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return url, ""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            main = soup.find("main") or soup.find("article") or soup.body
            text = main.get_text(separator="\n", strip=True) if main else ""
            return url, text[:5000]
        except Exception:
            # bs4/lxml unavailable — strip tags crudely
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text)
            return url, text[:5000]
    except Exception:
        return url, ""


# ── Source 4: LLM synthesis (grounded) ──────────────────────────────────────────

async def _synthesise_sections(bundle: GCPDocsBundle, raw_material: str) -> None:
    """Use the configured LLM to extract overview/features/security, grounded in raw."""
    try:
        from openai import AsyncOpenAI
    except Exception:
        _fallback_sections(bundle)
        return

    try:
        cfg = get_config()
        base_url = cfg.llm.base_url.rstrip("/")
        # Ollama exposes an OpenAI-compatible endpoint at /v1
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        client = AsyncOpenAI(base_url=base_url, api_key="ollama")

        prompt = (
            "You are a senior cloud documentation writer. Using ONLY the reference "
            "material below, produce a JSON object describing the Google Cloud product "
            f"'{bundle.product_name}'. Do NOT invent capabilities not supported by the "
            "material. If the material is thin, keep the sections short and factual.\n\n"
            "Return STRICT JSON with exactly these keys and no prose around it:\n"
            '{\n'
            '  "overview": "<2-4 sentence plain-English product overview>",\n'
            '  "key_features": ["<feature>", ...],            // 3-7 items\n'
            '  "security_considerations": ["<note>", ...]      // 2-5 items\n'
            '}\n\n'
            "=== REFERENCE MATERIAL ===\n"
            f"{raw_material[:6000]}\n"
            "=== END MATERIAL ==="
        )

        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=cfg.llm.max_tokens,
        )
        content = resp.choices[0].message.content or ""
        _apply_llm_json(bundle, content)
    except Exception:
        _fallback_sections(bundle)


def _apply_llm_json(bundle: GCPDocsBundle, content: str) -> None:
    import json
    # Strip markdown fences if present
    text = content.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # Grab the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except Exception:
        _fallback_sections(bundle)
        return
    bundle.overview = str(data.get("overview", "")).strip()
    kf = data.get("key_features", [])
    sc = data.get("security_considerations", [])
    bundle.key_features = [str(x).strip() for x in kf if str(x).strip()] if isinstance(kf, list) else []
    bundle.security_considerations = (
        [str(x).strip() for x in sc if str(x).strip()] if isinstance(sc, list) else []
    )
    if not bundle.overview and not bundle.key_features:
        _fallback_sections(bundle)


def _fallback_sections(bundle: GCPDocsBundle) -> None:
    """Deterministic, no-LLM content so the document is never empty."""
    if not bundle.overview:
        bundle.overview = (
            f"{bundle.product_name.title()} is a Google Cloud Platform service. "
            "Refer to the linked official documentation for a full description."
        )
    if not bundle.key_features and bundle.apis_required:
        bundle.key_features = [f"Enables the {api} API" for api in bundle.apis_required]
    if not bundle.security_considerations and bundle.common_roles:
        bundle.security_considerations = [
            f"Grant least-privilege access using IAM role {role}"
            for role in bundle.common_roles
        ]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return s.lower().strip()
