"""Loads config.yaml. All thresholds come from here; bots never hard-code numbers."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

REQUIRED_SECTIONS = ("data", "loop", "risk", "scout", "gatekeeper", "entry", "exit", "broker", "journal", "review")


class Section(dict):
    """A dict that also allows attribute access: ``cfg.scout.min_liquidity_usd``."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(f"config key missing: {key}") from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __deepcopy__(self, memo: dict) -> "Section":
        return Section({k: copy.deepcopy(v, memo) for k, v in self.items()})


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Section({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Section:
    with open(path or DEFAULT_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if overrides:
        _deep_merge(raw, overrides)
    cfg = _wrap(raw)
    validate(cfg)
    return cfg


def _deep_merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def validate(cfg: Section) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ValueError(f"config.yaml is missing sections: {missing}")
    if cfg.get("mode") not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    if cfg.entry.max_slippage_pct > 5:
        raise ValueError("entry.max_slippage_pct may never exceed 5")
    if cfg.entry.default_slippage_pct > cfg.entry.max_slippage_pct:
        raise ValueError("entry.default_slippage_pct exceeds entry.max_slippage_pct")
    if sum(cfg.entry.tranche_split_pct) != 100:
        raise ValueError("entry.tranche_split_pct must sum to 100")
    scores = [t["max_score"] for t in cfg.gatekeeper.tiers]
    if scores != sorted(scores) or scores[-1] != 100:
        raise ValueError("gatekeeper.tiers must be ascending and end at max_score 100")
    if cfg.capital_usd <= 0:
        raise ValueError("capital_usd must be positive")
