"""
gcp_service_detector.py — Compatibility shim.

Routes scan_gcp_service_features() to cloud_service_scanner.scan_cloud_service(),
auto-detecting the cloud provider (google / aws / azurerm) from the repo's versions.tf.

Used by ga_orchestrator and ga_router (which import this function by name).
"""
from __future__ import annotations

from backend.ga_workflow.ga_models import GCPServiceScanResult, WorkflowRun
from backend.ga_workflow.ga_detector import detect_terraform_provider
from backend.agent.tools.git_tools import get_latest_tag


async def scan_gcp_service_features(
    repo_name: str,
    run: WorkflowRun,
    days_back: int = 180,
) -> GCPServiceScanResult:
    """
    Scan the cloud service associated with this repo for new GA features.
    Auto-detects provider (google/aws/azurerm) from versions.tf.
    """
    from backend.ga_workflow.cloud_service_scanner import scan_cloud_service

    current_tag = get_latest_tag(repo_name) or "main"
    provider_key = detect_terraform_provider(repo_name, current_tag)

    return await scan_cloud_service(
        repo_name=repo_name,
        provider_key=provider_key,
        run=run,
        days_back=days_back,
    )
