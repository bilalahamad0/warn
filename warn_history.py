"""
warn_history.py
---------------
Downloads and parses historical California WARN PDF reports from 2014 to
the current year, extracts layoff records, and merges them with the live
XLSX dataset to produce a unified multi-year dataset.

Usage:
    python3 warn_history.py               # download + parse all missing years
    python3 warn_history.py --force       # re-download all, even if cached
    python3 warn_history.py --year 2022   # one year only
"""

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import pdfplumber
import pandas as pd

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
HIST_DIR = DATA_DIR / "historical"
CACHE_DIR = HIST_DIR / "pdfs"
HIST_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

COMBINED_FILE = DATA_DIR / "warn_all_years.json"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("warn_history")


# ---------------------------------------------------------------------------
# PDF URL catalogue  (2014 → present)
# New fiscal year begins July 1; label = start_year
# ---------------------------------------------------------------------------

PDF_CATALOGUE = {
    2014: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warnreportfor7-1-2014to06-30-2015.pdf",
    2015: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2015-to-06-30-2016.pdf",
    2016: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2016-to-06-30-2017.pdf",
    2017: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2017-to-06-30-2018.pdf",
    2018: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2018-to-06-30-2019.pdf",
    2019: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2019-to-6-30-2020.pdf",
    2020: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2020-to-06-30-2021.pdf",
    2021: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2021-to-06-30-2022.pdf",
    2022: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2022-to-06-30-2023.pdf",
    2023: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2023-to-06-30-2024.pdf",
    2024: "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn-report-for-7-1-2024-to-06-30-2025.pdf",
}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_pdf(year: int, force: bool = False) -> Optional[Path]:
    url = PDF_CATALOGUE.get(year)
    if not url:
        log.warning(f"No PDF URL for year {year}")
        return None

    pdf_path = CACHE_DIR / f"warn_{year}.pdf"
    if pdf_path.exists() and not force:
        log.info(f"  [{year}] Using cached PDF")
        return pdf_path

    log.info(f"  [{year}] Downloading {url} …")
    try:
        resp = requests.get(url, timeout=120, headers={"User-Agent": "WARNMonitor/2.0"})
        resp.raise_for_status()
        pdf_path.write_bytes(resp.content)
        log.info(f"  [{year}] Saved {len(resp.content) // 1024} KB")
        return pdf_path
    except Exception as e:
        log.error(f"  [{year}] Download failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _safe_int(val) -> Optional[int]:
    try:
        cleaned = str(val).replace(",", "").replace(" ", "").strip()
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _safe_date(val_str: str) -> Optional[str]:
    if not val_str or str(val_str).strip() in ("", "nan", "None"):
        return None
    # Strip internal spaces from spaced-digit PDF artifacts: "0 6 / 1 8 / 2 0 14" → "06/18/2014"
    text = re.sub(r"\s+", "", str(val_str)).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _fix_company(name: str) -> str:
    name = str(name).strip()
    name = re.sub(r"&rsquo;", "'", name, flags=re.IGNORECASE)
    name = re.sub(r"&amp;", "&", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name)
    return name


# ---------------------------------------------------------------------------
# PDF parser (uses pdfplumber)
# ---------------------------------------------------------------------------

HEADER_KEYWORDS = {
    "county": ["county", "parish"],
    "notice": ["notice", "received"],
    "effective": ["effective"],
    "company": ["company", "employer"],
    "employees": ["employee", "no.", "number", "laid"],
    "address": ["address"],
    "type": ["layoff", "closure", "type"],
    "industry": ["industry"],
}


def _match_col(header: str) -> Optional[str]:
    h = str(header).lower().strip()
    for field, keywords in HEADER_KEYWORDS.items():
        if any(kw in h for kw in keywords):
            return field
    return None


def _extract_table_from_page(page) -> list[dict]:
    """Extract all rows from a single pdfplumber page."""
    tables = page.extract_tables()
    rows = []
    for table in tables:
        if not table or len(table) < 2:
            continue
        # First row = header
        header_row = table[0]
        col_map = {}
        for i, h in enumerate(header_row):
            field = _match_col(str(h))
            if field and field not in col_map:
                col_map[field] = i

        if "company" not in col_map or "employees" not in col_map:
            continue  # Not a data table

        for row in table[1:]:
            if len(row) <= max(col_map.values()):
                continue
            company = _fix_company(row[col_map["company"]])
            if not company or "company" in company.lower() or not company.strip():
                continue
            emp = _safe_int(row[col_map["employees"]])
            if emp is None or emp < 1:
                continue

            rows.append(
                {
                    "company": company,
                    "employees": emp,
                    "notice_date": (
                        _safe_date(
                            row[col_map.get("notice", col_map.get("effective", 0))]
                        )
                        if col_map.get("notice")
                        else None
                    ),
                    "effective_date": (
                        _safe_date(row[col_map["effective"]])
                        if "effective" in col_map
                        else None
                    ),
                    "county": (
                        str(row[col_map["county"]]).strip()
                        if "county" in col_map
                        else ""
                    ),
                    "address": (
                        str(row[col_map.get("address", 0)]).strip()
                        if "address" in col_map
                        else ""
                    ),
                    "layoff_type": (
                        str(row[col_map.get("type", 0)]).strip()
                        if "type" in col_map
                        else ""
                    ),
                }
            )
    return rows


def parse_pdf(pdf_path: Path, fiscal_year: int) -> list[dict]:
    """Parse a single WARN PDF and return normalised records."""
    records = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            log.info(f"  Parsing {pdf_path.name} ({len(pdf.pages)} pages) …")
            for page in pdf.pages:
                page_rows = _extract_table_from_page(page)
                records.extend(page_rows)
    except Exception as e:
        log.error(f"  PDF parse error for {pdf_path.name}: {e}")
        return []

    # Tag records with the fiscal year
    for r in records:
        r["fiscal_year"] = fiscal_year
        r["source"] = "pdf"

    log.info(f"  Extracted {len(records)} records from {pdf_path.name}")
    return records


# ---------------------------------------------------------------------------
# Save per-year JSON
# ---------------------------------------------------------------------------


def _year_file(year: int) -> Path:
    return HIST_DIR / f"warn_{year}.json"


def _save_year(year: int, records: list[dict]):
    _year_file(year).write_text(
        json.dumps(
            {
                "fiscal_year": year,
                "record_count": len(records),
                "total_employees": sum(r.get("employees", 0) for r in records),
                "records": records,
            },
            indent=2,
            default=str,
        )
    )


def _load_year(year: int) -> list[dict]:
    f = _year_file(year)
    if not f.exists():
        return []
    return json.loads(f.read_text()).get("records", [])


# ---------------------------------------------------------------------------
# Merge with live XLSX data
# ---------------------------------------------------------------------------


def merge_with_live() -> dict:
    """
    Combine all historical PDFs + live XLSX into a unified dataset.
    Returns a summary dict with total counts and per-year breakdown.
    """
    all_records = []
    yearly_summary = []

    # Historical years
    for year in sorted(PDF_CATALOGUE.keys()):
        recs = _load_year(year)
        c = len(recs)
        e = sum(r.get("employees", 0) for r in recs)
        yearly_summary.append(
            {
                "fiscal_year": year,
                "label": f"FY {year}-{str(year+1)[2:]}",
                "records": c,
                "employees": e,
                "source": "pdf",
            }
        )
        all_records.extend(recs)

    # Live XLSX (current year)
    latest_file = DATA_DIR / "warn_latest.json"
    if latest_file.exists():
        payload = json.loads(latest_file.read_text())
        live_recs = payload.get("records", [])
        for r in live_recs:
            r["fiscal_year"] = "current"
            r["source"] = "xlsx"
        c = len(live_recs)
        e = sum(r.get("employees", 0) for r in live_recs)
        yearly_summary.append(
            {
                "fiscal_year": "current",
                "label": "FY 2025-26 (Live)",
                "records": c,
                "employees": e,
                "source": "xlsx",
            }
        )
        all_records.extend(live_recs)

    combined = {
        "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
        "total_records": len(all_records),
        "total_employees": sum(r.get("employees", 0) for r in all_records),
        "yearly_summary": yearly_summary,
        "records": all_records,
    }
    COMBINED_FILE.write_text(json.dumps(combined, indent=2, default=str))
    log.info(
        f"Combined dataset: {len(all_records):,} records across {len(yearly_summary)} years"
    )
    return combined


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(force: bool = False, year: Optional[int] = None) -> dict:
    years = [year] if year else sorted(PDF_CATALOGUE.keys())

    for y in years:
        year_file = _year_file(y)
        if year_file.exists() and not force:
            log.info(f"Year {y}: already parsed — skipping (use --force to re-parse)")
            continue

        log.info(f"Processing FY {y}-{y+1} …")
        pdf_path = _download_pdf(y, force=force)
        if not pdf_path:
            continue

        records = parse_pdf(pdf_path, fiscal_year=y)
        if records:
            _save_year(y, records)
        else:
            log.warning(
                f"Year {y}: no records extracted from PDF — may be scanned image"
            )

    return merge_with_live()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WARN Historical PDF Parser")
    parser.add_argument(
        "--force", action="store_true", help="Force re-download of all PDFs"
    )
    parser.add_argument(
        "--year", type=int, help="Process a single fiscal year (e.g. 2022)"
    )
    args = parser.parse_args()
    result = run(force=args.force, year=args.year)
    for s in result["yearly_summary"]:
        print(f"  {s['label']}: {s['records']:,} records, {s['employees']:,} employees")
    print(
        f"\nTotal: {result['total_records']:,} records, {result['total_employees']:,} employees"
    )
