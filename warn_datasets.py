"""
warn_datasets.py
----------------
Resolves which records back the California dashboard, and derives them.

Why this exists
~~~~~~~~~~~~~~~
California had two disjoint stores and the dashboards disagreed about it:

* ``data/warn_cumulative.json`` — the live EDD feed, whose earliest notice is
  2025-01-29. It backed the California dashboard's KPIs, table and nine of its
  twelve charts.
* ``data/historical/ca_national_history.json`` — the deep backfill, merged into
  ``data/warn_national.json`` by ``warn_sources.aggregate``. It contributes 71
  notices dated 2025-01-03 → 2025-01-28, worth 5,475 employees, that the live
  feed never had.

The merge dedupes nothing because the two sets are disjoint, so the US
dashboard counted 827 California notices for calendar 2025 while the California
dashboard counted 756 — same state, same year, two numbers, on one site.

The fix is to stop treating the live feed as California's source of truth and
derive the California view from the national dataset instead, which already
contains everything. One upstream, so the two pages cannot drift again.

Why a coverage boundary
~~~~~~~~~~~~~~~~~~~~~~~
The national CA slice reaches back to 2008, but only its 2025-onward rows carry
the ``industry`` field (the backfill has none at all) and pre-2025 rows are
missing county on ~15% of records. The California dashboard's industry chart,
industry filter and county filter would all silently degrade across that
boundary. So the dashboard covers 2025 onward — a calendar-year edge, chosen so
the 2025 KPI year is whole and directly comparable to the US dashboard's own
California slice — and says so on the page.

The invariant that keeps this honest is in ``tests/test_datasets.py``: *every*
California record in the national dataset dated on or after
``CA_COVERAGE_START`` must appear in the derived payload. A constant cannot
promise no-drift; that test can, and it fails loudly if the backfill is
extended, the merge changes, or the boundary moves.

Nothing is written to ``data/``. The payload is derived in memory and published
as ``docs/ca/data.json``, which is the inspectable artifact — persisting a
second copy under ``data/`` would just churn ~640 KB into git twice a day.

Imports nothing else from the project: ``warn_charts`` and ``warn_publish``
both depend on it, and ``warn_charts`` must not be made to pull in the whole
47-module ``warn_sources`` package just to resolve a path.
"""

import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

LATEST_FILE = DATA_DIR / "warn_latest.json"
CUMULATIVE_FILE = DATA_DIR / "warn_cumulative.json"
NATIONAL_FILE = DATA_DIR / "warn_national.json"

# Earliest notice date the California dashboard covers. See the module
# docstring for why this boundary exists and why it is a calendar-year edge.
CA_COVERAGE_START = "2025-01-01"

# The record shape ``docs/ca/data.json`` publishes. Fixed and exhaustive: the
# 71 backfilled records omit ``city`` and ``industry`` as *keys* rather than
# leaving them empty, and this file is a public API whose consumers should not
# have to cope with a field appearing and disappearing by row.
CA_RECORD_FIELDS = (
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
    "industry",
    "state",
)

log = logging.getLogger("warn_datasets")

_ca_cache = None


def _records(payload) -> list:
    """Records out of either payload shape (``{"records": [...]}`` or a list)."""
    if isinstance(payload, dict):
        return payload.get("records", [])
    return payload if isinstance(payload, list) else []


def _read(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _normalise(record: dict) -> dict:
    """Project a record onto CA_RECORD_FIELDS, filling absent keys with ""."""
    out = {}
    for field in CA_RECORD_FIELDS:
        value = record.get(field)
        out[field] = "" if value is None else value
    out["employees"] = _int(record.get("employees"))
    out["state"] = "CA"
    return out


def build_ca_dashboard(national_file: Path = None,
                       start: str = CA_COVERAGE_START) -> dict:
    """Derive the California dashboard payload from the national CA slice.

    Returns a payload in the same shape the dashboard has always consumed
    (``{"records": [...], "total_records": N, ...}``) so every downstream
    reader is unchanged. Totals are recomputed here rather than inherited from
    the national payload, which counts 47 jurisdictions.

    Raises ``FileNotFoundError`` when the national dataset is absent — callers
    fall back via :func:`load_ca_dashboard`.
    """
    path = Path(national_file) if national_file else NATIONAL_FILE
    national = _read(path)
    if national is None:
        raise FileNotFoundError(f"National dataset not found — {path}")

    records = [
        _normalise(r)
        for r in _records(national)
        if str(r.get("state") or "").upper() == "CA"
        and str(r.get("notice_date") or r.get("effective_date") or "")[:10] >= start
    ]
    # Newest first, so a rebuilt docs/ca/data.json diffs cleanly run to run.
    records.sort(key=lambda r: str(r.get("notice_date") or ""), reverse=True)

    dates = sorted(str(r.get("notice_date") or "")[:10] for r in records
                   if r.get("notice_date"))

    # Keep the live feed's provenance field on the published API even though the
    # records now come via the national dataset — the underlying filing source
    # is still the EDD.
    live = _read(CUMULATIVE_FILE) or {}

    return {
        "scope": "ca",
        "state": "CA",
        "coverage_start": start,
        "last_updated": (national.get("last_updated")
                         if isinstance(national, dict) else "") or "",
        "source_url": live.get("source_url", ""),
        "total_records": len(records),
        "total_employees": sum(_int(r.get("employees")) for r in records),
        "date_range_start": dates[0] if dates else "",
        "date_range_end": dates[-1] if dates else "",
        "records": records,
    }


def load_ca_dashboard(national_file: Path = None, refresh: bool = False) -> dict:
    """The California dashboard payload, cached per process.

    ``warn_publish`` and ``warn_charts`` both call this, so the page's KPIs,
    its table and its charts are guaranteed to describe the same records.

    Falls back to the raw cumulative store (then the latest download) if the
    national dataset is missing or unreadable — degraded but correct-shaped,
    which is what the dashboard did before this module existed. The fallback
    payload is returned as-is and carries no ``coverage_start``, which is how
    ``warn_publish`` detects the degraded state and says so on the page.
    """
    global _ca_cache
    if _ca_cache is not None and not refresh:
        return _ca_cache

    try:
        _ca_cache = build_ca_dashboard(national_file)
        return _ca_cache
    except Exception as exc:  # noqa: BLE001 — a degraded page beats no page
        log.warning(
            "Falling back to the raw California store — the dashboard will "
            "under-report early 2025 (%s)", exc,
        )

    for path in (CUMULATIVE_FILE, LATEST_FILE):
        payload = _read(path)
        if payload is not None:
            _ca_cache = payload
            return _ca_cache

    raise FileNotFoundError(
        "Run warn_monitor.py first — no California dataset found "
        f"({NATIONAL_FILE}, {CUMULATIVE_FILE}, {LATEST_FILE})"
    )


def ca_dashboard_records(national_file: Path = None) -> list:
    """Convenience: just the records the California dashboard shows."""
    return _records(load_ca_dashboard(national_file))


def reset_cache() -> None:
    """Drop the cached payload (tests, and any in-process rebuild)."""
    global _ca_cache
    _ca_cache = None
