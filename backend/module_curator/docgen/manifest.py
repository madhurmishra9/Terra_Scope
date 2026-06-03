"""
manifest.py — Idempotency tracking for published Confluence pages.

The manifest maps product_name → ProductEntry (parent_page_id, per-doc-type
page IDs, space key, last-published timestamp) and is persisted as JSON at
./data/docgen_manifest.json.

When the pipeline runs twice for the same product it uses the stored page IDs
to *update* rather than *duplicate* Confluence pages.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

_DEFAULT_PATH = Path("data/docgen_manifest.json")


class ProductEntry(BaseModel):
    parent_page_id: Optional[str] = None
    pages:          dict[str, str] = {}   # doc_type → page_id
    space_key:      str = ""
    last_published: str = ""


class DocgenManifest:
    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path = path
        self._data: dict[str, ProductEntry] = {}
        self._load()

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, product: str) -> Optional[ProductEntry]:
        return self._data.get(product)

    # ── Write ─────────────────────────────────────────────────────────────────

    def set(
        self,
        product: str,
        *,
        parent_page_id: Optional[str] = None,
        pages: Optional[dict[str, str]] = None,
        space_key: str = "",
    ) -> None:
        entry = self._data.get(product) or ProductEntry()
        if parent_page_id is not None:
            entry.parent_page_id = parent_page_id
        if pages:
            entry.pages.update(pages)
        if space_key:
            entry.space_key = space_key
        entry.last_published = datetime.now(timezone.utc).isoformat()
        self._data[product] = entry

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.model_dump() for k, v in self._data.items()}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {k: ProductEntry.model_validate(v) for k, v in raw.items()}
        except Exception:
            self._data = {}
