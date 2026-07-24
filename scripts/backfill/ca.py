"""
scripts/backfill/ca.py
----------------------
Reproducible extraction of California's deep WARN history (July 2014 on)
from the EDD fiscal-year report PDF archives into
``data/historical/ca_national_history.json``.

Unlike the other backfill scripts this one does NOT merge into a state
cumulative store: ``warn_sources/ca.py`` declares the output file as its
``history_file``, so ``warn_sources.aggregate`` folds these records into
the NATIONAL dataset only (dedup by ``warn_monitor._record_key``); the CA
dashboard and live pipeline are untouched.

Source PDFs are machine-generated tables, one row per notice *site* (the
same granularity as the live XLSX). Every page is parsed with
``pdfplumber.extract_tables``; the header row of each PDF maps its columns
(layouts vary across years — early years have City but no County/Address,
recent years County+Address but no City). Each report ends with a
"Summary by Month" table whose Total row gives the official notice and
employee counts — printed alongside our parse for validation. Only fields
a PDF really has are emitted, dates are ISO-or-None (never copied from
one field to another), and unparseable rows are dropped and counted,
never guessed.

Only records dated strictly BEFORE ``warn_sources.backfill.live_floor``
are written — the live store owns everything at/after its floor.

Usage:
    .venv/bin/python scripts/backfill/ca.py [--download-dir DIR]
"""

import argparse
import html
import json
import logging
import re
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import warn_monitor  # noqa: E402
from warn_sources import backfill  # noqa: E402

log = logging.getLogger("backfill.ca")

STATE = "ca"
EDD_BASE = "https://edd.ca.gov"
LISTING_URL = EDD_BASE + "/en/jobs_and_training/Layoff_Services_WARN/"
PDF_DIR = "/siteassets/files/jobs_and_training/warn/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
OUTPUT_FILE = REPO_ROOT / "data" / "historical" / "ca_national_history.json"

# Fallback if the listing page can't be fetched (paths verified live).
KNOWN_REPORTS = [
    "warnreportfor7-1-2014to06-30-2015.pdf",
    "warn-report-for-7-1-2015-to-06-30-2016.pdf",
    "warn-report-for-7-1-2016-to-06-30-2017.pdf",
    "warn-report-for-7-1-2017-to-06-30-2018.pdf",
    "warn-report-for-7-1-2018-to-06-30-2019.pdf",
    "warn-report-for-7-1-2019-to-6-30-2020.pdf",
    "warn-report-for-7-1-2020-to-06-30-2021.pdf",
    "warn-report-for-7-1-2021-to-06-30-2022.pdf",
    "warn-report-for-7-1-2022-to-06-30-2023.pdf",
    "warn-report-for-7-1-2023-to-06-30-2024.pdf",
    "warn-report-for-7-1-2024-to-06-30-2025.pdf",
    "warn-report-for-7-1-25-to-6-30-26.pdf",
]

# Header text -> unified field. Checked in order; first substring match
# wins ("county" must precede "city" only in the sense that neither is a
# substring of the other — order here is just documentation).
HEADER_MAP = [
    ("notice", "notice_date"),
    ("effective", "effective_date"),
    ("received", None),  # present in every PDF; not part of the schema
    ("company", "company"),
    ("county", "county"),
    ("city", "city"),
    ("employee", "employees"),
    ("layoff", "layoff_type"),
    ("closure", "layoff_type"),
    ("address", "address"),
]


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def discover_report_paths() -> list:
    """Fiscal-year report PDF paths from the EDD listing page.

    Falls back to the verified KNOWN_REPORTS list if the page can't be
    fetched or yields nothing. Returned sorted by fiscal-year start so
    processing order (and therefore dedup order) is deterministic.
    """
    paths = None
    try:
        page = _fetch(LISTING_URL).decode("utf-8", errors="replace")
        hrefs = re.findall(r'href="([^"]*?warn[^"]*?\.pdf)"', page, re.I)
        names = sorted(
            {
                h.rsplit("/", 1)[-1]
                for h in hrefs
                if re.match(r"warn-?report-?for", h.rsplit("/", 1)[-1], re.I)
            }
        )
        if names:
            paths = [PDF_DIR + n for n in names]
            log.info(f"Discovered {len(paths)} report PDFs from listing page.")
    except Exception as e:
        log.warning(f"Listing page fetch failed ({e}); using known list.")
    if not paths:
        paths = [PDF_DIR + n for n in KNOWN_REPORTS]
    return sorted(paths, key=lambda p: (fiscal_year(p) or "", p))


def fiscal_year(name: str) -> str:
    """'…7-1-2019-to-6-30-2020.pdf' -> 'FY2019-20' (None if no match)."""
    m = re.search(r"(?:for)?(\d{1,2})-(\d{1,2})-(\d{2,4})", name)
    if not m:
        return None
    start = int(m.group(3))
    start = start + 2000 if start < 100 else start
    return f"FY{start}-{str(start + 1)[-2:]}"


def download(path: str, dest_dir: Path, throttle: list) -> Path:
    """Fetch one report PDF (cached by filename; 1 request/second)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.rsplit("/", 1)[-1]
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"Reusing cached {dest.name}")
        return dest
    since = time.monotonic() - throttle[0]
    if since < 1.0:
        time.sleep(1.0 - since)
    log.info(f"Downloading {EDD_BASE + path}")
    data = _fetch(EDD_BASE + path)
    throttle[0] = time.monotonic()
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"{path} is not a PDF (no %PDF magic)")
    dest.write_bytes(data)
    log.info(f"Saved {len(data)} bytes to {dest}")
    return dest


def _norm(cell) -> str:
    """One table cell -> clean single-line text (HTML entities decoded)."""
    return re.sub(r"\s+", " ", html.unescape(cell or "").replace("\n", " ")).strip()


def _parse_date(cell: str):
    """'0 6 / 1 8 / 2 0 14' or '06/18/2014' -> '2014-06-18'; else None."""
    s = re.sub(r"\s+", "", cell)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if not m:
        return None
    mm, dd, yy = (int(g) for g in m.groups())
    yy = yy + 2000 if yy < 100 else yy
    try:
        d = date(yy, mm, dd)
    except ValueError:
        return None
    # Guard against OCR-grade typos landing absurd years in the dataset.
    return d.isoformat() if 1988 <= yy <= 2099 else None


def _parse_int(cell: str) -> int:
    s = re.sub(r"[,\s]", "", cell)
    return int(s) if s.isdigit() else 0


def _map_header(cells: list):
    """Header row -> {column index: field} (None for ignored columns)."""
    colmap = {}
    for i, cell in enumerate(cells):
        low = cell.lower()
        for needle, field in HEADER_MAP:
            if needle in low:
                colmap[i] = field
                break
    return colmap


def _is_header(cells: list) -> bool:
    joined = " ".join(cells).lower()
    return "notice" in joined and "company" in joined


def parse_pdf(path: Path) -> tuple:
    """One report PDF -> (records, stats).

    stats carries per-report validation data: raw row count, drop reasons,
    and the official Notices/Employees totals from the trailing "Summary
    by Month" table. Note: the report body has one row per SITE, so row
    count can exceed the official notice count in years where one notice
    covered several sites (e.g. FY2022-23).
    """
    records = []
    stats = {
        "rows": 0,
        "blank_rows": 0,
        "no_company": 0,
        "undated": 0,
        "official_notices": None,
        "official_employees": None,
    }
    colmap = None
    fields = []
    in_summary = False
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for raw in table:
                    cells = [_norm(c) for c in raw]
                    if not any(cells):
                        stats["blank_rows"] += 1
                        continue
                    if "summary" in cells[0].lower():
                        in_summary = True  # summary runs to end of PDF
                        continue
                    if in_summary:
                        if cells[0] == "Total" and len(cells) >= 3:
                            stats["official_notices"] = _parse_int(cells[1])
                            stats["official_employees"] = _parse_int(cells[2])
                        continue
                    if _is_header(cells):
                        colmap = _map_header(cells)
                        fields = [f for f in colmap.values() if f]
                        continue
                    if colmap is None:
                        continue  # preamble text caught as a table row
                    stats["rows"] += 1
                    values = {
                        f: cells[i]
                        for i, f in colmap.items()
                        if f and i < len(cells)
                    }
                    company = values.get("company", "")
                    if not company:
                        stats["no_company"] += 1
                        continue
                    rec = {"company": company}
                    for f in ("notice_date", "effective_date"):
                        rec[f] = _parse_date(values.get(f, ""))
                    rec["employees"] = _parse_int(values.get("employees", ""))
                    for f in ("county", "city", "address", "layoff_type"):
                        if f in fields:
                            rec[f] = values.get(f, "")
                    if not (rec["notice_date"] or rec["effective_date"]):
                        stats["undated"] += 1
                        continue
                    records.append(rec)
    return records, stats


def _event_date(rec: dict) -> str:
    return rec.get("notice_date") or rec.get("effective_date")


def _sort_key(rec: dict) -> tuple:
    return (
        rec.get("notice_date") or "",
        rec["company"].lower(),
        rec.get("effective_date") or "",
        rec.get("county", ""),
        rec.get("city", ""),
        rec.get("address", ""),
        rec.get("employees", 0),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    ap.add_argument(
        "--download-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "warn_backfill",
        help="where to cache the downloaded PDFs",
    )
    ap.add_argument(
        "--output", type=Path, default=OUTPUT_FILE, help="history JSON to write"
    )
    args = ap.parse_args()

    floor = backfill.live_floor(STATE)
    log.info(f"CA live floor: {floor}")

    throttle = [0.0]
    seen = set()
    kept = []
    dropped = {"blank_rows": 0, "no_company": 0, "undated": 0, "duplicate": 0}
    excluded_live_era = 0
    per_year = {}
    for path in discover_report_paths():
        fy = fiscal_year(path.rsplit("/", 1)[-1]) or path.rsplit("/", 1)[-1]
        pdf_path = download(path, args.download_dir, throttle)
        records, stats = parse_pdf(pdf_path)
        for reason in ("blank_rows", "no_company", "undated"):
            dropped[reason] += stats[reason]
        year_kept = 0
        year_employees = 0
        for rec in records:
            key = warn_monitor._record_key(rec)
            if key in seen:
                dropped["duplicate"] += 1
                continue
            seen.add(key)
            if floor is not None and _event_date(rec) >= floor:
                excluded_live_era += 1
                continue
            kept.append(rec)
            year_kept += 1
            year_employees += rec["employees"]
        per_year[fy] = {
            "rows": stats["rows"],
            "kept": year_kept,
            "employees": year_employees,
            "official_notices": stats["official_notices"],
            "official_employees": stats["official_employees"],
        }

    kept.sort(key=_sort_key)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": "EDD fiscal-year WARN report archives",
        "records": kept,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    log.info(f"Wrote {len(kept)} records to {args.output}")

    # ---- validation report -------------------------------------------------
    print("\nPer-fiscal-year validation (rows = site-level lines parsed;")
    print("official = report's own Summary-by-Month Total row):")
    header = (
        f"{'fiscal year':12s} {'rows':>6s} {'kept':>6s} {'employees':>10s} "
        f"{'official_notices':>17s} {'official_employees':>19s}"
    )
    print(header)
    suspect = []
    for fy in sorted(per_year):
        s = per_year[fy]
        print(
            f"{fy:12s} {s['rows']:6d} {s['kept']:6d} {s['employees']:10d} "
            f"{str(s['official_notices']):>17s} "
            f"{str(s['official_employees']):>19s}"
        )
        if s["rows"] < 200:
            suspect.append(fy)
    if suspect:
        print(f"WARNING: suspiciously low row counts for: {suspect}")
    dates = sorted(_event_date(r) for r in kept)
    print(f"\ntotal_records: {len(kept)}")
    print(f"date_range: {dates[0]} .. {dates[-1]}" if dates else "date_range: n/a")
    print(f"employees_total: {sum(r['employees'] for r in kept)}")
    print(f"dropped_rows: {dropped}")
    print(f"excluded_live_era (>= {floor}): {excluded_live_era}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
