"""
http_clients.py — Split proxy trust by destination (Priority 6 hygiene).

The recurring corporate-proxy bug: a client built with the httpx/OpenAI-SDK
default (trust_env=True) silently routes localhost traffic (Ollama, Kroki)
through the corporate HTTP_PROXY/HTTPS_PROXY, which then TLS-intercepts or
simply can't reach localhost. External official sources (cloud.google.com,
the Terraform registry, Confluence) are the opposite case — they legitimately
need that same proxy for egress.

One helper, used everywhere, so the choice is explicit at every call site
instead of relying on whatever a library's default happens to be.
"""
from __future__ import annotations

import httpx


def local_client(*, is_async: bool = True, **kw) -> httpx.AsyncClient | httpx.Client:
    """Ollama, Kroki, or any other localhost service — never proxied."""
    cls = httpx.AsyncClient if is_async else httpx.Client
    return cls(trust_env=False, **kw)


def external_client(*, is_async: bool = True, **kw) -> httpx.AsyncClient | httpx.Client:
    """cloud.google.com, registry.terraform.io, Confluence — proxied normally."""
    cls = httpx.AsyncClient if is_async else httpx.Client
    return cls(trust_env=True, **kw)
