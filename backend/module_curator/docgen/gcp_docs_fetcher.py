"""
gcp_docs_fetcher.py — Deep-research fetcher for PRE-CURATION design documents.

Researches a GCP product using Google's official documentation BEFORE any
Terraform module exists. Module metadata is optional enrichment, never
required. Produces a GCPDocsBundle with detailed, grounded research sections.

Sources fetched per product (best-effort, each optional):
  • cloud.google.com/<slug>/docs            — main docs
  • cloud.google.com/<slug>/docs/overview   — concepts
  • cloud.google.com/<slug>/docs/quotas     — limits
  • cloud.google.com/<slug>/pricing         — cost model
  • Terraform registry provider docs        — resource-level reference
  • terrascope.config.yaml gcp_products     — APIs / IAM roles (offline)

Synthesis: one grounded LLM call PER section (temperature 0.0). Each call
is instructed to answer ONLY from the fetched material and to write
"Not covered in referenced documentation" when the material lacks the info.

Proxy handling — two directions, deliberately different:
  • External web fetches keep httpx default trust_env=True — cloud.google.com
    legitimately needs the corporate proxy.
  • The Ollama call uses trust_env=False — local LLM traffic must NOT be
    intercepted by HTTP_PROXY / HTTPS_PROXY.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from backend.config import get_config
from backend.registry_fetcher.registry_api import (
    fetch_service_docs,
    is_network_available,
)


# ── Product slug map (GCP service name → cloud.google.com docs path) ───────────
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
    "filestore":       "filestore",
}

_PAGE_CAP = 15000          # chars of text kept per fetched page


@dataclass
class GCPDocsBundle:
    """Deep-research material for one product."""
    product_name:            str
    provider:                str = "google"
    # Research sections (LLM-synthesised, grounded)
    overview:                str = ""
    architecture_notes:      str = ""
    key_features:            list[str] = field(default_factory=list)
    use_cases:               list[str] = field(default_factory=list)
    terraform_design_considerations: list[str] = field(default_factory=list)
    iam_design:              list[str] = field(default_factory=list)
    security_considerations: list[str] = field(default_factory=list)
    limits_and_quotas:       list[str] = field(default_factory=list)
    cost_notes:              str = ""
    open_questions:          list[str] = field(default_factory=list)
    # Facts
    apis_required:           list[str] = field(default_factory=list)
    common_roles:            list[str] = field(default_factory=list)
    official_doc_urls:       list[str] = field(default_factory=list)
    raw_excerpt:             str = ""
    sources_used:            list[str] = field(default_factory=list)


# ── Section synthesis specs: (bundle attr, kind, instruction) ──────────────────
_SECTION_SPECS: list[tuple[str, str, str]] = [
    ("overview", "text",
     "Write 2-3 detailed paragraphs: what the service is, how it works, and "
     "when to choose it over alternative GCP services."),
    ("architecture_notes", "text",
     "Write a detailed paragraph on how this service fits into a GCP "
     "architecture: networking and VPC/Private Service Connect connectivity, "
     "regional vs zonal behaviour, and HA/DR characteristics."),
    ("key_features", "list",
     "List 6-10 key features. Each item must be one full sentence with "
     "specifics (tiers, protocols, integration points), not a bare phrase."),
    ("use_cases", "list",
     "List 4-6 concrete use cases / scenarios where this service is the "
     "right choice."),
    ("terraform_design_considerations", "list",
     "List what a future Terraform module for this service SHOULD cover: the "
     "provider resources available, key arguments and their implications, "
     "recommended variables to expose, sensible defaults, and immutable "
     "fields that force resource replacement. Ground this in the Terraform "
     "registry material where present."),
    ("iam_design", "list",
     "List recommended least-privilege IAM design: specific roles/* roles "
     "and which principal type (admins, service accounts, consumers) should "
     "hold each."),
    ("security_considerations", "list",
     "List 5-8 security considerations: encryption at rest and in transit, "
     "CMEK support, VPC Service Controls, audit logging, and relevant "
     "organisation-policy constraints."),
    ("limits_and_quotas", "list",
     "List the documented limits and quotas most relevant to design "
     "decisions (capacity ranges, per-project caps, performance ceilings)."),
    ("cost_notes", "text",
     "Summarise the pricing model and the main cost drivers in one "
     "paragraph."),
    ("open_questions", "list",
     "List 3-5 open questions a module-curation session should answer for "
     "this service (sizing, tiers, networking choices, retention, etc.)."),
]


# ── Public entry point ─────────────────────────────────────────────────────────

async def fetch_gcp_docs(
    product_name: str,
    provider: str = "google",
    *,
    use_llm: bool = True,
) -> GCPDocsBundle:
    """Deep-research *product_name*. Always returns a bundle (never raises)."""
    bundle = GCPDocsBundle(product_name=product_name, provider=provider)

    # 1. Config metadata — offline, always available
    _load_config_metadata(product_name, bundle)

    # 2. Deep fetch of official pages + Terraform registry
    raw_parts: list[str] = []
    if is_network_available():
        for label, url in _research_urls(product_name):
            text = await _fetch_page(url)
            if text:
                raw_parts.append(f"===== SOURCE: {label} ({url}) =====\n{text}")
                bundle.official_doc_urls.append(url)
                bundle.sources_used.append(label)
                print(f"[docgen-research] fetched {label}: {len(text)} chars")
            else:
                print(f"[docgen-research] no content from {label}")

        try:
            tf_docs = await fetch_service_docs(provider, product_name)
            if tf_docs and not tf_docs.lower().startswith("no documentation"):
                raw_parts.append(f"===== SOURCE: terraform-registry =====\n{tf_docs[:_PAGE_CAP]}")
                bundle.sources_used.append("terraform-registry")
        except Exception as e:
            print(f"[docgen-research] terraform registry fetch failed: {e}")

    # Always include the canonical doc URL even if every fetch failed
    canonical = _canonical_doc_url(product_name)
    if canonical not in bundle.official_doc_urls:
        bundle.official_doc_urls.append(canonical)

    raw_material = "\n\n".join(raw_parts).strip()
    bundle.raw_excerpt = raw_material[:4000]

    # 3. Per-section grounded synthesis
    if use_llm and raw_material:
        await _synthesise_sections(bundle, raw_material)
        if not bundle.apis_required or not bundle.common_roles:
            await _extract_apis_and_roles(bundle, raw_material)

    _fallback_sections(bundle)
    return bundle


# ── Source 1: config metadata ──────────────────────────────────────────────────

def _load_config_metadata(product_name: str, bundle: GCPDocsBundle) -> None:
    try:
        cfg = get_config()
    except Exception:
        return
    key = _normalise(product_name)
    for prod_key, meta in cfg.gcp_products.items():
        nk = _normalise(prod_key)
        if nk == key or key in nk or nk in key:
            bundle.apis_required = list(meta.apis)
            bundle.common_roles = list(meta.common_roles)
            bundle.sources_used.append("config-metadata")
            return


# ── Deep page fetching ──────────────────────────────────────────────────────────

def _slug(product_name: str) -> str:
    s = _GCP_DOC_SLUGS.get(_normalise(product_name))
    if not s:
        s = re.sub(r"[^a-z0-9]+", "-", _normalise(product_name)).strip("-")
    return s


def _canonical_doc_url(product_name: str) -> str:
    s = _slug(product_name)
    return f"https://cloud.google.com/{s}" if "/" in s else f"https://cloud.google.com/{s}/docs"


def _research_urls(product_name: str) -> list[tuple[str, str]]:
    s = _slug(product_name)
    if "/" in s:               # slug already a deep path (e.g. memorystore/docs/redis)
        base = f"https://cloud.google.com/{s}"
        return [("gcp-docs", base)]
    root = f"https://cloud.google.com/{s}"
    return [
        ("gcp-docs",     f"{root}/docs"),
        ("gcp-overview", f"{root}/docs/overview"),
        ("gcp-quotas",   f"{root}/docs/quotas"),
        ("gcp-pricing",  f"{root}/pricing"),
    ]


async def _fetch_page(url: str) -> str:
    """Fetch one page → cleaned text (trust_env default ON for corporate proxy)."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return ""
        return _html_to_text(r.text)
    except Exception:
        return ""


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body
        text = main.get_text(separator="\n", strip=True) if main else ""
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
    return text[:_PAGE_CAP]


# ── LLM synthesis (grounded, per section) ───────────────────────────────────────

def _make_llm_client():
    """OpenAI-compatible client for local Ollama — proxy bypass + long timeout."""
    from openai import AsyncOpenAI
    cfg = get_config()
    base_url = cfg.llm.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return AsyncOpenAI(
        base_url=base_url,
        api_key="ollama",
        max_retries=0,  # retrying a slow local model just resends the same big prompt
        http_client=httpx.AsyncClient(
            trust_env=False,                     # never proxy local Ollama traffic
            timeout=httpx.Timeout(300.0),
        ),
    ), cfg


async def _ask(client, cfg, prompt: str) -> str:
    resp = await client.chat.completions.create(
        model=cfg.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=cfg.llm.max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


async def _synthesise_sections(bundle: GCPDocsBundle, raw_material: str) -> None:
    """One grounded LLM call per research section."""
    try:
        client, cfg = _make_llm_client()
    except Exception as e:
        print(f"[docgen-research] LLM client unavailable: {e}")
        return

    grounding = (
        "You are a senior cloud architect writing a pre-curation research "
        f"document for the Google Cloud product '{bundle.product_name}'. "
        "Answer ONLY from the reference material below. Do NOT invent "
        "capabilities. If the material does not cover the request, reply "
        "exactly: Not covered in referenced documentation.\n\n"
        "=== REFERENCE MATERIAL ===\n"
        f"{raw_material[:24000]}\n"
        "=== END MATERIAL ===\n\n"
    )

    try:
        for attr, kind, instruction in _SECTION_SPECS:
            if kind == "list":
                fmt = ("Return ONLY a plain list, one item per line, each line "
                       "starting with '- '. No headers, no prose around it.")
            else:
                fmt = "Return ONLY the prose paragraphs. No headers, no preamble."
            try:
                answer = await _ask(client, cfg, grounding + instruction + "\n" + fmt)
                if not answer or "not covered in referenced documentation" in answer.lower():
                    print(f"[docgen-research] section '{attr}': not covered by material")
                    continue
                if kind == "list":
                    items = [re.sub(r"^[-*•]\s*", "", ln).strip()
                             for ln in answer.splitlines() if ln.strip()]
                    items = [i for i in items if len(i) > 3]
                    if items:
                        setattr(bundle, attr, items)
                else:
                    setattr(bundle, attr, answer)
            except Exception as e:
                print(f"[docgen-research] section '{attr}' failed: {e}")
    finally:
        await client.close()


async def _extract_apis_and_roles(bundle: GCPDocsBundle, raw_material: str) -> None:
    """For products missing from config: extract APIs and IAM roles via LLM."""
    client = None
    try:
        client, cfg = _make_llm_client()
        prompt = (
            f"From the reference material about Google Cloud '{bundle.product_name}' "
            "below, extract:\n"
            "APIS: the *.googleapis.com API endpoints that must be enabled\n"
            "ROLES: common predefined roles/* IAM roles for this service\n"
            "Return exactly two lines:\n"
            "APIS: api1.googleapis.com, api2.googleapis.com\n"
            "ROLES: roles/x.admin, roles/x.viewer\n"
            "If unknown from the material, write 'unknown' after the colon.\n\n"
            f"=== MATERIAL ===\n{raw_material[:12000]}\n=== END ==="
        )
        answer = await _ask(client, cfg, prompt)
        for line in answer.splitlines():
            low = line.lower().strip()
            if low.startswith("apis:") and "unknown" not in low and not bundle.apis_required:
                bundle.apis_required = [a.strip() for a in line.split(":", 1)[1].split(",")
                                        if "googleapis.com" in a]
            elif low.startswith("roles:") and "unknown" not in low and not bundle.common_roles:
                bundle.common_roles = [r.strip() for r in line.split(":", 1)[1].split(",")
                                       if r.strip().startswith("roles/")]
    except Exception as e:
        print(f"[docgen-research] API/role extraction failed: {e}")
    finally:
        if client is not None:
            await client.close()


# ── Deterministic fallbacks (doc must never be empty) ──────────────────────────

def _fallback_sections(bundle: GCPDocsBundle) -> None:
    if not bundle.overview:
        bundle.overview = (
            f"{bundle.product_name.title()} is a Google Cloud Platform service. "
            "Detailed overview could not be synthesised — start Ollama and "
            "re-generate, or refer to the linked official documentation."
        )
    if not bundle.key_features and bundle.apis_required:
        bundle.key_features = [f"Enables the {api} API" for api in bundle.apis_required]
    if not bundle.security_considerations and bundle.common_roles:
        bundle.security_considerations = [
            f"Grant least-privilege access using IAM role {role}"
            for role in bundle.common_roles
        ]
    if not bundle.open_questions:
        bundle.open_questions = [
            "Which capacity tier / sizing does the workload need?",
            "Which network (VPC, subnets, PSC) should the service attach to?",
            "What encryption requirements apply (Google-managed vs CMEK)?",
        ]


def _normalise(s: str) -> str:
    return s.lower().strip()
