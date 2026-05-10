"""
registry_api.py — Fetch Terraform provider documentation.

Strategy (smart offline/online):
  1. Check local cache (./data/registry_cache/).
  2. If cache miss and network available → fetch from GitHub raw (markdown) or
     scrape registry.terraform.io as fallback.
  3. If offline → return cached content or an informative placeholder.

Supports: google (GCP), aws (AWS), azurerm (Azure).
"""
from __future__ import annotations

import re
import socket
from typing import Optional

import httpx

from backend.registry_fetcher.cache_manager import get_cached_doc, save_cached_doc

# ── Provider GitHub raw doc roots ────────────────────────────────────────────

_GITHUB_RAW = {
    "google":  "https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/website/docs/r",
    "aws":     "https://raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/website/docs/r",
    "azurerm": "https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/r",
}

_PROVIDER_PREFIX = {
    "google":  "google_",
    "aws":     "aws_",
    "azurerm": "azurerm_",
}

# ── Service → resource list ───────────────────────────────────────────────────

_SERVICE_MAP: dict[str, list[str]] = {
    # GCP
    "cloud run":           ["google_cloud_run_v2_service", "google_cloud_run_v2_job", "google_cloud_run_service"],
    "bigquery":            ["google_bigquery_dataset", "google_bigquery_table", "google_bigquery_job", "google_bigquery_connection"],
    "cloud storage":       ["google_storage_bucket", "google_storage_bucket_iam_binding", "google_storage_bucket_object"],
    "gcs":                 ["google_storage_bucket", "google_storage_bucket_object"],
    "pub/sub":             ["google_pubsub_topic", "google_pubsub_subscription", "google_pubsub_topic_iam_binding"],
    "pubsub":              ["google_pubsub_topic", "google_pubsub_subscription"],
    "cloud sql":           ["google_sql_database_instance", "google_sql_database", "google_sql_user"],
    "gke":                 ["google_container_cluster", "google_container_node_pool"],
    "kubernetes engine":   ["google_container_cluster", "google_container_node_pool"],
    "cloud functions":     ["google_cloudfunctions2_function", "google_cloudfunctions_function"],
    "cloud build":         ["google_cloudbuild_trigger", "google_cloudbuild_worker_pool"],
    "dataflow":            ["google_dataflow_flex_template_job", "google_dataflow_job"],
    "spanner":             ["google_spanner_instance", "google_spanner_database", "google_spanner_database_iam_binding"],
    "firestore":           ["google_firestore_database", "google_firestore_document"],
    "vpc":                 ["google_compute_network", "google_compute_subnetwork", "google_compute_firewall", "google_compute_router"],
    "compute engine":      ["google_compute_instance", "google_compute_disk", "google_compute_address", "google_compute_instance_template"],
    "gce":                 ["google_compute_instance", "google_compute_disk"],
    "artifact registry":   ["google_artifact_registry_repository"],
    "secret manager":      ["google_secret_manager_secret", "google_secret_manager_secret_version"],
    "memorystore":         ["google_redis_instance", "google_memcache_instance"],
    "redis":               ["google_redis_instance"],
    "dataproc":            ["google_dataproc_cluster", "google_dataproc_job", "google_dataproc_autoscaling_policy"],
    "composer":            ["google_composer_environment"],
    "bigtable":            ["google_bigtable_instance", "google_bigtable_table", "google_bigtable_instance_iam_binding"],
    "vertex ai":           ["google_vertex_ai_dataset", "google_vertex_ai_endpoint", "google_vertex_ai_featurestore"],
    "datastream":          ["google_datastream_connection_profile", "google_datastream_stream"],
    "alloydb":             ["google_alloydb_cluster", "google_alloydb_instance"],
    "cloud armor":         ["google_compute_security_policy"],
    "load balancer":       ["google_compute_global_forwarding_rule", "google_compute_backend_service", "google_compute_url_map"],
    "iam":                 ["google_project_iam_binding", "google_service_account", "google_service_account_iam_binding"],
    "cloud tasks":         ["google_cloud_tasks_queue"],
    "cloud scheduler":     ["google_cloud_scheduler_job"],
    "dns":                 ["google_dns_managed_zone", "google_dns_record_set"],
    "network":             ["google_compute_network", "google_compute_subnetwork", "google_compute_firewall"],

    # AWS
    "s3":                  ["aws_s3_bucket", "aws_s3_bucket_policy", "aws_s3_bucket_versioning", "aws_s3_bucket_server_side_encryption_configuration"],
    "ec2":                 ["aws_instance", "aws_security_group", "aws_key_pair", "aws_eip", "aws_launch_template"],
    "lambda":              ["aws_lambda_function", "aws_lambda_event_source_mapping", "aws_lambda_permission"],
    "rds":                 ["aws_db_instance", "aws_db_subnet_group", "aws_db_parameter_group", "aws_rds_cluster"],
    "eks":                 ["aws_eks_cluster", "aws_eks_node_group", "aws_eks_fargate_profile"],
    "ecs":                 ["aws_ecs_cluster", "aws_ecs_service", "aws_ecs_task_definition"],
    "dynamodb":            ["aws_dynamodb_table", "aws_dynamodb_global_table"],
    "sqs":                 ["aws_sqs_queue", "aws_sqs_queue_policy"],
    "sns":                 ["aws_sns_topic", "aws_sns_topic_subscription", "aws_sns_topic_policy"],
    "vpc":                 ["aws_vpc", "aws_subnet", "aws_security_group", "aws_internet_gateway", "aws_nat_gateway", "aws_route_table"],
    "iam":                 ["aws_iam_role", "aws_iam_policy", "aws_iam_role_policy_attachment", "aws_iam_user"],
    "cloudfront":          ["aws_cloudfront_distribution", "aws_cloudfront_origin_access_identity"],
    "api gateway":         ["aws_apigatewayv2_api", "aws_apigatewayv2_stage", "aws_api_gateway_rest_api"],
    "elasticache":         ["aws_elasticache_cluster", "aws_elasticache_replication_group", "aws_elasticache_subnet_group"],
    "kinesis":             ["aws_kinesis_stream", "aws_kinesis_firehose_delivery_stream"],
    "glue":                ["aws_glue_job", "aws_glue_catalog_database", "aws_glue_crawler"],
    "emr":                 ["aws_emr_cluster", "aws_emr_instance_group"],
    "redshift":            ["aws_redshift_cluster", "aws_redshift_subnet_group"],
    "msk":                 ["aws_msk_cluster", "aws_msk_configuration"],
    "step functions":      ["aws_sfn_state_machine"],
    "eventbridge":         ["aws_cloudwatch_event_rule", "aws_cloudwatch_event_target"],
    "secrets manager":     ["aws_secretsmanager_secret", "aws_secretsmanager_secret_version"],
    "cloudwatch":          ["aws_cloudwatch_log_group", "aws_cloudwatch_metric_alarm"],
    "route53":             ["aws_route53_zone", "aws_route53_record"],
    "alb":                 ["aws_lb", "aws_lb_listener", "aws_lb_target_group"],
    "ecr":                 ["aws_ecr_repository", "aws_ecr_lifecycle_policy"],

    # Azure
    "azure functions":     ["azurerm_linux_function_app", "azurerm_function_app", "azurerm_service_plan"],
    "blob storage":        ["azurerm_storage_account", "azurerm_storage_container", "azurerm_storage_blob"],
    "storage":             ["azurerm_storage_account", "azurerm_storage_container"],
    "aks":                 ["azurerm_kubernetes_cluster", "azurerm_kubernetes_cluster_node_pool"],
    "sql":                 ["azurerm_mssql_server", "azurerm_mssql_database", "azurerm_sql_server"],
    "cosmos db":           ["azurerm_cosmosdb_account", "azurerm_cosmosdb_sql_database", "azurerm_cosmosdb_sql_container"],
    "service bus":         ["azurerm_servicebus_namespace", "azurerm_servicebus_queue", "azurerm_servicebus_topic"],
    "event hub":           ["azurerm_eventhub_namespace", "azurerm_eventhub", "azurerm_eventhub_consumer_group"],
    "vnet":                ["azurerm_virtual_network", "azurerm_subnet", "azurerm_network_security_group", "azurerm_public_ip"],
    "app service":         ["azurerm_linux_web_app", "azurerm_app_service", "azurerm_service_plan"],
    "container apps":      ["azurerm_container_app", "azurerm_container_app_environment"],
    "key vault":           ["azurerm_key_vault", "azurerm_key_vault_secret", "azurerm_key_vault_key"],
    "iam":                 ["azurerm_role_assignment", "azurerm_user_assigned_identity"],
    "data factory":        ["azurerm_data_factory", "azurerm_data_factory_pipeline", "azurerm_data_factory_linked_service_azure_blob_storage"],
    "synapse":             ["azurerm_synapse_workspace", "azurerm_synapse_spark_pool", "azurerm_synapse_sql_pool"],
    "databricks":          ["azurerm_databricks_workspace", "azurerm_databricks_access_connector"],
    "postgresql":          ["azurerm_postgresql_server", "azurerm_postgresql_database", "azurerm_postgresql_flexible_server"],
    "redis":               ["azurerm_redis_cache"],
    "container registry":  ["azurerm_container_registry"],
    "monitor":             ["azurerm_monitor_action_group", "azurerm_monitor_metric_alert", "azurerm_log_analytics_workspace"],
}


def is_network_available() -> bool:
    try:
        socket.setdefaulttimeout(3)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except OSError:
        return False


def resolve_resources(service_name: str, provider: str) -> list[str]:
    """Map a human service name to a list of Terraform resource type strings."""
    key = service_name.lower().strip()

    # Exact match
    if key in _SERVICE_MAP:
        pf = _PROVIDER_PREFIX.get(provider, provider + "_")
        return [r for r in _SERVICE_MAP[key] if r.startswith(pf)]

    # Substring match
    for svc, resources in _SERVICE_MAP.items():
        if key in svc or svc in key:
            pf = _PROVIDER_PREFIX.get(provider, provider + "_")
            matched = [r for r in resources if r.startswith(pf)]
            if matched:
                return matched

    # Heuristic fallback
    slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    pf = _PROVIDER_PREFIX.get(provider, provider + "_")
    return [f"{pf}{slug}"]


async def fetch_resource_docs(provider: str, resource_type: str) -> str:
    """Return documentation for a single Terraform resource type."""
    cached = get_cached_doc(provider, resource_type)
    if cached:
        return cached

    if not is_network_available():
        return f"[Offline — no cached docs for {resource_type}]"

    content = await _from_github(provider, resource_type)
    if not content:
        content = await _from_registry_scrape(provider, resource_type)
    if content:
        save_cached_doc(provider, resource_type, content)
        return content

    return f"[No documentation found for {resource_type}]"


async def fetch_service_docs(provider: str, service_name: str) -> str:
    """Fetch and combine docs for all resources matching a service name."""
    resources = resolve_resources(service_name, provider)
    parts = [f"# Terraform Docs: {service_name} ({provider})\n"]
    for resource in resources[:6]:
        doc = await fetch_resource_docs(provider, resource)
        if doc and not doc.startswith("["):
            parts.append(f"\n## Resource: {resource}\n{doc[:5000]}")
    return "\n".join(parts) if len(parts) > 1 else f"No documentation cached for {service_name}."


async def _from_github(provider: str, resource_type: str) -> Optional[str]:
    """Fetch resource docs from provider GitHub repo (markdown format)."""
    base = _GITHUB_RAW.get(provider)
    if not base:
        return None

    prefix = _PROVIDER_PREFIX.get(provider, f"{provider}_")
    slug = resource_type[len(prefix):] if resource_type.startswith(prefix) else resource_type

    for ext in [".html.markdown", ".markdown", ".md"]:
        url = f"{base}/{slug}{ext}"
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    text = r.text
                    # Strip YAML frontmatter
                    if text.startswith("---"):
                        end = text.find("---", 3)
                        if end > 0:
                            text = text[end + 3:].strip()
                    return text[:6000]
        except Exception:
            continue
    return None


async def _from_registry_scrape(provider: str, resource_type: str) -> Optional[str]:
    """Scrape registry.terraform.io as a fallback when GitHub raw 404s."""
    prefix = _PROVIDER_PREFIX.get(provider, f"{provider}_")
    slug = resource_type[len(prefix):] if resource_type.startswith(prefix) else resource_type
    url = f"https://registry.terraform.io/providers/hashicorp/{provider}/latest/docs/resources/{slug}"

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        # Target the main content div
        content_div = (
            soup.find("div", {"class": re.compile(r"docs-content|markdown-body|prose", re.I)})
            or soup.find("main")
            or soup.find("article")
        )
        if content_div:
            return content_div.get_text(separator="\n", strip=True)[:6000]
    except Exception:
        pass
    return None
