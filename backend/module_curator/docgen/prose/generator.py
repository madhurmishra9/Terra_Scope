"""
prose/generator.py — Generate grounded narrative prose using the local LLM.

Invoked by the field binder when field_map.yaml specifies:
  source: llm.<section_hint>

e.g.
  overview:
    source: llm.overview
  security_considerations:
    source: llm.security considerations

The LLM is called at low temperature (0.2) and is strictly grounded in
TFModuleMetadata — no external retrieval or hallucination. Falls back to a
[LLM prose unavailable: ...] placeholder on any error so it never blocks
the rest of the pipeline.
"""
from __future__ import annotations

from backend.module_curator.docgen.metadata.extractor import TFModuleMetadata

_SYSTEM_PROMPT = (
    "You are a concise technical writer generating factual documentation "
    "sections for Terraform modules. Use ONLY the metadata provided. "
    "Write 2–4 sentences. Plain prose — no markdown headers, no bullet points, "
    "no preamble like 'This section...'."
)


def generate_prose(
    section_hint: str,
    meta: TFModuleMetadata,
    doc_type: str = "",
) -> str:
    """
    Return a short grounded prose paragraph for *section_hint*.

    Parameters
    ----------
    section_hint : str
        Context label, e.g. "overview", "security considerations".
    meta         : TFModuleMetadata
        Extracted module metadata used to ground the response.
    doc_type     : str
        Document type (HLD, CPSD, …) for additional framing.
    """
    try:
        from openai import OpenAI
        from backend.config import get_config

        cfg    = get_config()
        client = OpenAI(
            base_url=cfg.llm.base_url.rstrip("/") + "/v1",
            api_key="ollama",
        )
        resp = client.chat.completions.create(
            model=cfg.llm.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": _build_prompt(section_hint, meta, doc_type)},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        return f"[LLM prose unavailable: {exc}]"


def _build_prompt(section_hint: str, meta: TFModuleMetadata, doc_type: str) -> str:
    resources = ", ".join(meta.resource_types[:10]) or "none detected"
    req_inputs = ", ".join(list(meta.required_inputs)[:8]) or "none"
    outputs    = ", ".join(list(meta.outputs)[:6]) or "none"
    doc_label  = f" ({doc_type})" if doc_type else ""

    return (
        f"Write a '{section_hint}' prose section for the "
        f"'{meta.service_name}' Terraform module documentation{doc_label}.\n\n"
        f"Module facts:\n"
        f"- Provider: {meta.provider}\n"
        f"- Description: {meta.description or 'not provided'}\n"
        f"- Resource types: {resources}\n"
        f"- Required inputs: {req_inputs}\n"
        f"- Outputs: {outputs}\n"
        f"- Terraform version constraint: {meta.terraform_version or 'not specified'}\n"
        f"- Provider version constraint: {meta.provider_version or 'not specified'}\n\n"
        f"Write ONLY the prose content (2–4 sentences). Do not use headers or bullets."
    )
