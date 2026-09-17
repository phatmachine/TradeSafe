"""Config loading. Thresholds are never hardcoded in compute/gates/setups modules — a
literal there is a defect (implementation spec, "Repo structure and build order"). This
module is the single place that reads config/*.yaml and computes the config_hash that
every DecisionRecord is stamped with.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _hash_file(name: str) -> str:
    path = CONFIG_DIR / name
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    thresholds: dict[str, Any]
    universe: dict[str, Any]
    sources: dict[str, Any]
    thresholds_hash: str
    universe_hash: str
    config_hash: str  # combined hash stamped on every DecisionRecord
    validated: bool

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.thresholds
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


@lru_cache(maxsize=1)
def load_config() -> Config:
    thresholds = _load_yaml("thresholds.yaml")
    universe = _load_yaml("universe.yaml")
    sources = _load_yaml("sources.yaml")
    thresholds_hash = _hash_file("thresholds.yaml")
    universe_hash = _hash_file("universe.yaml")
    combined = hashlib.sha256(f"{thresholds_hash}:{universe_hash}".encode()).hexdigest()[:16]
    validated = bool(thresholds.get("meta", {}).get("validated", False))
    return Config(
        thresholds=thresholds,
        universe=universe,
        sources=sources,
        thresholds_hash=thresholds_hash,
        universe_hash=universe_hash,
        config_hash=combined,
        validated=validated,
    )


def with_override(cfg: Config, path: tuple[str, ...], value: Any) -> Config:
    """Returns a new Config with one threshold overridden — used only by the
    calibration sweep, which must vary a threshold across many in-process runs without
    touching the on-disk file or the process-wide cached Config (implementation spec,
    "Calibration sweep"). config_hash is recomputed over the override so a swept report
    is never mistaken for one produced under the real, on-disk thresholds.
    """
    import copy

    new_thresholds = copy.deepcopy(cfg.thresholds)
    node = new_thresholds
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    override_hash = hashlib.sha256(f"{cfg.thresholds_hash}:{path}:{value}".encode()).hexdigest()[:16]
    combined = hashlib.sha256(f"{override_hash}:{cfg.universe_hash}".encode()).hexdigest()[:16]
    return Config(
        thresholds=new_thresholds,
        universe=cfg.universe,
        sources=cfg.sources,
        thresholds_hash=override_hash,
        universe_hash=cfg.universe_hash,
        config_hash=combined,
        validated=False,
    )


def reload_config() -> Config:
    """Bust the cache — used by tests and by the calibration sweep, which varies
    thresholds across many runs in-process."""
    load_config.cache_clear()
    return load_config()
