"""
cgate.settings — procedure values that used to be hard-coded.

Defaults are built in, so the engine runs with no configuration. A YAML file
(see ``config/procedure.example.yaml``) overrides them; point ``CGATE_CONFIG``
at it, or place it at ``config/procedure.yaml`` in the working directory.
"""

from __future__ import annotations

import copy
import os
from functools import lru_cache

DEFAULTS: dict = {
    "firm": {"name": "Example Engineering Ltd", "aliases": []},
    "references": {
        "procedure": "SOP-3.10",
        "checklist": "SOP-3.11",
        "jurisdiction_table": "SOP-3.7",
        "governance_note": "the cgate review-process notes",
    },
    "clocks": {
        "standard_notification_days": 10,
        "fasttrack_notification_days": 5,
        "standard_review_days": 7,
        "fasttrack_review_days": 3,
        "fasttrack_approval_days": 1,
    },
    "retention_years": 7,
    "agency_aliases": [],
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _config_path() -> str | None:
    env = os.environ.get("CGATE_CONFIG")
    if env:
        return env
    local = os.path.join("config", "procedure.yaml")
    return local if os.path.exists(local) else None


@lru_cache(maxsize=1)
def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = _config_path()
    if path and os.path.exists(path):
        import yaml
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, yaml.safe_load(fh) or {})
    return cfg


def reload() -> dict:
    load.cache_clear()
    return load()


def ref(name: str) -> str:
    return load()["references"][name]


def clock(name: str) -> int:
    return int(load()["clocks"][name])


def firm_name() -> str:
    return load()["firm"]["name"]
