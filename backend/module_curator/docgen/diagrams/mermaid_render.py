"""
diagrams/mermaid_render.py — Architecture diagrams, done deterministically.

Principle: the LLM never draws pixels. It (or `hcl_graph`) emits diagram
SOURCE (Mermaid), a renderer turns source into an image, and a render gate
rejects anything that won't compile.

Three capabilities:
  * generate  : normalized ArchGraph -> diagram source -> rendered image
  * ingest    : existing diagram -> ArchGraph
                  - if source (mermaid/dot): parse it (reliable)
                  - if image: vision-extract (LOSSY -> must be human-verified)
  * conform   : diagram nodes must match the module's actual TF resources
                (set-based, same guard philosophy as everywhere else)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Normalized, tool-agnostic representation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Node:
    id: str
    label: str
    kind: str = ""  # e.g. gcs_bucket, cloud_sql, psc_endpoint


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    label: str = ""


@dataclass
class ArchGraph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def to_mermaid(self) -> str:
        lines = ["flowchart TD"]
        for n in self.nodes:
            lines.append(f'    {n.id}["{n.label}"]')
        for e in self.edges:
            arrow = f'-- {e.label} -->' if e.label else "-->"
            lines.append(f"    {e.src} {arrow} {e.dst}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Generate: source -> rendered image, with a render gate
# --------------------------------------------------------------------------- #
class DiagramRenderError(RuntimeError):
    pass


def render_mermaid(source: str, out_path: Path) -> Path:
    """Render via mermaid-cli (mmdc). Raises DiagramRenderError if it won't
    compile -- that raise IS the render gate. Never ship an unrenderable diagram.
    """
    if shutil.which("mmdc") is None:
        raise DiagramRenderError("mermaid-cli (mmdc) not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as f:
        f.write(source)
        src_file = f.name
    proc = subprocess.run(
        ["mmdc", "-i", src_file, "-o", str(out_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise DiagramRenderError(proc.stderr.strip())
    return out_path


def render_mermaid_kroki(
    source: str,
    out_path: Path,
    kroki_url: str = "https://kroki.io",
    post=None,
) -> Path:
    """Render on demand via a Kroki server (self-hostable via Docker/K8s, which
    suits a corporate/air-gapped setup). POSTs raw mermaid source to
    {kroki_url}/mermaid/{png|svg} and writes the returned bytes.

    `post(url, data) -> bytes` is injected so it can use
    httpx.Client(trust_env=False) behind the corporate proxy, or be mocked.
    Also the render gate: a non-2xx / error response raises.
    """
    fmt = out_path.suffix.lstrip(".").lower() or "png"
    if fmt not in ("png", "svg", "pdf"):
        raise DiagramRenderError(f"Unsupported format: {fmt}")
    url = f"{kroki_url.rstrip('/')}/mermaid/{fmt}"
    if post is None:  # pragma: no cover - real HTTP path
        import httpx

        def post(u, data):
            r = httpx.Client(trust_env=False, timeout=30).post(
                u, content=data.encode(), headers={"Content-Type": "text/plain"}
            )
            r.raise_for_status()
            return r.content

    try:
        content = post(url, source)
    except Exception as e:  # noqa: BLE001 - normalize to the render-gate error
        raise DiagramRenderError(f"Kroki render failed: {e}") from e
    out_path.write_bytes(content)
    return out_path


def render_mermaid_kroki_bytes(
    source: str,
    fmt: str = "png",
    kroki_url: str = "https://kroki.io",
    post=None,
) -> bytes:
    """Same render gate as render_mermaid_kroki, but returns bytes directly
    (via a scratch temp file) instead of requiring an out_path — handy for
    callers (like the diagram resolver) that just want image bytes to attach."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / f"diagram.{fmt}"
        render_mermaid_kroki(source, out_path, kroki_url=kroki_url, post=post)
        return out_path.read_bytes()


def kroki_get_url(source: str, kroki_url: str = "https://kroki.io", fmt: str = "svg") -> str:
    """Build a stateless Kroki GET URL (deflate + urlsafe-base64). Handy to embed
    directly in docs where a live Kroki server is reachable and you don't want to
    commit an image asset."""
    import base64
    import zlib

    compressed = zlib.compress(source.encode("utf-8"), 9)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii")
    return f"{kroki_url.rstrip('/')}/mermaid/{fmt}/{encoded}"


def render_diagram(source: str, out_path: Path, backend: str = "mmdc", **kwargs) -> Path:
    """Unified on-demand entry point. backend='mmdc' (local CLI) or 'kroki' (HTTP)."""
    if backend == "mmdc":
        return render_mermaid(source, out_path)
    if backend == "kroki":
        return render_mermaid_kroki(source, out_path, **kwargs)
    raise DiagramRenderError(f"Unknown backend: {backend}")


# --------------------------------------------------------------------------- #
# Ingest: existing diagram -> ArchGraph
# --------------------------------------------------------------------------- #
_MM_NODE = re.compile(r'(\w+)\["([^"]+)"\]')
_MM_EDGE = re.compile(r'(\w+)\s*--(?:\s*(.*?)\s*-->|>)\s*(\w+)')


def ingest_mermaid(source: str) -> ArchGraph:
    """Parse mermaid SOURCE into the normalized graph. Reliable path -- use this
    whenever the existing diagram is already diagrams-as-code."""
    nodes = [Node(id=i, label=l) for i, l in _MM_NODE.findall(source)]
    known = {n.id for n in nodes}
    edges = []
    for src, label, dst in _MM_EDGE.findall(source):
        if src in known and dst in known:
            edges.append(Edge(src=src, dst=dst, label=label or ""))
    return ArchGraph(nodes=nodes, edges=edges)


def ingest_image(image_path: Path) -> ArchGraph:  # pragma: no cover
    """Vision-extract an ArchGraph from a rendered image (PNG/SVG/draw.io export).

    LOSSY. A vision model returns a best-effort node/edge list; this MUST be
    surfaced to a human for verification before it grounds any new doc. Prefer
    ingest_mermaid whenever source is available.
    """
    raise NotImplementedError(
        "Send the image to a vision model, prompt for a strict nodes/edges JSON, "
        "parse into ArchGraph, then flag the result for human review."
    )


# --------------------------------------------------------------------------- #
# Conform: diagram must match the module's real resources
# --------------------------------------------------------------------------- #
@dataclass
class DiagramConformance:
    undiagrammed_resources: list[str] = field(default_factory=list)  # in code, not drawn
    phantom_nodes: list[str] = field(default_factory=list)           # drawn, not in code

    @property
    def ok(self) -> bool:
        return not (self.undiagrammed_resources or self.phantom_nodes)


def check_diagram_vs_resources(graph: ArchGraph, resource_kinds: set[str]) -> DiagramConformance:
    drawn = {n.kind for n in graph.nodes if n.kind}
    return DiagramConformance(
        undiagrammed_resources=sorted(resource_kinds - drawn),
        phantom_nodes=sorted(drawn - resource_kinds),
    )
