"""Config loading.

`config.yaml` is tracked in git and holds no secrets. `config.local.yaml` is
gitignored and is deep-merged over it, so local overrides and API keys never
end up in a commit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_BASE = REPO_ROOT / "config.yaml"
_LOCAL = REPO_ROOT / "config.local.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dict-backed config with path resolution and env-var secret lookup."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value by dotted path, e.g. ``crosswalk.fuzzy_threshold``."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path:
        """Resolve a ``paths.*`` entry against the repo root."""
        raw = self.get(f"paths.{key}")
        if raw is None:
            raise KeyError(f"paths.{key} is not defined in config.yaml")
        p = Path(raw)
        return p if p.is_absolute() else REPO_ROOT / p

    def secret(self, dotted_env_key: str) -> str | None:
        """Read a secret from the env var named by a ``*_env`` config entry."""
        env_name = self.get(dotted_env_key)
        return os.environ.get(env_name) if env_name else None

    def as_dict(self) -> dict[str, Any]:
        return self._data

    @property
    def seasons(self) -> list[int]:
        start = int(self.get("seasons.start"))
        end = int(self.get("seasons.end"))
        return list(range(start, end + 1))


def load(base: Path = _BASE, local: Path = _LOCAL) -> Config:
    with open(base) as fh:
        data = yaml.safe_load(fh) or {}
    if local.exists():
        with open(local) as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    return Config(data)
