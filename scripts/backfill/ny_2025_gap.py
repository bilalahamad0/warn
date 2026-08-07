"""
scripts/backfill/ny_2025_gap.py
-------------------------------
One-time capture of New York's dashboard-era 2025 WARN notices
(2025-04-01 .. 2025-12-31) into ``data/historical/ny_history.json``.

Why this span was missing
    NY retired its legacy per-year HTML/PDF listings on 2025-04-01; the
    legacy pages (https://dol.ny.gov/legacy-warn-notices) are frozen at
    Jan 1 - Apr 1, 2025 and ``scripts/backfill/ny.py`` already captured
    them (plus 2016-2024) into the NY cumulative store. The replacement
    Tableau Public dashboard defaults to the current filing year, so when
    2026 began every dashboard-era 2025 notice rolled off the CSV export
    that ``warn_sources/ny.py`` polls — leaving the national dataset with
    61 NY notices for 2025 against 324 for 2024, and Apr-Oct + Dec 2025
    empty. No per-year archive page exists for 2025 (2025-warn-notices is
    a 404) and no dashboard-era notice has an individual posting page.

Where the data still lives
    The Tableau workbook itself keeps prior years behind the current-year
    default filter (the dashboard FAQ says previous-year notices can be
    downloaded from it). The same CSV export endpoint honors a caption
    URL filter — ``YEAR(Date of WARN Notice)=2025`` — which is how this
    script recovers the span: 671 site-level rows covering all twelve
    2025 months. Same feed, same columns as the live source, so parsing
    is delegated to ``warn_sources.ny.NewYorkDOL.parse``/``unify``.

Output pattern (mirrors scripts/backfill/ca.py)
    The result is NOT merged into the state's cumulative store.
    ``warn_sources/ny.py`` declares the output file as its
    ``history_file``, so ``warn_sources.aggregate`` folds these records
    into the NATIONAL dataset only; the NY live pipeline is untouched.

Window + dedup rules
    - Only notice dates 2025-04-01 .. 2025-12-31 are written. Jan-Mar
      2025 stays with the legacy-page records already in the cumulative
      store: the same notice is spelled differently across the PDF and
      Tableau sources ("F21 OpCO, LLC. dba Forever 21" vs "F21 OpCo,
      LLC"), so cross-source dedup is unreliable — only 41 of the 102
      Jan-Mar dashboard rows match the store textually and the rest
      would double-count. 2026 onward belongs to the live feed.
    - Identical rows (same ``_record_key`` AND address) collapse to one:
      Tableau flattens a notice's amendment history into repeated
      identical lines. Rows sharing a ``_record_key`` at DIFFERENT
      addresses are kept — they are genuinely distinct sites (Rite Aid's
      May 2025 filing alone spans 178 store rows, many with equal
      per-site headcounts in the same county).
    - Defensively, any window record matching the live cumulative store
      on (company, notice_date) is dropped. As of capture this drops
      nothing — the store has no dashboard-era 2025 record with a
      textual twin — but it keeps a re-run safe if the live feed ever
      re-surfaces a 2025 notice.

Run:  .venv/bin/python scripts/backfill/ny_2025_gap.py
The raw CSV is cached at data/states/ny/raw_backfill/tableau_2025.csv
(committed for provenance) so re-runs never re-fetch.
"""

import json
import logging
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import warn_monitor  # noqa: E402
from warn_sources.ny import NewYorkDOL  # noqa: E402

log = logging.getLogger("backfill.ny_2025_gap")

# The legacy->dashboard cutover and the last day before live-feed coverage.
WINDOW_START = "2025-04-01"
WINDOW_END = "2025-12-31"

# The live source's own export URL plus the year caption filter
# (``YEAR(Date of WARN Notice)=2025``, URL-encoded).
EXPORT_URL = NewYorkDOL.source_url + "&YEAR%28Date%20of%20WARN%20Notice%29=2025"

CACHE_FILE = REPO_ROOT / "data" / "states" / "ny" / "raw_backfill" / "tableau_2025.csv"
OUTPUT_FILE = REPO_ROOT / "data" / "historical" / "ny_history.json"
STORE_FILE = REPO_ROOT / "data" / "states" / "ny" / "warn_cumulative.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch(url: str = EXPORT_URL, cache_file: Path = CACHE_FILE) -> Path:
    """Download the year-filtered CSV export once; reuse the cached copy."""
    if cache_file.exists() and cache_file.stat().st_size > 0:
        log.info(f"Reusing cached {cache_file}")
        return cache_file
    log.info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data.strip().startswith(b"Business Legal Name"):
        raise RuntimeError("export does not look like the WARN CSV")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(data)
    log.info(f"Saved {len(data)} bytes to {cache_file}")
    time.sleep(1.0)  # courtesy pause; keeps any follow-up request polite
    return cache_file


def _coarse_key(record: dict) -> tuple:
    """(company, notice_date) — the defensive cross-store identity."""
    return (
        str(record.get("company", "")).strip().lower(),
        str(record.get("notice_date", "") or "")[:10],
    )


def capture(csv_path, store_records: list) -> tuple:
    """Year-2025 export CSV -> (gap-window records, stats).

    Pure so tests can drive it with a fixture CSV and a fake store.
    """
    src = NewYorkDOL()
    df = src.unify(src.parse(csv_path))

    stats = {
        "rows": len(df),
        "outside_window": 0,
        "undated": 0,
        "identical_dupes": 0,
        "in_live_store": 0,
    }
    store_keys = {_coarse_key(r) for r in store_records}
    seen = set()
    kept = []
    for r in df.to_dict("records"):
        date = r.get("notice_date")
        if not date:
            stats["undated"] += 1
            continue
        if not (WINDOW_START <= date <= WINDOW_END):
            stats["outside_window"] += 1
            continue
        # Tableau flattens amendment history into repeated identical
        # lines; collapse those but keep distinct sites (address differs).
        ident = (warn_monitor._record_key(r), str(r.get("address", "")).lower())
        if ident in seen:
            stats["identical_dupes"] += 1
            continue
        seen.add(ident)
        if _coarse_key(r) in store_keys:
            stats["in_live_store"] += 1
            continue
        r["employees"] = int(r.get("employees") or 0)
        kept.append(r)

    kept.sort(
        key=lambda r: (
            r.get("notice_date") or "",
            str(r.get("company", "")).lower(),
            str(r.get("county", "")),
            str(r.get("address", "")),
        )
    )
    return kept, stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    csv_path = fetch()
    store_records = []
    if STORE_FILE.exists():
        store_records = json.loads(STORE_FILE.read_text()).get("records", [])

    records, stats = capture(csv_path, store_records)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": (
            "NYS DOL WARN dashboard (Tableau Public), year-2025 CSV export"
        ),
        "window": [WINDOW_START, WINDOW_END],
        "records": records,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))
    log.info(f"Wrote {len(records)} records to {OUTPUT_FILE}")

    # ---- validation report -------------------------------------------------
    monthly = {}
    for r in records:
        month = r["notice_date"][:7]
        n, e = monthly.get(month, (0, 0))
        monthly[month] = (n + 1, e + r["employees"])
    print("\nPer-month capture (site-level records, gap window only):")
    print(f"{'month':8s} {'records':>8s} {'employees':>10s}")
    for month in sorted(monthly):
        n, e = monthly[month]
        print(f"{month:8s} {n:8d} {e:10d}")
    print(f"\ntotal_records: {len(records)}")
    print(f"employees_total: {sum(r['employees'] for r in records)}")
    print(f"stats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
