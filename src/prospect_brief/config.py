"""Configuration loading. Secrets come from .env; everything else from config/default.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


class Config:
    def __init__(self, data: dict[str, Any], root: Path = ROOT):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        load_dotenv(ROOT / ".env")
        with open(path or DEFAULT_CONFIG) as f:
            return cls(yaml.safe_load(f))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def contact_email(self) -> str:
        return os.environ.get("CONTACT_EMAIL", "contact-not-set@example.com")

    @property
    def user_agent(self) -> str:
        return str(self.get("fetch", "user_agent")).format(contact_email=self.contact_email)

    @property
    def blocklist(self) -> list[str]:
        return [d.lower() for d in self.get("blocklist", default=[])]

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def briefs_dir(self) -> Path:
        return self.root / "briefs"

    @property
    def signals_dir(self) -> Path:
        return self.root / "signals"

    def price(self, model: str) -> tuple[float, float]:
        p = self.get("prices_per_mtok", model, default={"input": 0.0, "output": 0.0})
        return float(p["input"]), float(p["output"])
