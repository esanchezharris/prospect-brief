"""Disk cache for fetched documents, keyed by sha256(url). This is the audit trail."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import Document


def cache_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


class DocumentCache:
    def __init__(self, root: Path):
        self.root = root
        self.docs = root / "docs"
        self.raw = root / "raw"
        self.docs.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        return self.docs / f"{key}.json"

    def get(self, url: str) -> Document | None:
        p = self.path(cache_key(url))
        if not p.exists():
            return None
        return Document.model_validate_json(p.read_text())

    def put(self, doc: Document, raw: bytes | None = None) -> None:
        self.path(doc.cache_key).write_text(doc.model_dump_json(indent=1))
        if raw is not None:
            ext = ".pdf" if "pdf" in doc.content_type else ".html"
            (self.raw / f"{doc.cache_key}{ext}").write_bytes(raw)

    def get_json(self, namespace: str, key: str) -> dict | None:
        p = self.root / namespace / f"{cache_key(key)}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def put_json(self, namespace: str, key: str, data: dict) -> None:
        d = self.root / namespace
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cache_key(key)}.json").write_text(json.dumps(data, indent=1))
