"""
warn_sources
------------
Registry of per-state WARN feed sources.

Adding a state = one module in this package implementing ``Source.fetch`` and
``Source.parse`` (see base.py), plus a registry entry below. Everything else —
change detection, alert ledgers, snapshot rotation, cumulative union,
aggregation, charts — is shared infrastructure.

Vendored implementations may be ported from Big Local News' Apache-2.0
warn-scraper project (see EXPANSION_RESEARCH.md) — ported into this package,
never a runtime dependency on an external platform.
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Optional

from .base import DATA_DIR, STATES_DIR, UNIFIED_FIELDS, Source, StatePaths  # noqa: F401

log = logging.getLogger("warn_sources")

# code -> Source subclass, auto-discovered from every module in this package.
# Adding a state therefore never touches a shared file: drop in <code>.py
# defining a Source subclass and it registers itself. A module that fails to
# import is skipped with a warning so one broken state can never take down the
# whole pipeline. (Modules named after Python keywords — in.py, or.py — are
# fine: discovery goes through importlib, not import statements.)
_INFRA_MODULES = {"base", "aggregate"}
SOURCES: dict = {}
for _modinfo in pkgutil.iter_modules(__path__):
    if _modinfo.name in _INFRA_MODULES:
        continue
    try:
        _mod = importlib.import_module(f".{_modinfo.name}", __name__)
    except Exception as _e:  # noqa: BLE001 — isolation boundary by design
        log.warning(f"Source module '{_modinfo.name}' failed to import: {_e}")
        continue
    for _obj in list(vars(_mod).values()):
        if (
            isinstance(_obj, type)
            and issubclass(_obj, Source)
            and _obj is not Source
            and _obj.__module__ == _mod.__name__
            and getattr(_obj, "code", "xx") != "xx"
        ):
            SOURCES[_obj.code] = _obj
SOURCES = dict(sorted(SOURCES.items()))


def get_source(code: str, data_dir: Optional[Path] = None) -> Source:
    """Instantiate the source for a state code (KeyError if unknown)."""
    return SOURCES[code.lower()](data_dir)


def all_sources(data_dir: Optional[Path] = None) -> list:
    """Instantiate every enabled source, in registry order."""
    return [cls(data_dir) for cls in SOURCES.values() if cls.enabled]


def registered_sources(data_dir: Optional[Path] = None) -> list:
    """Every registered source, including disabled ones.

    ``enabled`` gates live *fetching* only — a disabled source (e.g. TX
    behind its bot wall) can still hold backfilled historical data that the
    national dataset and dashboards should surface.
    """
    return [cls(data_dir) for cls in SOURCES.values()]


def run_all(
    dry_run: bool = False, force: bool = False, data_dir: Optional[Path] = None
) -> dict:
    """Run every enabled source with per-state failure isolation.

    One state failing (site moved, bot wall, parse break) must never block the
    others: its entry carries an ``error`` key instead and the loop continues.
    Returns {code: result_dict} in registry order.
    """
    results: dict = {}
    for source in all_sources(data_dir):
        try:
            results[source.code] = source.run(dry_run=dry_run, force=force)
        except Exception as e:  # noqa: BLE001 — isolation boundary by design
            log.error(f"[{source.code.upper()}] source failed: {e}")
            results[source.code] = {
                "state": source.code.upper(),
                "error": str(e),
                "file_changed": False,
                "diff": {"new_count": 0, "removed_count": 0, "amendment_count": 0},
                "summary": {},
                "cumulative": {},
            }
    return results
