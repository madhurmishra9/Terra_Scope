"""
llm_options.py — Shared per-request options for local-LLM calls.

Why this exists: every LLM call in TerraScope is grounded — the provider
schema, crawled docs, or actual module code is injected into the prompt and
the model is told to answer ONLY from it. Thinking-capable models (qwen3
family, deepseek-r1, ...) prepend a long reasoning trace to every answer,
which on CPU-only inference adds *minutes* of latency per call while adding
nothing to grounded extraction/generation tasks.

Worse than latency: through the OpenAI-compat endpoint the reasoning trace
is routed to `message.reasoning`, NOT `message.content` — so when the trace
exceeds `max_tokens`, `content` comes back EMPTY and the caller sees a
grounded question answered with nothing at all.

`llm.disable_thinking: true` (the default) turns thinking off, three ways
at once (verified against Ollama 0.31 + qwen3.5:9b: 60s+/empty-content with
thinking -> 14s/'OK' without):

  1. `extra_body={"reasoning_effort": "none"}` — the switch Ollama's
     OpenAI-compat endpoint actually maps to think=false.
  2. `extra_body={"think": False}` — Ollama's native-API switch; ignored by
     the compat endpoint today (unknown JSON fields are dropped by Go's
     unmarshal) but honoured if passthrough is added later.
  3. A `/no_think` system message — the classic-qwen3 documented soft
     switch, applied only when the configured model is a known thinking
     family, so non-thinking models never see the directive.

Usage at a call site:

    from backend.llm_options import llm_extra_body, nothink_messages
    resp = await client.chat.completions.create(
        model=cfg.llm.model,
        messages=nothink_messages([{"role": "user", "content": prompt}]),
        extra_body=llm_extra_body(),
        ...
    )
"""
from __future__ import annotations

from backend.config import get_config

# Model-name substrings whose chat templates honour the /no_think soft switch.
_SOFT_SWITCH_FAMILIES = ("qwen3",)


def _disabled() -> bool:
    try:
        return bool(get_config().llm.disable_thinking)
    except Exception:
        return True  # config unavailable -> favour the fast path


def llm_extra_body() -> dict:
    """Extra JSON fields for chat.completions.create. Empty dict when
    thinking is allowed, so call sites can pass it unconditionally."""
    if not _disabled():
        return {}
    return {"reasoning_effort": "none", "think": False}


def nothink_messages(messages: list[dict]) -> list[dict]:
    """Prepend the /no_think soft switch for model families that use it.
    Returns the messages unchanged for other models or when thinking is on."""
    if not _disabled():
        return messages
    try:
        model = get_config().llm.model.lower()
    except Exception:
        return messages
    if not any(fam in model for fam in _SOFT_SWITCH_FAMILIES):
        return messages
    if messages and messages[0].get("role") == "system":
        # Don't stack a second system message — annotate the existing one.
        first = dict(messages[0])
        if "/no_think" not in first.get("content", ""):
            first["content"] = f"{first['content']}\n/no_think"
        return [first] + list(messages[1:])
    return [{"role": "system", "content": "/no_think"}] + list(messages)
