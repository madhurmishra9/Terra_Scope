"""
diagrams/hcl_graph.py — Build an ArchGraph from the module's ACTUAL Terraform
resources.

This is what makes auto-added diagrams trustworthy: nodes come from parsed
HCL, never from LLM imagination. Edges are inferred from cross-resource
references (interpolation like
google_sql_database_instance.main.connection_name inside another resource's
body).

Scaffold parser: a dependency-light block scanner. Swap `iter_resources` for a
python-hcl2 parse in production; the ArchGraph contract stays identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .mermaid_render import ArchGraph, Edge, Node

_RESOURCE_RE = re.compile(r'^\s*resource\s+"([\w-]+)"\s+"([\w-]+)"\s*\{', re.MULTILINE)
_REF_RE = re.compile(r'\b([a-z][\w]*_[\w]+)\.([\w-]+)\b')

# Human-friendly labels for common GCP kinds; anything unknown falls back to
# the raw resource type so nothing is silently dropped.
_LABELS = {
    "google_storage_bucket": "GCS Bucket",
    "google_sql_database_instance": "Cloud SQL",
    "google_cloud_run_v2_service": "Cloud Run",
    "google_compute_network": "VPC",
    "google_compute_subnetwork": "Subnet",
    "google_service_account": "Service Account",
    "google_kms_crypto_key": "KMS Key",
    "google_compute_forwarding_rule": "PSC Endpoint",
    "google_bigquery_dataset": "BigQuery Dataset",
    "google_bigquery_table": "BigQuery Table",
    "google_pubsub_topic": "Pub/Sub Topic",
    "google_pubsub_subscription": "Pub/Sub Subscription",
}


@dataclass
class ParsedResource:
    rtype: str
    name: str
    body: str

    @property
    def node_id(self) -> str:
        return f"{self.rtype}_{self.name}".replace("-", "_")


def iter_resources(hcl: str) -> list[ParsedResource]:
    """Scan top-level resource blocks with brace matching."""
    out: list[ParsedResource] = []
    for m in _RESOURCE_RE.finditer(hcl):
        depth, i = 1, m.end()
        while i < len(hcl) and depth:
            depth += {"{": 1, "}": -1}.get(hcl[i], 0)
            i += 1
        out.append(ParsedResource(rtype=m.group(1), name=m.group(2), body=hcl[m.end():i]))
    return out


def graph_from_hcl(files: dict[str, str]) -> ArchGraph:
    hcl = "\n".join(files.values())
    resources = iter_resources(hcl)
    by_key = {(r.rtype, r.name): r for r in resources}

    nodes = [
        Node(id=r.node_id, label=f"{_LABELS.get(r.rtype, r.rtype)}\\n({r.name})", kind=r.rtype)
        for r in resources
    ]

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for r in resources:
        for rtype, rname in _REF_RE.findall(r.body):
            target = by_key.get((rtype, rname))
            if target and target is not r:
                key = (r.node_id, target.node_id)
                if key not in seen:
                    seen.add(key)
                    edges.append(Edge(src=r.node_id, dst=target.node_id, label="ref"))
    return ArchGraph(nodes=nodes, edges=edges)
