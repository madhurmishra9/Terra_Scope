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


_runtime_ctx_cache: dict[str, int] = {}


def effective_context_tokens() -> int:
    """The context window the RUNNING Ollama instance actually enforces.

    `terrascope.config.yaml`'s `llm.context_window` states an intent, but
    Ollama serves the model with its own `num_ctx` (default 4096 unless
    OLLAMA_CONTEXT_LENGTH / a Modelfile raises it). Prompts longer than the
    real window don't error — Ollama silently truncates from the FRONT,
    which cuts away injected grounding material while keeping the trailing
    instruction. The result reads like 'the material doesn't cover this'.

    Probe GET /api/ps for the loaded model's context_length; fall back to
    the configured value when the model isn't loaded yet or the probe fails.
    Cached per (base_url, model) — the runtime window doesn't change while
    the server is up.
    """
    try:
        cfg = get_config()
        key = f"{cfg.llm.base_url}|{cfg.llm.model}"
        if key in _runtime_ctx_cache:
            return _runtime_ctx_cache[key]
        import httpx
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            r = client.get(cfg.llm.base_url.rstrip("/") + "/api/ps")
            r.raise_for_status()
            for m in r.json().get("models", []):
                if m.get("name") == cfg.llm.model or m.get("model") == cfg.llm.model:
                    ctx = int(m.get("context_length") or 0)
                    if ctx > 0:
                        _runtime_ctx_cache[key] = ctx
                        return ctx
        return int(getattr(cfg.llm, "context_window", 8192) or 8192)
    except Exception:
        try:
            return int(get_config().llm.context_window or 8192)
        except Exception:
            return 8192


def grounding_char_budget(reserved_tokens: int = 1500) -> int:
    """Chars of grounding material that safely fit the live context window,
    reserving room for instructions + the answer. ~3.2 chars/token."""
    return max(3000, int((effective_context_tokens() - reserved_tokens) * 3.2))


def pydantic_ai_model_settings() -> dict:
    """Model settings for pydantic_ai Agents (the Repo Chat / question-engine
    path) — same thinking-off switches as llm_extra_body(), delivered through
    pydantic_ai's ModelSettings.extra_body passthrough."""
    body = llm_extra_body()
    return {"extra_body": body} if body else {}


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
