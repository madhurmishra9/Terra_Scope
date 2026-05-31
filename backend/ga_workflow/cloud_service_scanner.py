"""
cloud_service_scanner.py — Scans cloud provider release notes for new GA
features that are not yet reflected in the Terraform module.

Supports (v2.3):
  GCP   — Google Cloud release notes JSON feed + Discovery API schema
  AWS   — AWS "What's New" RSS feed (aws.amazon.com/about-aws/whats-new)
  Azure — Azure Updates RSS feed (azure.microsoft.com/en-us/updates)

Each provider returns a GCPServiceScanResult (unified model, provider-agnostic
despite the GCP-originating name) with:
  - features:            every GA feature found in release notes
  - actionable_features: features NOT yet in the Terraform module's .tf code

Entry point:
  scan_cloud_service(repo_name, provider_key, run) → GCPServiceScanResult
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

from backend.config import get_config
from backend.agent.tools.git_tools import get_latest_tag
from backend.agent.tools.hcl_tools import get_all_resources, summarize_module
from backend.ga_workflow.ga_models import (
    GCPServiceFeatureModel,
    GCPServiceScanResult,
    CloudProvider,
    WorkflowRun,
    WorkflowStage,
)

HTTP_TIMEOUT = 15.0

# ── AWS product-slug map ───────────────────────────────────────────────────────
# Maps the terrascope gcp_product (used as cloud_product) to AWS service names
# used in the "What's New" feed categories/tags.
AWS_PRODUCT_MAP: dict[str, dict] = {
    "s3": {
        "aws_service":   "Amazon S3",
        "tf_prefix":     "aws_s3_",
        "docs_url":      "https://aws.amazon.com/s3/whats-new/",
        "search_terms":  ["S3", "Simple Storage Service"],
    },
    "ec2": {
        "aws_service":   "Amazon EC2",
        "tf_prefix":     "aws_instance",
        "docs_url":      "https://aws.amazon.com/ec2/whats-new/",
        "search_terms":  ["EC2", "Elastic Compute Cloud"],
    },
    "lambda": {
        "aws_service":   "AWS Lambda",
        "tf_prefix":     "aws_lambda_",
        "docs_url":      "https://aws.amazon.com/lambda/whats-new/",
        "search_terms":  ["Lambda", "serverless"],
    },
    "rds": {
        "aws_service":   "Amazon RDS",
        "tf_prefix":     "aws_db_",
        "docs_url":      "https://aws.amazon.com/rds/whats-new/",
        "search_terms":  ["RDS", "Relational Database"],
    },
    "eks": {
        "aws_service":   "Amazon EKS",
        "tf_prefix":     "aws_eks_",
        "docs_url":      "https://aws.amazon.com/eks/whats-new/",
        "search_terms":  ["EKS", "Elastic Kubernetes"],
    },
    "dynamodb": {
        "aws_service":   "Amazon DynamoDB",
        "tf_prefix":     "aws_dynamodb_",
        "docs_url":      "https://aws.amazon.com/dynamodb/whats-new/",
        "search_terms":  ["DynamoDB"],
    },
    "sqs": {
        "aws_service":   "Amazon SQS",
        "tf_prefix":     "aws_sqs_",
        "docs_url":      "https://aws.amazon.com/sqs/whats-new/",
        "search_terms":  ["SQS", "Simple Queue"],
    },
    "sns": {
        "aws_service":   "Amazon SNS",
        "tf_prefix":     "aws_sns_",
        "docs_url":      "https://aws.amazon.com/sns/whats-new/",
        "search_terms":  ["SNS", "Simple Notification"],
    },
    "cloudfront": {
        "aws_service":   "Amazon CloudFront",
        "tf_prefix":     "aws_cloudfront_",
        "docs_url":      "https://aws.amazon.com/cloudfront/whats-new/",
        "search_terms":  ["CloudFront"],
    },
    "kinesis": {
        "aws_service":   "Amazon Kinesis",
        "tf_prefix":     "aws_kinesis_",
        "docs_url":      "https://aws.amazon.com/kinesis/whats-new/",
        "search_terms":  ["Kinesis"],
    },
}

# ── Azure product-slug map ─────────────────────────────────────────────────────
AZURE_PRODUCT_MAP: dict[str, dict] = {
    "blob": {
        "azure_service": "Azure Blob Storage",
        "tf_prefix":     "azurerm_storage_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=storage-accounts",
        "search_terms":  ["Blob", "Storage"],
    },
    "aks": {
        "azure_service": "Azure Kubernetes Service",
        "tf_prefix":     "azurerm_kubernetes_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=kubernetes-service",
        "search_terms":  ["AKS", "Kubernetes"],
    },
    "functions": {
        "azure_service": "Azure Functions",
        "tf_prefix":     "azurerm_function_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=functions",
        "search_terms":  ["Functions", "serverless"],
    },
    "sql": {
        "azure_service": "Azure SQL",
        "tf_prefix":     "azurerm_sql_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=azure-sql-database",
        "search_terms":  ["SQL", "database"],
    },
    "cosmos": {
        "azure_service": "Azure Cosmos DB",
        "tf_prefix":     "azurerm_cosmosdb_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=cosmos-db",
        "search_terms":  ["Cosmos", "CosmosDB"],
    },
    "servicebus": {
        "azure_service": "Azure Service Bus",
        "tf_prefix":     "azurerm_servicebus_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=service-bus",
        "search_terms":  ["Service Bus"],
    },
    "eventhub": {
        "azure_service": "Azure Event Hubs",
        "tf_prefix":     "azurerm_eventhub_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=event-hubs",
        "search_terms":  ["Event Hub", "EventHub"],
    },
    "appservice": {
        "azure_service": "Azure App Service",
        "tf_prefix":     "azurerm_app_service",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=app-service",
        "search_terms":  ["App Service"],
    },
    "containerapps": {
        "azure_service": "Azure Container Apps",
        "tf_prefix":     "azurerm_container_app",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=container-apps",
        "search_terms":  ["Container Apps"],
    },
    "keyvault": {
        "azure_service": "Azure Key Vault",
        "tf_prefix":     "azurerm_key_vault",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/?product=key-vault",
        "search_terms":  ["Key Vault"],
    },
}

# ── GA signal detection ────────────────────────────────────────────────────────

_GA_SIGNALS = [
    "generally available", " GA ", "is now GA", "graduated to GA",
    "now available", "production-ready", "launched", "released",
    "now generally available",
]
_NOT_GA_SIGNALS = [
    "preview", "beta", "alpha", "experimental", "pre-GA",
    "public preview", "private preview",
]


def _is_ga(text: str) -> bool:
    lower = text.lower()
    if any(s.lower() in lower for s in _NOT_GA_SIGNALS):
        return False
    return any(s.lower() in lower for s in _GA_SIGNALS)


# ── AWS release note fetching ──────────────────────────────────────────────────

async def fetch_aws_whats_new(search_terms: list[str], days_back: int = 180) -> list[dict]:
    """
    Fetch the AWS "What's New" RSS feed and filter to the given service terms.
    Returns a list of {title, description, date, url, source} dicts.
    """
    RSS_URL = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"
    results: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(RSS_URL)
            if resp.status_code != 200:
                return results
            root = ET.fromstring(resp.text)
    except Exception as e:
        print(f"[cloud_scanner] AWS feed error: {e}")
        return results

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        link  = (item.findtext("link") or "").strip()
        text  = f"{title} {desc}".lower()

        # Filter to matching service
        if not any(term.lower() in text for term in search_terms):
            continue

        # Date filter
        try:
            from email.utils import parsedate_to_datetime
            pub_dt = parsedate_to_datetime(pub)
            if pub_dt.replace(tzinfo=timezone.utc) < cutoff:
                continue
        except Exception:
            pass

        results.append({
            "title":       title,
            "description": re.sub(r"<[^>]+>", "", desc)[:500],
            "date":        pub,
            "url":         link,
            "source":      "aws_whats_new",
        })

    return results


# ── Azure release note fetching ────────────────────────────────────────────────

async def fetch_azure_updates(search_terms: list[str], days_back: int = 180) -> list[dict]:
    """
    Fetch the Azure Updates RSS feed and filter to the given service terms.
    Returns a list of {title, description, date, url, source} dicts.
    """
    RSS_URL = "https://azure.microsoft.com/en-us/updates/feed/"
    results: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(RSS_URL)
            if resp.status_code != 200:
                return results
            # Azure feed is Atom or RSS — try both
            root = ET.fromstring(resp.text)
    except Exception as e:
        print(f"[cloud_scanner] Azure feed error: {e}")
        return results

    # Handle RSS 2.0
    items = root.findall(".//item")
    # Handle Atom
    atom_ns = "http://www.w3.org/2005/Atom"
    if not items:
        items = root.findall(f".//{atom_ns}entry")

    for item in items:
        title = (
            item.findtext("title")
            or item.findtext(f"{{{atom_ns}}}title")
            or ""
        ).strip()
        desc = (
            item.findtext("description")
            or item.findtext(f"{{{atom_ns}}}summary")
            or item.findtext(f"{{{atom_ns}}}content")
            or ""
        ).strip()
        pub = (
            item.findtext("pubDate")
            or item.findtext(f"{{{atom_ns}}}updated")
            or item.findtext(f"{{{atom_ns}}}published")
            or ""
        ).strip()
        link_el = item.find(f"{{{atom_ns}}}link")
        link = (
            item.findtext("link")
            or (link_el.get("href") if link_el is not None else "")
            or ""
        ).strip()
        text = f"{title} {desc}".lower()

        if not any(term.lower() in text for term in search_terms):
            continue

        results.append({
            "title":       title,
            "description": re.sub(r"<[^>]+>", "", desc)[:500],
            "date":        pub,
            "url":         link,
            "source":      "azure_updates",
        })

    return results[:60]


# ── LLM analysis (cloud-agnostic) ─────────────────────────────────────────────

async def analyze_cloud_features_with_llm(
    cloud_name: str,
    service_name: str,
    tf_prefix: str,
    module_resources: list[str],
    module_summary: dict,
    release_notes: list[dict],
    run: WorkflowRun,
) -> list[GCPServiceFeatureModel]:
    """
    Ask the local LLM to identify new GA features not yet in the module.
    Cloud-agnostic: works for GCP, AWS, Azure by varying the prompt.
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
    ) or "No release notes fetched."

    resources_text = "\n".join(f"  - {r}" for r in module_resources) or "  (none found)"

    prompt = f"""You are a {cloud_name} Terraform expert analysing a module for {service_name}.

TERRAFORM MODULE CURRENTLY COVERS THESE RESOURCES:
{resources_text}

MODULE SUMMARY:
{json.dumps(module_summary, indent=2)[:1500]}

RECENT {cloud_name.upper()} {service_name.upper()} RELEASE NOTES (GA features only):
{notes_text}

Identify NEW GA features that:
  (a) {cloud_name} now supports for {service_name}
  (b) are NOT yet reflected in the Terraform module's .tf resources/arguments

Return a JSON array. Each item has exactly these fields:
- feature_name: short identifier (snake_case)
- description: one sentence what it is
- announced_date: ISO date string or empty string
- terraform_impact: one of: new_resource, new_argument, api_only, unknown
- terraform_resources: list of affected {tf_prefix}xxx resource types (may be empty)
- terraform_args: list of new argument names (may be empty)
- ga_confirmed: true if explicitly stated as GA, false if uncertain
- source_url: the announcement URL or empty string

Return ONLY the JSON array, no markdown, no explanation.
If there are no actionable new features, return an empty array []."""

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()

        import json as _json
        features = []
        for item in _json.loads(raw):
            try:
                features.append(GCPServiceFeatureModel(**item))
            except Exception:
                pass
        run.log(f"LLM identified {len(features)} new {cloud_name} features for {service_name}")
        return features
    except Exception as e:
        run.log(f"LLM feature analysis failed: {e}", level="warning")
        return []


# ── GCP scanner (delegates to existing gcp_service_scanner) ───────────────────

async def scan_gcp_product(repo_name: str, run: WorkflowRun, days_back: int = 180) -> GCPServiceScanResult:
    from backend.ga_workflow.gcp_service_scanner import scan_gcp_service
    return await scan_gcp_service(repo_name=repo_name, run=run)


# ── AWS scanner ───────────────────────────────────────────────────────────────

async def scan_aws_service(
    repo_name: str,
    product: str,
    run: WorkflowRun,
    days_back: int = 180,
) -> GCPServiceScanResult:
    """Scan AWS What's New feed for new GA features for the given service."""
    run.stage = WorkflowStage.SCANNING_SERVICE
    cfg = get_config()
    now = datetime.now(timezone.utc).isoformat()

    product_info = AWS_PRODUCT_MAP.get(product, {
        "aws_service":  product,
        "tf_prefix":    "aws_",
        "docs_url":     "https://aws.amazon.com/about-aws/whats-new/",
        "search_terms": [product],
    })

    run.log(f"Scanning AWS What's New for: {product_info['aws_service']}")

    # Fetch release notes
    notes = await fetch_aws_whats_new(product_info["search_terms"], days_back=days_back)
    run.log(f"Fetched {len(notes)} AWS What's New entries for {product}")

    # Get current module resources
    current_tag = get_latest_tag(repo_name) or "main"
    resources = get_all_resources(repo_name, current_tag)
    module_resources = list({r.resource_type for r in resources})
    module_summary = summarize_module(repo_name, current_tag)

    # LLM analysis
    features = await analyze_cloud_features_with_llm(
        cloud_name="AWS",
        service_name=product_info["aws_service"],
        tf_prefix=product_info["tf_prefix"],
        module_resources=module_resources,
        module_summary=module_summary,
        release_notes=notes,
        run=run,
    )

    # Classify actionable
    actionable = [f for f in features if f.terraform_impact in ("new_resource", "new_argument")]
    total = len(features)
    summary = (
        f"Found {total} new AWS {product_info['aws_service']} GA features, "
        f"{len(actionable)} actionable for Terraform module."
        if total > 0
        else f"No new GA {product_info['aws_service']} features found beyond what the module currently covers."
    )

    return GCPServiceScanResult(
        repo_name=repo_name,
        gcp_product=product,
        scan_date=now,
        total_features=total,
        actionable_count=len(actionable),
        features=features,
        actionable_features=actionable,
        module_resources=module_resources,
        summary=summary,
    )


# ── Azure scanner ─────────────────────────────────────────────────────────────

async def scan_azure_service(
    repo_name: str,
    product: str,
    run: WorkflowRun,
    days_back: int = 180,
) -> GCPServiceScanResult:
    """Scan Azure Updates feed for new GA features for the given service."""
    run.stage = WorkflowStage.SCANNING_SERVICE
    now = datetime.now(timezone.utc).isoformat()

    product_info = AZURE_PRODUCT_MAP.get(product, {
        "azure_service": product,
        "tf_prefix":     "azurerm_",
        "docs_url":      "https://azure.microsoft.com/en-us/updates/",
        "search_terms":  [product],
    })

    run.log(f"Scanning Azure Updates for: {product_info['azure_service']}")

    notes = await fetch_azure_updates(product_info["search_terms"], days_back=days_back)
    run.log(f"Fetched {len(notes)} Azure Updates entries for {product}")

    current_tag = get_latest_tag(repo_name) or "main"
    resources = get_all_resources(repo_name, current_tag)
    module_resources = list({r.resource_type for r in resources})
    module_summary = summarize_module(repo_name, current_tag)

    features = await analyze_cloud_features_with_llm(
        cloud_name="Azure",
        service_name=product_info["azure_service"],
        tf_prefix=product_info["tf_prefix"],
        module_resources=module_resources,
        module_summary=module_summary,
        release_notes=notes,
        run=run,
    )

    actionable = [f for f in features if f.terraform_impact in ("new_resource", "new_argument")]
    total = len(features)
    summary = (
        f"Found {total} new Azure {product_info['azure_service']} GA features, "
        f"{len(actionable)} actionable for Terraform module."
        if total > 0
        else f"No new GA {product_info['azure_service']} features found beyond what the module currently covers."
    )

    return GCPServiceScanResult(
        repo_name=repo_name,
        gcp_product=product,
        scan_date=now,
        total_features=total,
        actionable_count=len(actionable),
        features=features,
        actionable_features=actionable,
        module_resources=module_resources,
        summary=summary,
    )


# ── Main routing entry point ──────────────────────────────────────────────────

async def scan_cloud_service(
    repo_name: str,
    provider_key: str,
    run: WorkflowRun,
    days_back: int = 180,
) -> GCPServiceScanResult:
    """
    Route to the correct cloud scanner based on provider_key.
    Returns a GCPServiceScanResult (provider-agnostic unified model).
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    product = repo_cfg.gcp_product if repo_cfg else "unknown"

    if provider_key == "aws":
        return await scan_aws_service(repo_name, product, run, days_back=days_back)
    elif provider_key == "azurerm":
        return await scan_azure_service(repo_name, product, run, days_back=days_back)
    else:
        return await scan_gcp_product(repo_name, run, days_back=days_back)
