"""
gcp_service_scanner.py — Scans GCP product release notes and the Google
Discovery API to find NEW GA features for a specific GCP service/product
that are not yet reflected in the repo's Terraform module.

This runs ALONGSIDE the provider GA detection (ga_detector.py) to give
a complete picture:
  - ga_detector:          What did the Terraform PROVIDER add/change?
  - gcp_service_scanner:  What did the GCP SERVICE itself announce as GA
                          that might need new Terraform resources or args?

Sources consulted per product:
  1. Google Cloud Release Notes RSS/JSON feed (cloud.google.com/feeds)
  2. Google API Discovery Service  (discovery.googleapis.com)
  3. LLM synthesis — maps GCP feature announcements to Terraform impact

Entry point:
  scan_gcp_service(repo_name, run) → GCPServiceScanResult
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional
import httpx

from backend.config import get_config
from backend.agent.tools.git_tools import get_latest_tag
from backend.agent.tools.hcl_tools import get_all_resources, summarize_module
from backend.ga_workflow.ga_models import (
    GCPServiceFeatureModel,
    GCPServiceScanResult,
    WorkflowRun,
    WorkflowStage,
)

HTTP_TIMEOUT = 15.0

# ── GCP Release Notes feed per product ────────────────────────────────────────
# cloud.google.com JSON release notes endpoint, filtered by product slug
RELEASE_NOTES_URL = "https://cloud.google.com/feeds/release-notes.json"

# Google API Discovery index — lists every GCP API version
DISCOVERY_INDEX_URL = "https://discovery.googleapis.com/discovery/v1/apis"

# Per-product: Discovery API name + preferred version
GCP_PRODUCT_API_MAP: dict[str, dict] = {
    "bigquery": {
        "api_name": "bigquery",
        "version": "v2",
        "release_notes_product": "bigquery",
        "tf_resource_prefix": "google_bigquery_",
        "docs_url": "https://cloud.google.com/bigquery/docs/release-notes",
    },
    "storage": {
        "api_name": "storage",
        "version": "v1",
        "release_notes_product": "cloud-storage",
        "tf_resource_prefix": "google_storage_",
        "docs_url": "https://cloud.google.com/storage/docs/release-notes",
    },
    "dataflow": {
        "api_name": "dataflow",
        "version": "v1b3",
        "release_notes_product": "dataflow",
        "tf_resource_prefix": "google_dataflow_",
        "docs_url": "https://cloud.google.com/dataflow/docs/release-notes",
    },
    "pubsub": {
        "api_name": "pubsub",
        "version": "v1",
        "release_notes_product": "pubsub",
        "tf_resource_prefix": "google_pubsub_",
        "docs_url": "https://cloud.google.com/pubsub/docs/release-notes",
    },
    "dataproc": {
        "api_name": "dataproc",
        "version": "v1",
        "release_notes_product": "dataproc",
        "tf_resource_prefix": "google_dataproc_",
        "docs_url": "https://cloud.google.com/dataproc/docs/release-notes",
    },
    "composer": {
        "api_name": "composer",
        "version": "v1",
        "release_notes_product": "cloud-composer",
        "tf_resource_prefix": "google_composer_",
        "docs_url": "https://cloud.google.com/composer/docs/release-notes",
    },
    "spanner": {
        "api_name": "spanner",
        "version": "v1",
        "release_notes_product": "spanner",
        "tf_resource_prefix": "google_spanner_",
        "docs_url": "https://cloud.google.com/spanner/docs/release-notes",
    },
    "bigtable": {
        "api_name": "bigtable",
        "version": "v2",
        "release_notes_product": "bigtable",
        "tf_resource_prefix": "google_bigtable_",
        "docs_url": "https://cloud.google.com/bigtable/docs/release-notes",
    },
    "dataplex": {
        "api_name": "dataplex",
        "version": "v1",
        "release_notes_product": "dataplex",
        "tf_resource_prefix": "google_dataplex_",
        "docs_url": "https://cloud.google.com/dataplex/docs/release-notes",
    },
    "vertex_ai": {
        "api_name": "aiplatform",
        "version": "v1",
        "release_notes_product": "vertex-ai",
        "tf_resource_prefix": "google_vertex_ai_",
        "docs_url": "https://cloud.google.com/vertex-ai/docs/release-notes",
    },
    "datastream": {
        "api_name": "datastream",
        "version": "v1",
        "release_notes_product": "datastream",
        "tf_resource_prefix": "google_datastream_",
        "docs_url": "https://cloud.google.com/datastream/docs/release-notes",
    },
    "bigquery_analytics_hub": {
        "api_name": "analyticshub",
        "version": "v1",
        "release_notes_product": "analytics-hub",
        "tf_resource_prefix": "google_bigquery_analytics_hub_",
        "docs_url": "https://cloud.google.com/bigquery/docs/analytics-hub-release-notes",
    },
}

# GA keywords — filter release-note entries to only confirmed GA announcements
GA_SIGNALS = [
    "generally available", " GA ", "is now GA", "graduated to GA",
    "now available", "production-ready", "launched", "release",
]

# Keywords indicating something is NOT GA (still preview/alpha/beta)
NOT_GA_SIGNALS = [
    "preview", "beta", "alpha", "experimental", "pre-GA",
    "public preview", "private preview",
]


# ── Fetch GCP release notes ───────────────────────────────────────────────────

async def fetch_gcp_release_notes(product_slug: str) -> list[dict]:
    """
    Fetch Google Cloud release notes for a specific product.
    Tries the JSON feed first; falls back to a simpler search.
    Returns a list of {title, description, date, url} dicts.
    """
    results: list[dict] = []

    # Primary: cloud.google.com JSON feed filtered to product
    try:
        url = f"https://cloud.google.com/feeds/release-notes.json?product={product_slug}"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                entries = data.get("items") or data.get("entries") or []
                for entry in entries[:60]:   # limit to 60 most recent
                    results.append({
                        "title":       entry.get("title", ""),
                        "description": entry.get("contentSnippet") or entry.get("content", "")[:500],
                        "date":        entry.get("pubDate") or entry.get("published", ""),
                        "url":         entry.get("link") or entry.get("url", ""),
                        "source":      "gcp_release_notes",
                    })
                return results
    except Exception:
        pass

    # Secondary: use the product-specific docs URL hint with a lightweight approach
    # This returns an empty list gracefully — caller will still run LLM analysis
    return results


async def fetch_discovery_api_schema(api_name: str, version: str) -> Optional[dict]:
    """
    Fetch the Google API Discovery document for a service.
    This gives us the full REST API schema — methods, resources, properties —
    which the LLM can compare to what's currently in the Terraform module.
    """
    try:
        url = f"https://discovery.googleapis.com/discovery/v1/apis/{api_name}/{version}/rest"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                schema = resp.json()
                # Return a trimmed version — full schema can be 2+ MB
                return _trim_discovery_schema(schema)
    except Exception:
        pass
    return None


def _trim_discovery_schema(schema: dict) -> dict:
    """
    Return only the parts of the Discovery schema relevant to Terraform:
    resources (top-level methods) and schemas (data shapes/properties).
    Caps total size for LLM context.
    """
    trimmed = {
        "name":           schema.get("name", ""),
        "version":        schema.get("version", ""),
        "description":    schema.get("description", "")[:300],
        "resources":      {},
        "schemas":        {},
    }

    # Top-level resource names and their methods (insert/list/get/patch/delete)
    raw_resources = schema.get("resources", {})
    for res_name, res_body in list(raw_resources.items())[:30]:
        methods = list(res_body.get("methods", {}).keys())
        trimmed["resources"][res_name] = {"methods": methods}

    # Schema property names (field names) — critical for spotting new attributes
    raw_schemas = schema.get("schemas", {})
    for schema_name, schema_body in list(raw_schemas.items())[:40]:
        props = list((schema_body.get("properties") or {}).keys())
        trimmed["schemas"][schema_name] = {"properties": props[:30]}

    return trimmed


# ── LLM analysis of GCP service features ─────────────────────────────────────

async def analyze_gcp_features_with_llm(
    product: str,
    product_info: dict,
    module_summary: dict,
    module_resources: list[str],
    release_notes: list[dict],
    api_schema: Optional[dict],
    run: WorkflowRun,
) -> list[GCPServiceFeatureModel]:
    """
    Send GCP release notes + API schema + module summary to the local LLM.
    Ask it to identify new GA features that:
      (a) the GCP service now supports
      (b) are NOT yet reflected in the Terraform module's .tf code

    Returns structured GCPServiceFeatureModel objects.
    """
    cfg = get_config()
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    notes_text = "\n".join(
        f"- [{e.get('date', '')}] {e.get('title', '')}: {e.get('description', '')[:200]}"
        for e in release_notes[:25]
    ) or "No release notes fetched — analyse based on API schema."

    schema_text = json.dumps(api_schema, indent=2)[:3000] if api_schema else "API schema unavailable."

    resources_text = "\n".join(f"  - {r}" for r in module_resources) or "  (none found)"

    prompt = f"""You are a Google Cloud Terraform expert analysing a Terraform module for the {product} service.

TERRAFORM MODULE CURRENTLY COVERS THESE RESOURCES:
{resources_text}

MODULE SUMMARY (variables, outputs, provider version):
{json.dumps(module_summary, indent=2)[:1500]}

RECENT GCP {product.upper()} RELEASE NOTES:
{notes_text}

GOOGLE {product.upper()} API SCHEMA SUMMARY (resource names and properties):
{schema_text}

TASK:
Identify NEW Google Cloud {product} GA features or API capabilities that:
1. Are GENERALLY AVAILABLE (not preview/beta/alpha)
2. Would require NEW or UPDATED Terraform resources or arguments in this module
3. Are NOT already covered by the existing module resources listed above

For each feature found, classify its Terraform impact:
- "new_resource"  → needs a brand-new google_{product}_ resource block
- "new_argument"  → needs a new argument on an existing resource
- "api_only"      → GCP API feature with no Terraform resource yet
- "unknown"       → unclear impact

Return a JSON array. Each element must have EXACTLY these fields:
{{
  "feature_name": "short name",
  "description": "one sentence what this feature does",
  "announced_date": "YYYY-MM-DD or empty string",
  "product": "{product}",
  "source": "release_notes" or "api_discovery" or "release_notes+llm",
  "terraform_impact": "new_resource|new_argument|api_only|unknown",
  "terraform_resources": ["google_{product}_xxx"],
  "terraform_args": ["arg_name_if_new_argument"],
  "source_url": "url or empty string",
  "ga_confirmed": true or false
}}

Return ONLY the JSON array. No markdown, no explanation. If no new GA features found, return [].
"""

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()

        items = json.loads(raw)
        features = []
        for item in items:
            try:
                # Ensure ga_confirmed is only True when release note confirms GA
                if item.get("ga_confirmed") and not _is_ga_confirmed(
                    item.get("description", "") + item.get("feature_name", "")
                ):
                    item["ga_confirmed"] = False
                features.append(GCPServiceFeatureModel(**item))
            except Exception:
                pass

        run.log(f"LLM identified {len(features)} new GCP {product} features")
        return features

    except Exception as e:
        run.log(f"LLM GCP feature analysis failed: {e}", level="warning")
        return []


def _is_ga_confirmed(text: str) -> bool:
    """Check whether text explicitly confirms a GA status."""
    lower = text.lower()
    # Must have a positive GA signal
    has_ga = any(sig.lower() in lower for sig in GA_SIGNALS)
    # Must NOT have a preview/beta qualifier overriding it
    has_not_ga = any(sig.lower() in lower for sig in NOT_GA_SIGNALS)
    return has_ga and not has_not_ga


# ── Gap analysis: what does the module LACK vs GCP service ────────────────────

def identify_module_gaps(
    features: list[GCPServiceFeatureModel],
    module_resources: list[str],
    tf_prefix: str,
) -> list[GCPServiceFeatureModel]:
    """
    Filter the feature list to only those where the module has a clear gap —
    i.e. the feature's proposed terraform_resources are not already in the module.
    """
    actionable = []
    for feature in features:
        if not feature.ga_confirmed:
            continue
        if feature.terraform_impact == "api_only":
            # API-only features are informational — include anyway
            actionable.append(feature)
            continue

        # Check if the proposed resources are already in the module
        proposed = feature.terraform_resources or []
        missing = [r for r in proposed if r not in module_resources]

        if missing or (feature.terraform_impact == "new_argument" and feature.terraform_args):
            # Resource is missing OR new argument not yet in module
            actionable.append(feature)

    return actionable


# ── Main entry point ──────────────────────────────────────────────────────────

async def scan_gcp_service(
    repo_name: str,
    run: WorkflowRun,
) -> GCPServiceScanResult:
    """
    Full GCP service feature scan pipeline:
    1. Identify the GCP product for the repo
    2. Fetch GCP release notes for that product
    3. Fetch Google API Discovery schema
    4. Inventory what resources/args the module already has
    5. LLM: compare GCP service state vs module state → identify gaps
    6. Filter to actionable GA features only
    7. Return GCPServiceScanResult

    This runs as Stage 1b alongside ga_detector's provider changelog scan.
    """
    run.stage = WorkflowStage.SCANNING_SERVICE
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        run.log(f"Repo '{repo_name}' not in config — skipping GCP service scan", level="warning")
        return _empty_result(repo_name, "unknown")

    product = repo_cfg.gcp_product
    product_info = GCP_PRODUCT_API_MAP.get(product)

    if not product_info:
        run.log(
            f"No GCP product mapping for '{product}' — "
            f"add it to GCP_PRODUCT_API_MAP in gcp_service_scanner.py",
            level="warning",
        )
        return _empty_result(repo_name, product)

    run.log(f"Scanning GCP {product} service for new GA features …")

    # ── Inventory what the module already covers
    current_tag = get_latest_tag(repo_name) or "main"
    module_resources_raw = get_all_resources(repo_name, current_tag)
    module_resources = list({r.resource_type for r in module_resources_raw})
    module_summary = summarize_module(repo_name, current_tag)
    run.log(f"Module currently has {len(module_resources)} resource types at tag {current_tag}")

    # ── Fetch GCP release notes
    run.log(f"Fetching GCP release notes for {product_info['release_notes_product']} …")
    release_notes = await fetch_gcp_release_notes(product_info["release_notes_product"])
    run.log(f"Fetched {len(release_notes)} release note entries")

    # ── Fetch API Discovery schema
    run.log(f"Fetching Google API Discovery schema ({product_info['api_name']} {product_info['version']}) …")
    api_schema = await fetch_discovery_api_schema(
        product_info["api_name"], product_info["version"]
    )
    if api_schema:
        run.log(f"API schema fetched: {len(api_schema.get('resources', {}))} resources, "
                f"{len(api_schema.get('schemas', {}))} schemas")
    else:
        run.log("API schema unavailable — LLM will rely on release notes only", level="warning")

    # ── LLM analysis
    run.log("Analysing GCP service features vs module coverage …")
    all_features = await analyze_gcp_features_with_llm(
        product=product,
        product_info=product_info,
        module_summary=module_summary,
        module_resources=module_resources,
        release_notes=release_notes,
        api_schema=api_schema,
        run=run,
    )

    # ── Gap analysis
    actionable = identify_module_gaps(
        all_features, module_resources, product_info["tf_resource_prefix"]
    )
    run.log(
        f"GCP service scan complete: {len(all_features)} features found, "
        f"{len(actionable)} actionable gaps identified"
    )

    # ── Build summary
    summary = _build_scan_summary(product, all_features, actionable, product_info)

    return GCPServiceScanResult(
        repo_name=repo_name,
        gcp_product=product,
        scan_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        total_features=len(all_features),
        actionable_count=len(actionable),
        features=all_features,
        actionable_features=actionable,
        module_resources=module_resources,
        summary=summary,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_result(repo_name: str, product: str) -> GCPServiceScanResult:
    return GCPServiceScanResult(
        repo_name=repo_name,
        gcp_product=product,
        scan_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        total_features=0,
        actionable_count=0,
        features=[],
        actionable_features=[],
        module_resources=[],
        summary="GCP service scan skipped — product not mapped or repo not found.",
    )


def _build_scan_summary(
    product: str,
    features: list[GCPServiceFeatureModel],
    actionable: list[GCPServiceFeatureModel],
    product_info: dict,
) -> str:
    if not features:
        return (
            f"No new GA {product} features found beyond what the module currently covers. "
            f"The module appears up to date with the GCP {product} service."
        )

    new_resources = [f for f in actionable if f.terraform_impact == "new_resource"]
    new_args      = [f for f in actionable if f.terraform_impact == "new_argument"]
    api_only      = [f for f in actionable if f.terraform_impact == "api_only"]

    parts = [
        f"GCP {product} service scan identified {len(features)} new GA features "
        f"({len(actionable)} require module updates)."
    ]
    if new_resources:
        parts.append(
            f"{len(new_resources)} feature(s) need new Terraform resources: "
            + ", ".join(f.feature_name[:40] for f in new_resources[:3]) + "."
        )
    if new_args:
        parts.append(
            f"{len(new_args)} feature(s) need new arguments on existing resources."
        )
    if api_only:
        parts.append(
            f"{len(api_only)} feature(s) are GCP API-level with no Terraform resource yet."
        )
    parts.append(f"Reference: {product_info.get('docs_url', '')}")
    return " ".join(parts)
