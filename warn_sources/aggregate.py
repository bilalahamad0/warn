"""
warn_sources.aggregate
----------------------
Builds ``data/warn_national.json`` — the unified multi-state dataset that
drives the US map and any cross-state analytics.

Reads each enabled source's cumulative store (fallback: latest), stamps the
state code on legacy records that predate the unified schema, and writes one
payload with per-state summaries plus the concatenated record list.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import registered_sources
from .base import DATA_DIR, sanitize_records

log = logging.getLogger("warn_sources")

NATIONAL_FILE = DATA_DIR / "warn_national.json"


def _read_records(path: Path) -> list:
    try:
        return json.loads(path.read_text()).get("records", [])
    except Exception as e:
        log.warning(f"Could not read {path} ({e}) — skipping.")
        return []


def build_national(
    data_dir: Optional[Path] = None, output_file: Optional[Path] = None
) -> dict:
    """Concatenate every enabled source's records into the national dataset."""
    output_file = output_file if output_file is not None else NATIONAL_FILE

    states: dict = {}
    records: list = []
    for source in registered_sources(data_dir):
        store = (
            source.paths.cumulative
            if source.paths.cumulative.exists()
            else source.paths.latest
        )
        if not store.exists():
            log.info(f"[{source.code.upper()}] no data yet — skipped in national set.")
            continue
        code = source.code.upper()
        # Cumulative stores keep every row ever seen, including ones written
        # before a parser bug was fixed — clean them on the way out.
        state_records = sanitize_records(_read_records(store), code)
        # Optional deep-history file: national-only records merged with
        # _record_key dedup so live-era rows never double count.
        if source.history_file is not None and source.history_file.exists():
            import warn_monitor

            have = {warn_monitor._record_key(r) for r in state_records}
            extra = [
                r for r in _read_records(source.history_file)
                if warn_monitor._record_key(r) not in have
            ]
            if extra:
                log.info(f"[{code}] +{len(extra)} history records (national only)")
            state_records = state_records + extra
        for r in state_records:
            # Legacy records (pre-unified-schema) carry no state field.
            r.setdefault("state", code)
        employees = int(sum(r.get("employees") or 0 for r in state_records))
        states[code] = {
            "name": source.name,
            "agency": source.agency,
            "source_url": source.source_url,
            "total_records": len(state_records),
            "total_employees": employees,
        }
        records.extend(state_records)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        "states_live": len(states),
        "total_records": len(records),
        "total_employees": int(sum(r.get("employees") or 0 for r in records)),
        "states": states,
        "records": records,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2, default=str))
    log.info(
        f"National dataset: {len(records)} records across {len(states)} state(s) "
        f"→ {output_file.name}"
    )
    return payload
