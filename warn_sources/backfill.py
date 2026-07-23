"""
warn_sources.backfill
---------------------
Merge *historical* records into a state's cumulative store.

Backfill only ever touches ``warn_cumulative.json`` — never the latest
snapshot, the diff path, or the alert ledgers — so it can deepen dashboard
history without any chance of triggering notification emails.

To avoid double-counting the era the live feed already covers, callers
should pass only records dated before the live store's earliest record;
``live_floor`` returns that boundary.
"""

import logging
from pathlib import Path
from typing import Optional

import warn_monitor

log = logging.getLogger("warn_sources")


def _clean(record: dict, code: str) -> Optional[dict]:
    """Normalize one historical record; None if it fails minimal validation."""
    company = str(record.get("company") or "").strip()
    if not company:
        return None
    out = dict(record)
    out["state"] = code.upper()
    out["company"] = company
    out["employees"] = warn_monitor._safe_int(record.get("employees")) or 0
    for f in ("notice_date", "effective_date"):
        out[f] = warn_monitor._safe_date(record.get(f)) if record.get(f) else None
    for f in ("layoff_type", "county", "city", "address", "industry"):
        if f in out and out[f] is not None:
            out[f] = str(out[f]).strip()
    return out


def live_floor(code: str, data_dir: Optional[Path] = None) -> Optional[str]:
    """Earliest event date (notice, else effective) in the state's live store."""
    import json

    from . import get_source

    paths = get_source(code, data_dir).paths
    store = paths.cumulative if paths.cumulative.exists() else paths.latest
    if not store.exists():
        return None
    records = json.loads(store.read_text()).get("records", [])
    dates = [
        str(r.get("notice_date") or r.get("effective_date") or "")[:10]
        for r in records
    ]
    dates = [d for d in dates if len(d) == 10]
    return min(dates) if dates else None


def merge_records(
    code: str, records: list, data_dir: Optional[Path] = None
) -> dict:
    """Merge historical records into the state's cumulative store.

    Deduplication happens on warn_monitor._record_key, so re-running a
    backfill is idempotent. Works for disabled sources too (their historical
    data is still worth showing even while live fetch is blocked).
    """
    from . import get_source

    source = get_source(code, data_dir)
    source.paths.ensure()
    cleaned = [c for c in (map(lambda r: _clean(r, code), records)) if c]
    dropped = len(records) - len(cleaned)
    if dropped:
        log.info(f"[{code.upper()}] backfill: dropped {dropped} invalid record(s).")
    summary = warn_monitor.update_cumulative(
        cleaned,
        cumulative_file=source.paths.cumulative,
        amended_file=source.paths.amended,
        source_url=source.source_url,
    )
    log.info(
        f"[{code.upper()}] backfill merged {len(cleaned)} record(s); "
        f"cumulative now {summary['total_records']}."
    )
    return {k: v for k, v in summary.items() if k != "records"}
