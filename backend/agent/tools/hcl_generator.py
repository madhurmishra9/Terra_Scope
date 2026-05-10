"""
hcl_generator.py — Context builder and validator for the HCL generation pipeline.

Responsibilities:
  1. GCP_RESOURCE_SCHEMAS  — embedded, schema-grounded attribute lists per resource type
  2. build_generation_context() — assemble LLM context from schemas + existing module code
  3. _validate_hcl_files()      — Python-side post-generation checks (security, lint, quality)
  4. _parse_generated_files()   — parse ---FILE:---ENDFILE--- markers from LLM output

No LLM calls here. Pure Python / HCL analysis.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from backend.agent.models import GeneratedFile, ValidationNote
from backend.agent.tools.git_tools import get_file_at_tag, list_tf_files_at_tag, get_latest_tag
from backend.config import get_config


# ── Embedded provider schema library ──────────────────────────────────────────
# Source: hashicorp/google >= 5.39, < 8  /  hashicorp/google-beta >= 5.39, < 8
# Format per resource:
#   required   — must always be set
#   optional   — set when relevant
#   blocks     — nested block names + their key attributes
#   deprecated — NEVER emit these
#   notes      — free-text grounding note

GCP_RESOURCE_SCHEMAS: dict[str, dict] = {
    # ── BigQuery ───────────────────────────────────────────────────────────────
    "google_bigquery_dataset": {
        "required": ["dataset_id", "project"],
        "optional": [
            "friendly_name", "description", "location", "delete_contents_on_destroy",
            "default_table_expiration_ms", "default_partition_expiration_ms",
            "max_time_travel_hours", "storage_billing_model", "labels",
            "resource_tags", "is_case_insensitive",
        ],
        "blocks": {
            "default_encryption_configuration": ["kms_key_name"],
            "access": ["role", "user_by_email", "group_by_email", "domain", "special_group"],
        },
        "deprecated": [],
        "notes": "max_time_travel_hours must be 48-168. storage_billing_model: LOGICAL or PHYSICAL.",
    },
    "google_bigquery_table": {
        "required": ["dataset_id", "table_id"],
        "optional": [
            "project", "friendly_name", "description", "labels", "schema",
            "clustering", "expiration_time", "deletion_protection", "require_partition_filter",
        ],
        "blocks": {
            "time_partitioning": ["type", "expiration_ms", "field", "require_partition_filter"],
            "range_partitioning": ["field", "range.start", "range.end", "range.interval"],
            "view": ["query", "use_legacy_sql"],
            "materialized_view": ["query", "enable_refresh", "refresh_interval_ms", "max_staleness"],
            "external_data_configuration": [
                "autodetect", "source_format", "source_uris", "schema",
                "compression", "ignore_unknown_values", "max_bad_records", "connection_id",
                "csv_options.*", "google_sheets_options.*", "hive_partitioning_options.*",
            ],
        },
        "deprecated": [],
        "notes": "lifecycle { ignore_changes = [encryption_configuration] } when dataset-level KMS is used.",
    },
    "google_bigquery_dataset_iam_binding": {
        "required": ["project", "dataset_id", "role", "members"],
        "optional": [],
        "blocks": {"condition": ["title", "description", "expression"]},
        "deprecated": [],
        "notes": "Prefer over legacy access{} blocks — avoids IAM->primitive role conversion diffs.",
    },
    "google_bigquery_row_access_policy": {
        "required": ["dataset_id", "table_id", "policy_id", "filter_predicate"],
        "optional": ["project"],
        "blocks": {"grantees": ["user_by_email", "group_by_email", "iam_member"]},
        "deprecated": [],
        "notes": "filter_predicate is a SQL WHERE clause. Each grantee block must have exactly one identity field.",
    },
    "google_bigquery_routine": {
        "required": ["dataset_id", "routine_id", "routine_type", "language", "definition_body"],
        "optional": ["project", "description", "return_type"],
        "blocks": {"arguments": ["name", "data_type", "mode", "argument_kind"]},
        "deprecated": [],
        "notes": "routine_type: SCALAR_FUNCTION, PROCEDURE, TABLE_VALUED_FUNCTION.",
    },

    # ── Cloud Storage ──────────────────────────────────────────────────────────
    "google_storage_bucket": {
        "required": ["name", "location"],
        "optional": [
            "project", "storage_class", "labels", "public_access_prevention",
            "uniform_bucket_level_access", "force_destroy",
        ],
        "blocks": {
            "versioning": ["enabled"],
            "encryption": ["default_kms_key_name"],
            "logging": ["log_bucket", "log_object_prefix"],
            "retention_policy": ["is_locked", "retention_period"],
            "soft_delete_policy": ["retention_duration_seconds"],
            "autoclass": ["enabled"],
            "hierarchical_namespace": ["enabled"],
            "lifecycle_rule": ["action.type", "action.storage_class", "condition.*"],
            "cors": ["origin", "method", "response_header", "max_age_seconds"],
            "website": ["main_page_suffix", "not_found_page"],
            "ip_filter": ["mode", "allow_cross_org_vpcs", "allow_all_service_agent_access",
                          "public_network_source.allowed_ip_cidr_ranges",
                          "vpc_network_sources.network", "vpc_network_sources.allowed_ip_cidr_ranges"],
        },
        "deprecated": [],
        "notes": "[SECURITY] public_access_prevention default should be 'enforced', not 'inherited'.",
    },
    "google_storage_bucket_iam_binding": {
        "required": ["bucket", "role", "members"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },
    "google_storage_notification": {
        "required": ["bucket", "topic", "payload_format"],
        "optional": ["event_types", "custom_attributes", "object_name_prefix"],
        "blocks": {},
        "deprecated": [],
        "notes": "GCS service account needs roles/pubsub.publisher on the topic. "
                 "Use google_storage_project_service_account data source to get the SA email.",
    },

    # ── Pub/Sub ────────────────────────────────────────────────────────────────
    "google_pubsub_schema": {
        "required": ["name", "type", "definition"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "type: AVRO or PROTOCOL_BUFFER. definition is JSON-encoded schema.",
    },
    "google_pubsub_topic": {
        "required": ["name"],
        "optional": ["project", "labels", "kms_key_name", "message_retention_duration"],
        "blocks": {
            "message_storage_policy": ["allowed_persistence_regions"],
            "schema_settings": ["schema", "encoding"],
        },
        "deprecated": [],
        "notes": "schema_settings.encoding: JSON or BINARY.",
    },
    "google_pubsub_subscription": {
        "required": ["name", "topic"],
        "optional": [
            "project", "labels", "ack_deadline_seconds", "message_retention_duration",
            "retain_acked_messages", "filter", "enable_message_ordering",
            "enable_exactly_once_delivery",
        ],
        "blocks": {
            "expiration_policy": ["ttl"],
            "dead_letter_policy": ["dead_letter_topic", "max_delivery_attempts"],
            "retry_policy": ["minimum_backoff", "maximum_backoff"],
            "push_config": ["push_endpoint", "attributes", "oidc_token.service_account_email",
                            "oidc_token.audience", "no_wrapper.write_metadata"],
            "bigquery_config": ["table", "use_topic_schema", "use_table_schema",
                                "write_metadata", "drop_unknown_fields", "service_account_email"],
            "cloud_storage_config": ["bucket", "filename_prefix", "filename_suffix",
                                     "filename_datetime_format", "max_duration", "max_bytes",
                                     "max_messages", "state", "avro_config.write_metadata",
                                     "avro_config.use_topic_schema"],
        },
        "deprecated": [],
        "notes": "Only one of push_config/bigquery_config/cloud_storage_config can be set. "
                 "expiration_policy.ttl = '' means never expire.",
    },
    "google_pubsub_topic_iam_binding": {
        "required": ["topic", "role", "members"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },
    "google_pubsub_subscription_iam_binding": {
        "required": ["subscription", "role", "members"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },

    # ── Spanner ────────────────────────────────────────────────────────────────
    "google_spanner_instance": {
        "required": ["name", "config", "display_name"],
        "optional": ["project", "labels", "num_nodes", "processing_units"],
        "blocks": {
            "autoscaling_config": [
                "autoscaling_limits.min_processing_units", "autoscaling_limits.max_processing_units",
                "autoscaling_limits.min_nodes", "autoscaling_limits.max_nodes",
                "autoscaling_targets.high_priority_cpu_utilization_percent",
                "autoscaling_targets.storage_utilization_percent",
            ],
        },
        "deprecated": [],
        "notes": "num_nodes and processing_units are mutually exclusive. "
                 "Use processing_units (100 PU = 1 node) unless specifying whole nodes. "
                 "config examples: regional-us-central1, nam4.",
    },
    "google_spanner_database": {
        "required": ["name", "instance"],
        "optional": [
            "project", "ddl", "version_retention_period", "deletion_protection",
            "enable_drop_protection", "database_dialect",
        ],
        "blocks": {"encryption_config": ["kms_key_name"]},
        "deprecated": [],
        "notes": "database_dialect: GOOGLE_STANDARD_SQL or POSTGRESQL. "
                 "deletion_protection is Terraform-level only.",
    },
    "google_spanner_instance_iam_binding": {
        "required": ["instance", "role", "members"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },
    "google_spanner_database_iam_binding": {
        "required": ["instance", "database", "role", "members"],
        "optional": ["project"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },

    # ── Dataproc ───────────────────────────────────────────────────────────────
    "google_dataproc_cluster": {
        "required": ["name"],
        "optional": ["project", "region", "labels", "graceful_decommission_timeout"],
        "blocks": {
            "cluster_config": [
                "staging_bucket", "temp_bucket",
                "master_config.num_instances", "master_config.machine_type",
                "master_config.disk_config.boot_disk_type", "master_config.disk_config.boot_disk_size_gb",
                "worker_config.num_instances", "worker_config.machine_type",
                "worker_config.disk_config.boot_disk_type", "worker_config.disk_config.boot_disk_size_gb",
                "preemptible_worker_config.num_instances", "preemptible_worker_config.preemptibility",
                "software_config.image_version", "software_config.override_properties",
                "software_config.optional_components",
                "gce_cluster_config.zone", "gce_cluster_config.network", "gce_cluster_config.subnetwork",
                "gce_cluster_config.internal_ip_only", "gce_cluster_config.service_account",
                "gce_cluster_config.service_account_scopes", "gce_cluster_config.tags",
                "gce_cluster_config.metadata",
                "gce_cluster_config.shielded_instance_config.enable_secure_boot",
                "gce_cluster_config.shielded_instance_config.enable_vtpm",
                "gce_cluster_config.shielded_instance_config.enable_integrity_monitoring",
                "encryption_config.kms_key_name",
                "autoscaling_config.policy_uri",
                "lifecycle_config.idle_delete_ttl", "lifecycle_config.auto_delete_time",
                "initialization_action.script", "initialization_action.timeout_sec",
                "metastore_config.dataproc_metastore_service",
                "endpoint_config.enable_http_port_access",
            ],
        },
        "deprecated": [],
        "notes": "[SECURITY] internal_ip_only should default to true. "
                 "preemptibility values: PREEMPTIBLE, SPOT, NON_PREEMPTIBLE. "
                 "master_num_instances: 1 (standard) or 3 (HA).",
    },
    "google_dataproc_cluster_iam_binding": {
        "required": ["cluster", "role", "members"],
        "optional": ["project", "region"],
        "blocks": {},
        "deprecated": [],
        "notes": "",
    },

    # ── Cloud Composer ─────────────────────────────────────────────────────────
    "google_composer_environment": {
        "required": ["name"],
        "optional": ["project", "region", "labels"],
        "blocks": {
            "config": [
                "environment_size",
                "resilience_mode",
                "node_config.network", "node_config.subnetwork", "node_config.service_account",
                "node_config.tags",
                "node_config.ip_allocation_policy.use_ip_aliases",
                "node_config.ip_allocation_policy.cluster_secondary_range_name",
                "node_config.ip_allocation_policy.services_secondary_range_name",
                "software_config.image_version", "software_config.airflow_config_overrides",
                "software_config.env_variables", "software_config.pypi_packages",
                "workloads_config.scheduler.cpu", "workloads_config.scheduler.memory_gb",
                "workloads_config.scheduler.storage_gb", "workloads_config.scheduler.count",
                "workloads_config.web_server.cpu", "workloads_config.web_server.memory_gb",
                "workloads_config.web_server.storage_gb",
                "workloads_config.worker.cpu", "workloads_config.worker.memory_gb",
                "workloads_config.worker.storage_gb", "workloads_config.worker.min_count",
                "workloads_config.worker.max_count",
                "workloads_config.triggerer.cpu", "workloads_config.triggerer.memory_gb",
                "workloads_config.triggerer.count",
                "private_environment_config.enable_private_endpoint",
                "private_environment_config.master_ipv4_cidr_block",
                "private_environment_config.cloud_sql_ipv4_cidr_block",
                "private_environment_config.cloud_composer_network_ipv4_cidr_block",
                "encryption_config.kms_key_name",
                "maintenance_window.start_time", "maintenance_window.end_time",
                "maintenance_window.recurrence",
                "web_server_network_access_control.allowed_ip_range.value",
                "web_server_network_access_control.allowed_ip_range.description",
            ],
        },
        "deprecated": ["config.node_count", "config.software_config.scheduler_count",
                       "config.node_config.zone", "config.node_config.machine_type",
                       "config.node_config.disk_size_gb"],
        "notes": "Composer 2 only. Use workloads_config for sizing, NOT node_count/machine_type. "
                 "environment_size: ENVIRONMENT_SIZE_SMALL/MEDIUM/LARGE. "
                 "resilience_mode: STANDARD_RESILIENCE or HIGH_RESILIENCE.",
    },

    # ── Dataflow ───────────────────────────────────────────────────────────────
    "google_dataflow_flex_template_job": {
        "required": ["name", "container_spec_gcs_path"],
        "optional": [
            "project", "region", "labels", "temp_location", "on_delete",
            "max_workers", "service_account_email", "network", "subnetwork",
            "machine_type", "launcher_machine_type", "sdk_container_image",
            "ip_configuration", "enable_streaming_engine", "autoscaling_algorithm",
            "skip_wait_on_job_termination", "kms_key_name", "additional_experiments",
            "parameters", "transform_name_mapping",
        ],
        "blocks": {},
        "deprecated": [],
        "notes": "MUST set provider = google-beta in the resource block. "
                 "ip_configuration: WORKER_IP_PUBLIC or WORKER_IP_PRIVATE. "
                 "[SECURITY] prefer WORKER_IP_PRIVATE.",
    },

    # ── IAM / Common ───────────────────────────────────────────────────────────
    "google_service_account": {
        "required": ["account_id"],
        "optional": ["project", "display_name", "description"],
        "blocks": {},
        "deprecated": [],
        "notes": "account_id max 30 chars. Use snake_case.",
    },
    "google_project_iam_member": {
        "required": ["project", "role", "member"],
        "optional": [],
        "blocks": {"condition": ["title", "description", "expression"]},
        "deprecated": [],
        "notes": "member format: serviceAccount:email, user:email, group:email.",
    },
    "google_project_service": {
        "required": ["project", "service"],
        "optional": ["disable_on_destroy", "disable_dependent_services"],
        "blocks": {},
        "deprecated": [],
        "notes": "Set disable_on_destroy = false in modules to prevent accidental API disablement.",
    },
}

# Product -> relevant resource types mapping
_PRODUCT_RESOURCES: dict[str, list[str]] = {
    "bigquery": [
        "google_bigquery_dataset", "google_bigquery_table",
        "google_bigquery_dataset_iam_binding", "google_bigquery_row_access_policy",
        "google_bigquery_routine",
    ],
    "storage": [
        "google_storage_bucket", "google_storage_bucket_iam_binding",
        "google_storage_notification",
    ],
    "pubsub": [
        "google_pubsub_schema", "google_pubsub_topic", "google_pubsub_subscription",
        "google_pubsub_topic_iam_binding", "google_pubsub_subscription_iam_binding",
    ],
    "spanner": [
        "google_spanner_instance", "google_spanner_database",
        "google_spanner_instance_iam_binding", "google_spanner_database_iam_binding",
    ],
    "dataproc": [
        "google_dataproc_cluster", "google_dataproc_cluster_iam_binding",
    ],
    "composer": ["google_composer_environment"],
    "dataflow": ["google_dataflow_flex_template_job"],
    "common": ["google_service_account", "google_project_iam_member", "google_project_service"],
}

_SECURITY_BASELINE = """
SECURITY BASELINE — apply to every module:
- Storage: public_access_prevention = "enforced" (not "inherited")
- Storage: uniform_bucket_level_access default = true
- Compute/Dataproc: internal_ip_only = true
- Dataflow: ip_configuration = "WORKER_IP_PRIVATE"
- Encryption: emit dynamic encryption block keyed on kms_key_name variable
- IAM: use _iam_binding not _iam_policy; least-privilege roles only
- No hardcoded 0.0.0.0/0 ingress on sensitive ports
- Tags: always emit common_labels local merged with var.labels

COMMON LABELS PATTERN (emit in every module):
  locals {
    common_labels = merge(
      { environment = var.environment, managed_by = "terraform", module = "MODULE_NAME" },
      var.additional_labels,
    )
  }
"""

_OUTPUT_FORMAT_INSTRUCTIONS = """
OUTPUT FORMAT — strict:
Each file MUST use these exact markers:
  ---FILE: repos/{module}/filename.tf---
  {complete HCL content}
  ---ENDFILE---

For EXTEND mode: wrap diffs as:
  ---FILE: repos/{module}/filename.tf---
  --- a/repos/{module}/filename.tf
  +++ b/repos/{module}/filename.tf
  @@ ... @@
  {unified diff hunks}
  ---ENDFILE---

Rules:
1. Output ONLY the file markers and HCL. No prose, no explanations.
2. 2-space indentation everywhere.
3. Blank line between every top-level block.
4. for_each over count for all iterations.
5. dynamic blocks for optional nested blocks (keyed on != null).
6. Every variable must have type, description, and validation where applicable.
7. Every output must have description.
8. Flag issues ONLY as inline HCL comments:
   # [SECURITY]: <issue>
   # [COST]: <issue>
   # [TFLINT]: <issue>
"""


# ── Context builder ────────────────────────────────────────────────────────────

def build_generation_context(
    mode: str,
    target_module: str,
    base_repos: list[str],
    gcp_product: Optional[str] = None,
) -> str:
    """
    Assemble the full context string injected into the generation prompt.
    Order: schemas -> existing patterns -> security baseline -> output format.
    """
    parts: list[str] = []

    # 1. Schema context
    resource_types = _get_relevant_resource_types(gcp_product, base_repos)
    parts.append("=== GROUNDED RESOURCE SCHEMAS (hashicorp/google >= 5.39, < 8) ===")
    parts.append(_format_schema_context(resource_types))
    parts.append("")

    # 2. Existing module patterns
    if base_repos:
        parts.append("=== EXISTING MODULE PATTERNS (match these naming and style conventions) ===")
        for repo_name in base_repos[:2]:
            snippet = _get_module_code_snippet(repo_name)
            if snippet:
                parts.append(f"--- from: {repo_name} ---")
                parts.append(snippet)
        parts.append("")

    # 3. Security baseline
    parts.append("=== SECURITY BASELINE ===")
    parts.append(_SECURITY_BASELINE)
    parts.append("")

    # 4. Output format
    parts.append("=== OUTPUT FORMAT ===")
    parts.append(_OUTPUT_FORMAT_INSTRUCTIONS)

    return "\n".join(parts)


def _get_relevant_resource_types(gcp_product: Optional[str], base_repos: list[str]) -> list[str]:
    """Return resource type list to include in schema context."""
    types: list[str] = []

    if gcp_product and gcp_product in _PRODUCT_RESOURCES:
        types.extend(_PRODUCT_RESOURCES[gcp_product])

    # Heuristic: infer product from repo names if gcp_product not given
    if not types:
        for repo in base_repos:
            for product, rtypes in _PRODUCT_RESOURCES.items():
                if product in repo:
                    types.extend(rtypes)
                    break

    # Always include common resources
    types.extend(_PRODUCT_RESOURCES["common"])

    # Dedup while preserving order
    seen: set[str] = set()
    result = []
    for t in types:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _format_schema_context(resource_types: list[str]) -> str:
    """Format schema entries as compact, LLM-readable text."""
    lines: list[str] = []
    for rt in resource_types:
        schema = GCP_RESOURCE_SCHEMAS.get(rt)
        if not schema:
            continue
        lines.append(f"RESOURCE: {rt}")
        if schema["required"]:
            lines.append(f"  REQUIRED:    {', '.join(schema['required'])}")
        if schema["optional"]:
            lines.append(f"  OPTIONAL:    {', '.join(schema['optional'])}")
        if schema["blocks"]:
            for block, attrs in schema["blocks"].items():
                lines.append(f"  BLOCK {block}: {', '.join(attrs[:6])}")
        if schema["deprecated"]:
            lines.append(f"  DEPRECATED (never use): {', '.join(schema['deprecated'])}")
        if schema.get("notes"):
            lines.append(f"  NOTE: {schema['notes']}")
        lines.append("")
    return "\n".join(lines)


def _get_module_code_snippet(repo_name: str) -> str:
    """
    Read the root main.tf and variables.tf of a repo (at latest tag or main)
    and return a compact excerpt for style grounding.
    Truncated to 2000 chars to stay within context window budget.
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return ""

    tag = get_latest_tag(repo_name) or "main"
    snippets: list[str] = []

    for fname in ["versions.tf", "main.tf", "variables.tf"]:
        content = get_file_at_tag(repo_name, tag, fname)
        if content:
            # Remove Apache license header
            content = re.sub(r'/\*\*.*?Copyright.*?\*/\s*', '', content, flags=re.DOTALL)
            snippets.append(f"# {fname}\n{content[:600]}")

    combined = "\n\n".join(snippets)
    return combined[:2000]


# ── Post-generation validation ─────────────────────────────────────────────────

def validate_hcl_files(files: list[GeneratedFile]) -> list[ValidationNote]:
    """
    Python-side validation of generated HCL.
    Checks security baselines, HCL grammar rules, and variable completeness.
    Does NOT run terraform validate (no binary required).
    """
    notes: list[ValidationNote] = []

    for f in files:
        if f.is_diff:
            notes.extend(_validate_diff(f))
            continue
        notes.extend(_check_security(f))
        notes.extend(_check_lint(f))
        notes.extend(_check_variable_completeness(f))
        notes.extend(_check_output_completeness(f))
        notes.extend(_check_versions(f))

    return notes


def _check_security(f: GeneratedFile) -> list[ValidationNote]:
    notes = []
    c = f.content

    if '"inherited"' in c and "public_access_prevention" in c:
        notes.append(ValidationNote(
            level="security", file=f.path,
            message='public_access_prevention = "inherited" — change to "enforced"',
        ))
    if re.search(r'internal_ip_only\s*=\s*false', c):
        notes.append(ValidationNote(
            level="security", file=f.path,
            message="internal_ip_only = false exposes VMs to public internet",
        ))
    if "WORKER_IP_PUBLIC" in c:
        notes.append(ValidationNote(
            level="security", file=f.path,
            message="Dataflow ip_configuration = WORKER_IP_PUBLIC — prefer WORKER_IP_PRIVATE",
        ))
    if re.search(r'"0\.0\.0\.0/0"', c):
        notes.append(ValidationNote(
            level="security", file=f.path,
            message="0.0.0.0/0 CIDR found — verify this is intentional",
        ))
    if re.search(r'deletion_protection\s*=\s*false', c) and "variables.tf" not in f.path:
        notes.append(ValidationNote(
            level="security", file=f.path,
            message="deletion_protection = false on a non-variable resource — consider setting true",
        ))
    return notes


def _check_lint(f: GeneratedFile) -> list[ValidationNote]:
    notes = []
    c = f.content

    if re.search(r'\bcount\s*=\s*\d', c) and "for_each" not in c:
        notes.append(ValidationNote(
            level="lint", file=f.path,
            message="count= found — prefer for_each for resource iteration",
        ))
    if re.search(r'^\s*depends_on\s*=', c, re.MULTILINE):
        notes.append(ValidationNote(
            level="lint", file=f.path,
            message="depends_on found — prefer implicit dependencies via attribute references",
        ))
    # Hardcoded project IDs (8+ digit numbers in strings that aren't vars)
    if re.search(r'"[0-9]{8,}"', c):
        notes.append(ValidationNote(
            level="lint", file=f.path,
            message="Possible hardcoded project/numeric ID — use var.project_id instead",
        ))
    # Inline sensitive values
    if re.search(r'(?:password|secret|key)\s*=\s*"[^$][^"]{4,}"', c, re.IGNORECASE):
        notes.append(ValidationNote(
            level="security", file=f.path,
            message="Possible hardcoded secret/password/key — use var.* with sensitive=true",
        ))
    return notes


def _check_variable_completeness(f: GeneratedFile) -> list[ValidationNote]:
    if "variables.tf" not in f.path:
        return []
    notes = []
    c = f.content
    # Find variable blocks
    var_blocks = re.findall(r'variable\s+"([^"]+)"\s*\{([^}]*)\}', c, re.DOTALL)
    for name, body in var_blocks:
        if "description" not in body:
            notes.append(ValidationNote(
                level="lint", file=f.path,
                message=f'Variable "{name}" is missing description',
            ))
        if "type" not in body:
            notes.append(ValidationNote(
                level="lint", file=f.path,
                message=f'Variable "{name}" is missing explicit type',
            ))
    return notes


def _check_output_completeness(f: GeneratedFile) -> list[ValidationNote]:
    if "outputs.tf" not in f.path:
        return []
    notes = []
    c = f.content
    out_blocks = re.findall(r'output\s+"([^"]+)"\s*\{([^}]*)\}', c, re.DOTALL)
    for name, body in out_blocks:
        if "description" not in body:
            notes.append(ValidationNote(
                level="lint", file=f.path,
                message=f'Output "{name}" is missing description',
            ))
    return notes


def _check_versions(f: GeneratedFile) -> list[ValidationNote]:
    if "versions.tf" not in f.path:
        return []
    notes = []
    c = f.content
    # Exact pin detection: version = "5.40.0" instead of ~> or >= constraint
    if re.search(r'version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', c):
        notes.append(ValidationNote(
            level="lint", file=f.path,
            message="Exact version pin in required_providers — use ~> or >= constraint for modules",
        ))
    if "required_version" not in c:
        notes.append(ValidationNote(
            level="lint", file=f.path,
            message="versions.tf is missing required_version constraint",
        ))
    return notes


def _validate_diff(f: GeneratedFile) -> list[ValidationNote]:
    notes = []
    # Verify it looks like a real unified diff
    if not (f.content.startswith("---") or f.content.startswith("@@")):
        notes.append(ValidationNote(
            level="info", file=f.path,
            message="File marked as diff but content does not start with --- or @@ markers",
        ))
    return notes


# ── Output parser ──────────────────────────────────────────────────────────────

def parse_generated_files(raw_output: str, target_module: str) -> list[GeneratedFile]:
    """
    Parse LLM output containing ---FILE: path---..---ENDFILE--- markers
    into a list of GeneratedFile objects.

    Falls back to extracting fenced code blocks if no markers are found.
    """
    files: list[GeneratedFile] = []

    # Primary: ---FILE: path--- ... ---ENDFILE---
    marker_pattern = re.compile(
        r'---FILE:\s*(.+?)---\s*\n(.*?)---ENDFILE---',
        re.DOTALL,
    )
    for m in marker_pattern.finditer(raw_output):
        path = m.group(1).strip()
        content = _strip_code_fences(m.group(2).strip())
        is_diff = content.startswith("---") or content.startswith("@@")
        files.append(GeneratedFile(path=path, content=content, is_diff=is_diff))

    if files:
        return files

    # Fallback: extract ```hcl ... ``` fenced blocks and guess filenames
    fence_pattern = re.compile(r'```(?:hcl|terraform)?\s*\n(.*?)```', re.DOTALL)
    # Look for file path hints in preceding text
    path_hint = re.compile(r'(?:FILE|file|path):\s*([^\n]+\.tf)')

    raw_lines = raw_output.split("\n")
    last_hint = f"repos/{target_module}/main.tf"
    hint_idx: dict[int, str] = {}

    for i, line in enumerate(raw_lines):
        ph = path_hint.search(line)
        if ph:
            hint_idx[i] = ph.group(1).strip()

    for fm in fence_pattern.finditer(raw_output):
        # Find the last path hint before this match
        start_line = raw_output[:fm.start()].count("\n")
        best_path = last_hint
        for li, hint in hint_idx.items():
            if li <= start_line:
                best_path = hint

        content = fm.group(1).strip()
        files.append(GeneratedFile(
            path=best_path,
            content=content,
            is_diff=False,
            description="(fallback parse — no FILE markers found)",
        ))

    return files


def _strip_code_fences(text: str) -> str:
    """Remove ```hcl / ``` wrappers if present."""
    text = re.sub(r'^```(?:hcl|terraform)?\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    return text.strip()
