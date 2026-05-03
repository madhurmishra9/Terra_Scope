"""
issue_tools.py — Pattern-matched issue KB + solution retrieval.
Fast deterministic answers for known GCP/Terraform errors.
No LLM hallucination risk for common issues.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Optional

from backend.agent.models import IssueSolution


# ── Embedded Knowledge Base ────────────────────────────────────────────────────
# In production, load from backend/knowledge/issues_kb.json
# Listed here for portability.

ISSUES_KB: list[dict] = [
    # ── IAM / Permission Errors ───────────────────────────────────────────────
    {
        "id": "iam_403_bigquery",
        "patterns": [r"403.*bigquery", r"bigquery.*403", r"Error 403.*BigQuery",
                     r"googleapi: Error 403", r"accessDenied.*bigquery"],
        "title": "BigQuery API Permission Denied (403)",
        "gcp_product": "bigquery",
        "root_cause": "The service account used by Terraform lacks BigQuery permissions, or the BigQuery API is not enabled in the project.",
        "affected_resource_type": "google_bigquery_dataset",
        "solution_steps": [
            "Enable the BigQuery API: run the gcloud command below",
            "Grant the service account the required IAM role (see below)",
            "Verify the service account email matches the one in variables.tf",
            "Re-run: terraform init && terraform plan",
        ],
        "gcloud_commands": [
            "gcloud services enable bigquery.googleapis.com --project=YOUR_PROJECT_ID",
            "gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member='serviceAccount:SA_EMAIL' --role='roles/bigquery.admin'",
        ],
        "terraform_fix": None,
        "provider_version_note": None,
    },
    {
        "id": "iam_403_storage",
        "patterns": [r"403.*storage", r"storage.*403", r"googleapi: Error 403.*storage",
                     r"accessDenied.*storage"],
        "title": "Cloud Storage Permission Denied (403)",
        "gcp_product": "storage",
        "root_cause": "The service account lacks Storage permissions or the Cloud Storage API is not enabled.",
        "affected_resource_type": "google_storage_bucket",
        "solution_steps": [
            "Enable Cloud Storage API",
            "Grant roles/storage.admin or roles/storage.objectAdmin to the service account",
            "Check bucket-level IAM policies if using fine-grained access",
            "Re-run terraform plan",
        ],
        "gcloud_commands": [
            "gcloud services enable storage.googleapis.com --project=YOUR_PROJECT_ID",
            "gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member='serviceAccount:SA_EMAIL' --role='roles/storage.admin'",
        ],
        "terraform_fix": None,
        "provider_version_note": None,
    },
    {
        "id": "iam_403_dataflow",
        "patterns": [r"403.*dataflow", r"dataflow.*403", r"dataflow.*permission"],
        "title": "Dataflow Permission Denied (403)",
        "gcp_product": "dataflow",
        "root_cause": "Service account missing Dataflow Worker role, or Dataflow/Storage APIs not enabled.",
        "affected_resource_type": "google_dataflow_job",
        "solution_steps": [
            "Enable the Dataflow API and Storage API",
            "Grant roles/dataflow.admin to the Terraform SA",
            "Grant roles/dataflow.worker to the Dataflow runtime SA",
            "Ensure the GCS temp bucket exists before running Dataflow jobs",
        ],
        "gcloud_commands": [
            "gcloud services enable dataflow.googleapis.com --project=YOUR_PROJECT_ID",
            "gcloud services enable storage.googleapis.com --project=YOUR_PROJECT_ID",
            "gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member='serviceAccount:SA_EMAIL' --role='roles/dataflow.admin'",
        ],
        "terraform_fix": None,
        "provider_version_note": None,
    },
    # ── Provider Version Errors ───────────────────────────────────────────────
    {
        "id": "provider_version_conflict",
        "patterns": [r"provider.*hashicorp/google.*required", r"required_providers.*google",
                     r"no available releases match", r"provider version constraints",
                     r"lock file.*provider"],
        "title": "Google Provider Version Conflict",
        "gcp_product": "all",
        "root_cause": "The module's required_providers version constraint conflicts with the root module's .terraform.lock.hcl or another module's constraint.",
        "affected_resource_type": None,
        "solution_steps": [
            "Check versions.tf for the required_providers constraint",
            "Run terraform init -upgrade to resolve to latest compatible version",
            "Update .terraform.lock.hcl and commit it",
            "If conflict persists, align version constraints across all modules",
        ],
        "gcloud_commands": [],
        "terraform_fix": """terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.0, < 6.0"  # Use a range, not a pinned version
    }
  }
}""",
        "provider_version_note": "Use version ranges (>= X.Y) not exact pins (= X.Y.Z) in module required_providers.",
    },
    # ── State Errors ─────────────────────────────────────────────────────────
    {
        "id": "state_lock",
        "patterns": [r"Error acquiring the state lock", r"state.*locked",
                     r"Lock Info", r"lock.*ID.*terraform"],
        "title": "Terraform State Lock",
        "gcp_product": "all",
        "root_cause": "A previous terraform operation did not release the state lock (e.g., killed mid-apply, network interruption with GCS backend).",
        "affected_resource_type": None,
        "solution_steps": [
            "Verify no other terraform process is running for this workspace",
            "If safe, force-unlock using the Lock ID shown in the error",
            "If using GCS backend, check the lock file in your state bucket",
            "Never force-unlock if another apply is genuinely in progress",
        ],
        "gcloud_commands": [],
        "terraform_fix": "terraform force-unlock LOCK_ID",
        "provider_version_note": None,
    },
    {
        "id": "state_drift",
        "patterns": [r"terraform plan.*to destroy", r"unexpected difference",
                     r"resource.*was unexpectedly modified", r"state.*out of sync"],
        "title": "State Drift Detected",
        "gcp_product": "all",
        "root_cause": "A GCP resource was modified outside of Terraform (manually via console/gcloud), causing Terraform state to diverge from actual infrastructure.",
        "affected_resource_type": None,
        "solution_steps": [
            "Run terraform refresh to update state from real GCP resources",
            "Review the plan carefully — do NOT apply blindly",
            "Use terraform import if a resource was created outside Terraform",
            "Set up policy constraints to prevent manual resource changes",
        ],
        "gcloud_commands": [],
        "terraform_fix": "terraform refresh\nterraform plan  # review before apply",
        "provider_version_note": None,
    },
    # ── API Not Enabled ───────────────────────────────────────────────────────
    {
        "id": "api_not_enabled",
        "patterns": [r"API not enabled", r"has not been used in project",
                     r"enable the API", r"SERVICE_DISABLED",
                     r"googleapis.com.*not enabled"],
        "title": "Required GCP API Not Enabled",
        "gcp_product": "all",
        "root_cause": "The GCP API required by this Terraform resource is not enabled in the target project. The error message will specify which API.",
        "affected_resource_type": None,
        "solution_steps": [
            "Note the API name from the error (e.g., bigquery.googleapis.com)",
            "Enable it via gcloud or add google_project_service to the module",
            "Wait 30-60 seconds for the API enablement to propagate",
            "Re-run terraform plan",
        ],
        "gcloud_commands": [
            "gcloud services enable API_NAME.googleapis.com --project=YOUR_PROJECT_ID",
        ],
        "terraform_fix": """resource "google_project_service" "api" {
  project = var.project_id
  service = "bigquery.googleapis.com"  # Replace with required API
  disable_on_destroy = false
}""",
        "provider_version_note": None,
    },
    # ── Resource Already Exists ───────────────────────────────────────────────
    {
        "id": "resource_already_exists",
        "patterns": [r"already exists", r"googleapi: Error 409",
                     r"resource.*already managed", r"alreadyExists"],
        "title": "Resource Already Exists (409 Conflict)",
        "gcp_product": "all",
        "root_cause": "A GCP resource with the same name/ID already exists in the project, either from a previous run or created manually.",
        "affected_resource_type": None,
        "solution_steps": [
            "If the resource should be managed by Terraform, import it: terraform import",
            "If it's a stale resource, delete it manually first",
            "Check for naming collisions if using dynamic names",
            "Use lifecycle { prevent_destroy = true } to protect critical resources",
        ],
        "gcloud_commands": [],
        "terraform_fix": "terraform import RESOURCE_TYPE.NAME RESOURCE_ID",
        "provider_version_note": None,
    },
    # ── CMEK / Encryption ─────────────────────────────────────────────────────
    {
        "id": "cmek_permission",
        "patterns": [r"CMEK", r"customer.managed.*key", r"cloudkms.*permission",
                     r"cloudkms.*403", r"KMS.*keyRing"],
        "title": "CMEK / Cloud KMS Permission Error",
        "gcp_product": "all",
        "root_cause": "The service account lacks Cloud KMS permissions, or the KMS key/keyring does not exist in the expected location.",
        "affected_resource_type": None,
        "solution_steps": [
            "Enable the Cloud KMS API",
            "Grant roles/cloudkms.cryptoKeyEncrypterDecrypter to the GCP service SA (not Terraform SA)",
            "Verify the key ring and key exist in the correct region",
            "Ensure the key is in the same region as the encrypted resource",
        ],
        "gcloud_commands": [
            "gcloud services enable cloudkms.googleapis.com --project=YOUR_PROJECT_ID",
            "gcloud kms keys list --keyring=YOUR_KEYRING --location=YOUR_REGION --project=YOUR_PROJECT_ID",
        ],
        "terraform_fix": None,
        "provider_version_note": None,
    },
]


def match_known_issue(error_text: str) -> Optional[IssueSolution]:
    """
    Match an error string against the KB. Returns the first matching solution.
    This is deterministic and hallucination-free.
    """
    error_lower = error_text.lower()
    for issue in ISSUES_KB:
        for pattern in issue["patterns"]:
            if re.search(pattern, error_text, re.IGNORECASE):
                return IssueSolution(
                    error_pattern=pattern,
                    root_cause=issue["root_cause"],
                    affected_resource_type=issue.get("affected_resource_type"),
                    solution_steps=issue["solution_steps"],
                    gcloud_commands=issue.get("gcloud_commands", []),
                    related_files=[],
                    terraform_fix=issue.get("terraform_fix"),
                    provider_version_note=issue.get("provider_version_note"),
                )
    return None


def get_all_issue_titles() -> list[str]:
    """Return titles of all issues in KB — for UI display."""
    return [i["title"] for i in ISSUES_KB]


def issues_for_product(gcp_product: str) -> list[dict]:
    """Return issues relevant to a specific GCP product."""
    return [
        i for i in ISSUES_KB
        if i.get("gcp_product") in (gcp_product, "all")
    ]
