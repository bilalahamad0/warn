"""
scripts/backfill/oh_2023_2025_gap.py
------------------------------------
One-time capture of Ohio's 2023-2025 WARN notices
(2023-01-01 .. 2025-12-31) into ``data/historical/oh_history.json``.

Why this span was missing
    ``warn_sources/oh.py`` covers the current year (the ODJFS notices
    page's embedded ``csvUrl``) plus Big Local News' pre-scraped
    2017-2022 snapshot. In between, 2023-2025 existed only as archive
    PDFs neither this pipeline nor BLN's own scraper parsed — leaving
    the national dataset with zero Ohio records for all three years
    (2022=59, then nothing until 2026=64), which the US map rendered as
    a suspected source coverage gap.

Where the data still lives
    JFS's rebuilt site republishes each archived year as a CSV on the
    same Cloudinary DAM the live feed uses, in the live feed's exact
    shape (same junk preamble, same columns, embedded as ``csvUrl`` in
    each year page's Next.js payload). The year pages hang off "Submit
    a WARN Notice" ▸ Archived Notices (https://jfs.ohio.gov/page/...,
    2021-2025), with a stated retention of current year plus five years
    back — so this capture also beats the archive's rolling deletion.
    Parsing is delegated to ``warn_sources.oh.OhioJFS.parse``/``unify``
    via the same consolidated-CSV shape the live ``fetch`` writes.

Output pattern (mirrors scripts/backfill/ny_2025_gap.py)
    The result is NOT merged into the state's cumulative store.
    ``warn_sources/oh.py`` declares the output file as its
    ``history_file``, so ``warn_sources.aggregate`` folds these records
    into the NATIONAL dataset only; the OH live pipeline is untouched.

Window + dedup rules
    - Only notice dates 2023-01-01 .. 2025-12-31 are written. 2022 and
      earlier stays with the BLN snapshot already in the cumulative
      store; 2026 onward belongs to the live feed.
    - Ohio re-lists an amended filing as a single "UPDATE ..." row
      re-dated to the update's receipt date (the original line is
      replaced, not kept). UPDATE rows are kept verbatim, exactly as
      the live source keeps them. Two 2025-archive rows were re-dated
      into 2026 this way (Eagle Machining, Daniel Drake Center); they
      fall outside the window and are dropped — their original 2025
      notice dates are not published anywhere, and inventing them is
      worse than the two-row undercount.
    - Identical rows (same ``_record_key``; Ohio publishes no address)
      collapse to one. As of capture this collapses nothing — the
      archive has no flattened amendment repeats — but it keeps a
      re-run over a re-exported archive safe.
    - Defensively, any window record matching the live cumulative store
      on (company, notice_date) is dropped. As of capture this drops
      nothing — the store holds only 2015-2022 and 2026 records — but
      it keeps a re-run safe if the live feed ever re-surfaces a
      2023-2025 notice.

Run:  .venv/bin/python scripts/backfill/oh_2023_2025_gap.py
The raw CSVs are cached at data/states/oh/raw_backfill/<year>_warn_notice.csv
(committed for provenance) so re-runs never re-fetch.
"""

import csv
import json
import logging
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import warn_monitor  # noqa: E402
from warn_sources import oh as oh_module  # noqa: E402
from warn_sources.oh import RAW_COLUMNS, OhioJFS  # noqa: E402

log = logging.getLogger("backfill.oh_2023_2025_gap")

# The BLN-snapshot era ends with 2022; the live feed owns 2026 onward.
WINDOW_START = "2023-01-01"
WINDOW_END = "2025-12-31"

# Per-year archive CSVs (each year page's ``csvUrl``, resolved 2026-08-06
# from the pages under https://jfs.ohio.gov/page/c2b1de753adecfaf4e10e
# "Submit a WARN Notice" ▸ Archived Notices).
ARCHIVE_CSVS = {
    "2023": (
        "https://dam.assets.ohio.gov/raw/upload/f_auto/q_auto/v1776259430/"
        "jfs.ohio.gov/2026/2023-warn-notice_1_9.csv"
    ),
    "2024": (
        "https://dam.assets.ohio.gov/raw/upload/f_auto/q_auto/v1776259430/"
        "jfs.ohio.gov/2026/2024_warn_notice.csv"
    ),
    "2025": (
        "https://dam.assets.ohio.gov/raw/upload/f_auto/q_auto/v1776198587/"
        "jfs.ohio.gov/2026/2025_warn_notice.csv"
    ),
}

CACHE_DIR = REPO_ROOT / "data" / "states" / "oh" / "raw_backfill"
OUTPUT_FILE = REPO_ROOT / "data" / "historical" / "oh_history.json"
STORE_FILE = REPO_ROOT / "data" / "states" / "oh" / "warn_cumulative.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_all(urls=None, cache_dir: Path = CACHE_DIR) -> list:
    """Download each year's archive CSV once (sequential, polite); reuse
    cached copies. Returns the cached paths in year order."""
    urls = urls if urls is not None else ARCHIVE_CSVS
    paths = []
    for year in sorted(urls):
        cache_file = cache_dir / f"{year}_warn_notice.csv"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            log.info(f"Reusing cached {cache_file}")
            paths.append(cache_file)
            continue
        url = urls[year]
        log.info(f"Downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if b"Date Received" not in data:
            raise RuntimeError(f"{year} export does not look like the WARN CSV")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        log.info(f"Saved {len(data)} bytes to {cache_file}")
        paths.append(cache_file)
        time.sleep(1.5)  # politeness: sequential, ~1 request/1.5s
    return paths


def _feed_rows(text: str) -> list:
    """Archive CSV text -> row dicts, via the live source's line cleanup.

    The archive files carry the live feed's junk preamble ("s,h,s,...",
    a row of bare commas), but the 2023 export pads every line with
    trailing commas, pushing the "s,h,..." line past the length filter
    inside ``oh._read_feed_csv`` — so pure s/h/comma lines are stripped
    here first, then the vendored cleanup handles the rest.
    """
    lines = []
    for line in text.splitlines():
        bare = line.replace(",", "").strip()
        if bare and set(bare.lower()) <= {"s", "h"}:
            continue
        lines.append(line)
    return oh_module._read_feed_csv("\n".join(lines))


def _coarse_key(record: dict) -> tuple:
    """(company, notice_date) — the defensive cross-store identity."""
    return (
        str(record.get("company", "")).strip().lower(),
        str(record.get("notice_date", "") or "")[:10],
    )


def capture(csv_paths, store_records: list) -> tuple:
    """Per-year archive CSVs -> (window records, stats).

    Pure so tests can drive it with a fixture CSV and a fake store.
    """
    rows = []
    for path in csv_paths:
        rows.extend(_feed_rows(Path(path).read_text(errors="replace")))

    # Reuse the live parser end to end: write the same consolidated CSV
    # shape ``OhioJFS.fetch`` produces, then parse + unify it.
    src = OhioJFS(data_dir=None)
    with tempfile.TemporaryDirectory() as td:
        consolidated = Path(td) / "oh_archive_consolidated.csv"
        with open(consolidated, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=RAW_COLUMNS, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
        df = src.unify(src.parse(consolidated))

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
        # Ohio publishes no address, so _record_key (company/county/city/
        # dates/employees) is the full row identity; repeats collapse.
        ident = warn_monitor._record_key(r)
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
            str(r.get("city", "")),
        )
    )
    return kept, stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    csv_paths = fetch_all()
    store_records = []
    if STORE_FILE.exists():
        store_records = json.loads(STORE_FILE.read_text()).get("records", [])

    records, stats = capture(csv_paths, store_records)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": (
            "ODJFS per-year WARN archive CSVs (jfs.ohio.gov, 2023-2025)"
        ),
        "window": [WINDOW_START, WINDOW_END],
        "records": records,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))
    log.info(f"Wrote {len(records)} records to {OUTPUT_FILE}")

    # ---- validation report -------------------------------------------------
    yearly = {}
    for r in records:
        year = r["notice_date"][:4]
        n, e = yearly.get(year, (0, 0))
        yearly[year] = (n + 1, e + r["employees"])
    print("\nPer-year capture (window only):")
    print(f"{'year':6s} {'records':>8s} {'employees':>10s}")
    for year in sorted(yearly):
        n, e = yearly[year]
        print(f"{year:6s} {n:8d} {e:10d}")
    months = sorted({r["notice_date"][:7] for r in records})
    print(f"months represented: {len(months)} (expect 36)")
    print(f"\ntotal_records: {len(records)}")
    print(f"employees_total: {sum(r['employees'] for r in records)}")
    print(f"stats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
