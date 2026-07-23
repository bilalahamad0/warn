"""
Backfill historical New York WARN notices (2016 - 2025).

Sources
-------
1. Big Local News public archive (Apache-2.0), one XLSX covering the
   pre-dashboard spreadsheet era 2016-01 through 2021-06:
   https://storage.googleapis.com/bln-data-public/warn-layoffs/ny_historical.xlsx
   Column crosswalk ported from BLN's warn-transformer
   (warn_transformer/transformers/ny.py, Apache-2.0).
2. NYS DOL legacy per-year listing pages, whose per-notice links redirect
   to standardized PDF postings parsed here with pdfplumber:
   - 2025 (Jan-Mar): https://dol.ny.gov/legacy-warn-notices  (live)
   - 2024:           https://dol.ny.gov/2024-warn-notices    (live)
   - 2023:           https://dol.ny.gov/2023-warn-notices    (live)
   - 2022:           gone from the live site ("Access denied"); read via the
                     Wayback Machine snapshot of the same page.
   - 2021:           the archive URL was https://dol.ny.gov/warn-notices-2021
                     (404 today); read via the Wayback Machine. Only notices
                     after 2021-06-30 are taken - the BLN XLSX covers H1.

Dedup rules
-----------
- Only records whose event date (notice_date, else effective_date) is
  strictly before ``warn_sources.backfill.live_floor('ny')`` are merged;
  the live store covers everything after it.
- Scraped notices are deduped on (event number, site address) so a notice
  amended across year pages lands once (newest posting wins).
- 2021-page notices whose event number already appears in the BLN XLSX are
  dropped (amendments of already-covered H1-2021 events).
- Records with no parseable date at all are dropped and counted.

Run:  .venv/bin/python scripts/backfill/ny.py
Downloads are cached (NY_BACKFILL_CACHE, default <tempdir>/warn-ny-backfill)
so re-runs are cheap; the merge itself is idempotent via _record_key.
"""

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pdfplumber
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from warn_sources.backfill import live_floor, merge_records  # noqa: E402

BLN_XLSX_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "ny_historical.xlsx"
)
WAYBACK = "https://web.archive.org"
# Listing pages, newest era first so event-number dedup keeps the most
# recent (amended) posting of a notice. (label, url, via_wayback)
LISTING_PAGES = [
    ("2025", "https://dol.ny.gov/legacy-warn-notices", False),
    ("2024", "https://dol.ny.gov/2024-warn-notices", False),
    ("2023", "https://dol.ny.gov/2023-warn-notices", False),
    (
        "2022",
        f"{WAYBACK}/web/20240110202709/https://dol.ny.gov/2022-warn-notices",
        True,
    ),
    (
        "2021",
        f"{WAYBACK}/web/20240110202709/https://dol.ny.gov/warn-notices-2021",
        True,
    ),
]
XLSX_MAX_DATE = "2021-06-30"  # last notice date covered by the BLN file

CACHE_DIR = Path(
    os.environ.get("NY_BACKFILL_CACHE")
    or Path(tempfile.gettempdir()) / "warn-ny-backfill"
)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

MONTHS = (
    "january february march april may june july august september "
    "october november december"
).split()
_MONTH_RE = re.compile(
    r"(" + "|".join(m.capitalize() for m in MONTHS) + r")\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
)
_NUM_DATE_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def _iso(year: int, month: int, day: int):
    if not (2013 <= year <= 2027 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def first_date(text):
    """First parseable date in free text -> ISO string or None."""
    if not text:
        return None
    text = str(text)
    candidates = []
    m = _ISO_DATE_RE.search(text)
    if m:
        candidates.append(
            (m.start(), _iso(int(m.group(1)), int(m.group(2)),
                             int(m.group(3))))
        )
    m = _MONTH_RE.search(text)
    if m:
        candidates.append(
            (m.start(), _iso(int(m.group(3)),
                             MONTHS.index(m.group(1).lower()) + 1,
                             int(m.group(2))))
        )
    m = _NUM_DATE_RE.search(text)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        candidates.append(
            (m.start(), _iso(year, int(m.group(1)), int(m.group(2))))
        )
    # Earliest valid match in the string wins.
    for _, value in sorted(candidates, key=lambda c: c[0]):
        if value:
            return value
    return None


def fetch(url: str, session: requests.Session, pause: float = 0.0) -> bytes:
    """GET with an on-disk cache and simple retry/backoff."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()
    cached = CACHE_DIR / key
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()
    last_err = None
    for delay in (0, 5, 20):
        if delay:
            time.sleep(delay)
        try:
            resp = session.get(url, headers=UA, timeout=90)
            if resp.status_code == 200 and resp.content:
                cached.write_bytes(resp.content)
                if pause:
                    time.sleep(pause)
                return resp.content
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code in (404, 403):
                break
        except requests.RequestException as exc:  # pragma: no cover
            last_err = str(exc)
    raise RuntimeError(f"fetch failed for {url}: {last_err}")


# ---------------------------------------------------------------------------
# Source 1: BLN historical XLSX (2016-01 .. 2021-06)
# ---------------------------------------------------------------------------


def parse_xlsx(path: Path):
    """BLN ny_historical.xlsx -> (records, xlsx_event_numbers).

    Crosswalk (BLN warn-transformer, Apache-2.0): Company -> company,
    Notice Date -> notice_date, Layoff Date (else Closing Date) ->
    effective_date, Number Affected -> employees, Dislocation Type ->
    layoff_type, County/City/Address verbatim, NAICS Description ->
    industry.
    """
    df = pd.read_excel(path)
    records, events = [], set()

    def cell(row, col):
        val = row.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        text = re.sub(r"\s+", " ", str(val)).strip()
        return "" if text.lower() in ("nan", "none", "----") else text

    for _, row in df.iterrows():
        company = cell(row, "Company")
        if not company:
            continue
        effective = first_date(cell(row, "Layoff Date")) or first_date(
            cell(row, "Closing Date")
        )
        event = cell(row, "Event #")
        if event:
            events.add(event)
        records.append(
            {
                "company": company,
                "notice_date": first_date(cell(row, "Notice Date")),
                "effective_date": effective,
                "employees": _to_int(cell(row, "Number Affected")),
                "layoff_type": cell(row, "Dislocation Type"),
                "county": cell(row, "County"),
                "city": cell(row, "City"),
                "address": cell(row, "Address"),
                "industry": cell(row, "NAICS Description"),
                "_event": event,
            }
        )
    return records, events


def _to_int(val) -> int:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Source 2: legacy listing pages -> per-notice PDFs
# ---------------------------------------------------------------------------


def listing_links(html: bytes, via_wayback: bool):
    """Per-notice URLs from a listing page, in page (newest-first) order."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html.decode("utf-8", errors="replace"), "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()  # some hrefs carry stray whitespace
        path = re.sub(r"^(?:https?://[^/]+)?(?:/web/\d+(?:im_|id_)?/"
                      r"https?://[^/]+)?", "", href)
        if not path.startswith("/warn-"):
            continue
        if any(s in path for s in ("dashboard", "worker-adjustment",
                                   "warn-notices")):
            continue
        if via_wayback:
            url = href if href.startswith("http") else WAYBACK + href
        else:
            url = "https://dol.ny.gov" + path
        if path not in seen:
            seen.add(path)
            out.append(url)
    return out


def _clean_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return "" if re.fullmatch(r"[-–— ]*", text) else text


def _company_block(lines):
    """Company name + address lines following a 'Company:' line."""
    stop = re.compile(
        r"^(County:|FEIN|Industry Type:|Contact:|Phone:|Business Type:|"
        r"Total Number|Number Affected|Impacted Site)"
    )
    name, addr = "", []
    for line in lines:
        line = _clean_line(line)
        if not line:
            continue
        if stop.match(line):
            break
        if not name:
            name = line
        else:
            addr.append(line)
    return name, ", ".join(addr)


def parse_pdf(blob: bytes, slug: str):
    """One WARN posting PDF -> list of unified records (site-level).

    Handles both NYS DOL layouts: the pre-2023 'OFFICE OF DISLOCATED
    WORKERS PROGRAM' single-site sheet and the 2023+ 'WARN UNIT' sheet
    with an 'Impacted Sites' section (one record per site).
    """
    if not blob.startswith(b"%PDF"):
        raise ValueError("not a PDF")
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    if not text.strip():
        raise ValueError("no extractable text")
    lines = text.split("\n")
    rescinded = "rescind" in slug.lower()

    def rx(pattern, chunk=text):
        m = re.search(pattern, chunk)
        return _clean_line(m.group(1)) if m else ""

    company, address = "", ""
    for i, line in enumerate(lines):
        if _clean_line(line).startswith("Company:"):
            rest = _clean_line(line)[len("Company:"):].strip()
            block = ([rest] if rest else []) + lines[i + 1:i + 8]
            company, address = _company_block(block)
            break

    if "OFFICE OF DISLOCATED WORKERS PROGRAM" in text or (
        "Impacted Site" not in text and "Number Affected" in text
    ):
        # Old layout: one site per posting.
        layoff_type = rx(r"Reason Stated for Filing:\s*([^\n]+)") or rx(
            r"Classification:\s*([^\n]+)"
        )
        effective = first_date(
            rx(r"Layoff Date:\s*([^\n]+)")
        ) or first_date(rx(r"Closing Date:\s*([^\n]+)"))
        record = {
            "company": company,
            "notice_date": first_date(rx(r"Date of Notice:\s*([^\n]+)")),
            "effective_date": effective,
            "employees": _to_int(rx(r"Number Affected:\s*([\d,]+)")),
            "layoff_type": (layoff_type + (" Rescinded" if rescinded else ""))
            .strip(),
            "county": rx(r"County:\s*([^|\n]+)"),
            "city": "",
            "address": address,
            "industry": "",
            "_event": rx(r"Event Number:\s*([\w./-]+)"),
        }
        return [record]

    # New layout: header totals + one section per impacted site.
    kind = "Closure" if re.search(r"Reason For Closure|Closure Start", text) \
        else "Layoff"
    layoff_type = (kind + (" Rescinded" if rescinded else "")).strip()
    industry = rx(r"Industry Type:\s*(?:[\d\s\-–]+:\s*)?([^\n]+)")
    effective = first_date(
        rx(r"(?:Layoff|Closure) Start Date:\s*([^\n]+)")
    )
    header_notice = first_date(rx(r"Date of Notice:\s*([^\n]+)"))
    total = _to_int(rx(r"Total Number of Affected Workers:\s*([\d,]+)"))

    site_chunks = re.split(r"Event Number:", text)[1:]
    records = []
    for chunk in site_chunks:
        records.append(
            {
                "company": company,
                "notice_date": first_date(
                    rx(r"Date of Notice:\s*([^\n]+)", chunk)
                ) or header_notice,
                "effective_date": effective,
                "employees": _to_int(
                    rx(r"Number of Affected Employees at Site:\s*([\d,]+)",
                       chunk)
                ),
                "layoff_type": layoff_type,
                "county": rx(r"County:\s*([^|\n]+)", chunk),
                "city": "",
                "address": rx(r"Address:\s*([^\n]+)", chunk) or address,
                "industry": industry,
                "_event": _clean_line(chunk.split("\n", 1)[0]),
            }
        )
    if not records:
        records.append(
            {
                "company": company,
                "notice_date": header_notice,
                "effective_date": effective,
                "employees": total,
                "layoff_type": layoff_type,
                "county": rx(r"County:\s*([^|\n]+)"),
                "city": "",
                "address": address,
                "industry": industry,
                "_event": rx(r"Event Number:\s*([\w./-]+)"),
            }
        )
    return records


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    session = requests.Session()
    notes = {"pdf_failures": [], "dropped_dateless": 0}

    xlsx_blob = fetch(BLN_XLSX_URL, session)
    xlsx_path = CACHE_DIR / "ny_historical.xlsx"
    xlsx_path.write_bytes(xlsx_blob)
    xlsx_records, xlsx_events = parse_xlsx(xlsx_path)
    print(f"[xlsx] {len(xlsx_records)} rows (2016-01 .. {XLSX_MAX_DATE})")

    scraped = []
    for label, page_url, via_wb in LISTING_PAGES:
        try:
            links = listing_links(fetch(page_url, session), via_wb)
        except RuntimeError as exc:
            notes["pdf_failures"].append(f"listing {label}: {exc}")
            continue
        ok = 0
        for url in links:
            slug = url.rsplit("/", 1)[-1]
            try:
                blob = fetch(url, session, pause=0.4 if via_wb else 0.15)
                recs = parse_pdf(blob, slug)
            except Exception as exc:
                notes["pdf_failures"].append(f"{label} {slug}: {exc}")
                continue
            for r in recs:
                r["_year_page"] = label
            scraped.extend(recs)
            ok += 1
        print(f"[{label}] {ok}/{len(links)} notices parsed")

    # Dedup scraped postings on (event number, address); pages were walked
    # newest-first, so the most recent (amended) posting wins.
    deduped, seen = [], set()
    dup_events = 0
    for r in scraped:
        key = (r["_event"], r["address"].lower()[:40]) if r["_event"] else None
        if key and key in seen:
            dup_events += 1
            continue
        if key:
            seen.add(key)
        deduped.append(r)

    # 2021 page: keep only the half-year the XLSX does not cover, and skip
    # amendments of events already present in the XLSX.
    filtered, dropped_2021_overlap, dropped_2021_xlsx_event = [], 0, 0
    for r in deduped:
        if r.pop("_year_page", None) == "2021":
            event_date = r["notice_date"] or r["effective_date"]
            if r["_event"] and r["_event"] in xlsx_events:
                dropped_2021_xlsx_event += 1
                continue
            if not event_date or event_date <= XLSX_MAX_DATE:
                dropped_2021_overlap += 1
                continue
        filtered.append(r)

    candidates = xlsx_records + filtered
    for r in candidates:
        r.pop("_event", None)

    floor = live_floor("ny")
    print(f"[floor] live store floor: {floor}")
    kept, dropped_floor = [], 0
    for r in candidates:
        event_date = r["notice_date"] or r["effective_date"]
        if not event_date:
            notes["dropped_dateless"] += 1
            continue
        if floor and event_date >= floor:
            dropped_floor += 1
            continue
        kept.append(r)

    summary = merge_records("ny", kept)
    result = {
        "merged_input": len(kept),
        "cumulative": summary,
        "xlsx_rows": len(xlsx_records),
        "scraped_site_records": len(scraped),
        "deduped_amended_postings": dup_events,
        "dropped_2021_overlap": dropped_2021_overlap,
        "dropped_2021_xlsx_event": dropped_2021_xlsx_event,
        "dropped_dateless": notes["dropped_dateless"],
        "dropped_at_live_floor": dropped_floor,
        "pdf_failures": notes["pdf_failures"],
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
