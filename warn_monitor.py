"""
warn_monitor.py
---------------
Downloads the CA EDD WARN XLSX, parses it robustly, detects changes vs the
last known snapshot, and persists cleaned data + change logs.

Usage:
    python3 warn_monitor.py               # full run
    python3 warn_monitor.py --dry-run     # parse only, no file writes
    python3 warn_monitor.py --force       # ignore ETag, always re-download
"""

import json
import hashlib
import logging
import argparse
import re
from datetime import datetime, date, timezone
from pathlib import Path

from typing import Optional

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

WARN_XLSX_URL = (
    "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx"
)
LOCAL_XLSX = BASE_DIR / "file.xlsx"
META_FILE = DATA_DIR / "meta.json"
SNAPSHOT_FILE = DATA_DIR / "warn_snapshot.json"
LATEST_FILE = DATA_DIR / "warn_latest.json"
CHANGELOG_FILE = DATA_DIR / "changelog.jsonl"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("warn_monitor")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """MD5 of a file, used for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_meta() -> dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return {}


def _save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, indent=2, default=str))


def _fix_company_name(name: str) -> str:
    """Normalise HTML entities and whitespace in company names."""
    name = str(name).strip()
    name = re.sub(r"&rsquo;", "'", name, flags=re.IGNORECASE)
    name = re.sub(r"&amp;", "&", name, flags=re.IGNORECASE)
    name = re.sub(r"&nbsp;", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name)
    # Deduplicate known variants
    juul_pattern = re.compile(r"ju+l+", re.IGNORECASE)
    if juul_pattern.search(name):
        name = "Juul Labs, Inc."
    return name


def _safe_int(val) -> Optional[int]:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _safe_date(val) -> Optional[str]:
    if pd.isna(val) if hasattr(pd, "isna") else val != val:
        return None
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception:
        return str(val).strip() or None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_xlsx(force: bool = False):
    """
    Download WARN XLSX with ETag caching.
    Returns (changed: bool, local_path: str).
    """
    meta = _load_meta()
    headers = {"User-Agent": "WARNMonitor/2.0"}
    if not force and meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if not force and meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]

    log.info("Requesting WARN XLSX from EDD …")
    resp = requests.get(WARN_XLSX_URL, headers=headers, timeout=60)

    if resp.status_code == 304:
        log.info("EDD server: 304 Not Modified — data unchanged.")
        return False, str(LOCAL_XLSX)

    resp.raise_for_status()

    # Write file
    LOCAL_XLSX.write_bytes(resp.content)
    new_hash = _file_hash(LOCAL_XLSX)
    old_hash = meta.get("file_hash", "")

    meta.update(
        {
            "etag": resp.headers.get("ETag", ""),
            "last_modified": resp.headers.get("Last-Modified", ""),
            "file_hash": new_hash,
            "last_checked": datetime.now(timezone.utc).isoformat() + "Z",
            "url": WARN_XLSX_URL,
        }
    )
    _save_meta(meta)

    changed = new_hash != old_hash
    if changed:
        log.info(f"File changed (hash: {old_hash[:8]} → {new_hash[:8]})")
    else:
        log.info("File downloaded but content hash identical — no change.")
    return changed, str(LOCAL_XLSX)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _detect_sheet_format(xls: pd.ExcelFile) -> str:
    """Return the correct sheet name for the WARN data."""
    sheets = xls.sheet_names
    log.info(f"Available sheets: {sheets}")
    for name in ["Detailed WARN Report ", "Detailed WARN Report", "Sheet1"]:
        if name in sheets:
            return name
    return sheets[0]


def _parse_sheet1(df: pd.DataFrame) -> pd.DataFrame:
    """Parse the modern 'Sheet1' format."""
    col_map = {}
    for col in df.columns:
        lc = str(col).lower().replace("\n", " ").strip()
        if "notice" in lc and "date" in lc:
            col_map["notice_date"] = col
        elif "effective" in lc and "date" in lc:
            col_map["effective_date"] = col
        elif "company" in lc:
            col_map["company"] = col
        elif "no" in lc and "employee" in lc:
            col_map["employees"] = col
        elif "county" in lc:
            col_map["county"] = col
        elif "city" in lc:
            col_map["city"] = col
        elif "layoff" in lc or "warn" in lc or "type" in lc:
            col_map["layoff_type"] = col
        elif "address" in lc:
            col_map["address"] = col
        elif "industry" in lc:
            col_map["industry"] = col

    rows = []
    for _, row in df.iterrows():
        company = _fix_company_name(row.get(col_map.get("company", ""), ""))
        if not company or company.lower() in ("company", "nan", ""):
            continue
        emp = _safe_int(row.get(col_map.get("employees", ""), None))
        if emp is None:
            continue
        rows.append(
            {
                "company": company,
                "notice_date": _safe_date(row.get(col_map.get("notice_date"), None)),
                "effective_date": _safe_date(
                    row.get(col_map.get("effective_date"), None)
                ),
                "employees": emp,
                "county": str(row.get(col_map.get("county", ""), "")).strip(),
                "city": str(row.get(col_map.get("city", ""), "")).strip(),
                "layoff_type": str(row.get(col_map.get("layoff_type", ""), "")).strip(),
                "address": str(row.get(col_map.get("address", ""), "")).strip(),
                "industry": str(row.get(col_map.get("industry", ""), "")).strip(),
            }
        )
    return pd.DataFrame(rows)


def _parse_detailed_sheet(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Parse the 'Detailed WARN Report' format with Unnamed columns."""
    # Find the header row by looking for 'Company' keyword
    header_row = None
    for i, row in df_raw.iterrows():
        vals = [str(v).lower() for v in row.values]
        if any("company" in v for v in vals):
            header_row = i
            break

    if header_row is not None:
        df = df_raw.iloc[header_row + 1 :].copy()
        df.columns = df_raw.iloc[header_row].values
        df = df.reset_index(drop=True)
    else:
        df = df_raw.copy()

    # Map column positions
    cols = list(df.columns)
    log.info(f"Detailed sheet columns: {cols}")

    # Try to find columns by content analysis
    col_indices = {}
    for i, col in enumerate(cols):
        col_s = str(col).lower().strip()
        if "notice" in col_s and "date" in col_s:
            col_indices["notice_date"] = col
        elif "effective" in col_s:
            col_indices["effective_date"] = col
        elif "company" in col_s:
            col_indices["company"] = col
        elif "employ" in col_s:
            col_indices["employees"] = col
        elif "county" in col_s:
            col_indices["county"] = col
        elif "city" in col_s:
            col_indices["city"] = col
        elif "layoff" in col_s or "type" in col_s or "warn" in col_s:
            col_indices["layoff_type"] = col
        elif "address" in col_s:
            col_indices["address"] = col
        elif "industry" in col_s:
            col_indices["industry"] = col

    # Fallback to positional (Detailed WARN Report sheet layout):
    # 0=County/Parish, 1=Notice Date, 2=Processed Date, 3=Effective Date,
    # 4=Company, 5=Layoff/Closure, 6=No. Of Employees, 7=Address, 8=Related Industry
    if not col_indices:
        positional = {
            "county": cols[0] if len(cols) > 0 else None,
            "notice_date": cols[1] if len(cols) > 1 else None,
            "effective_date": cols[3] if len(cols) > 3 else None,
            "company": cols[4] if len(cols) > 4 else None,
            "layoff_type": cols[5] if len(cols) > 5 else None,
            "employees": cols[6] if len(cols) > 6 else None,
            "address": cols[7] if len(cols) > 7 else None,
            "industry": cols[8] if len(cols) > 8 else None,
        }
        col_indices = {k: v for k, v in positional.items() if v}

    rows = []
    for _, row in df.iterrows():
        company_col = col_indices.get("company")
        if not company_col:
            continue
        company = _fix_company_name(row.get(company_col, ""))
        if not company or company.lower() in ("company", "nan", ""):
            continue
        emp_col = col_indices.get("employees")
        emp = _safe_int(row.get(emp_col, None)) if emp_col else None
        if emp is None:
            continue
        rows.append(
            {
                "company": company,
                "notice_date": _safe_date(
                    row.get(col_indices.get("notice_date"), None)
                ),
                "effective_date": _safe_date(
                    row.get(col_indices.get("effective_date"), None)
                ),
                "employees": emp,
                "county": str(row.get(col_indices.get("county", ""), "")).strip(),
                "city": str(row.get(col_indices.get("city", ""), "")).strip(),
                "layoff_type": str(
                    row.get(col_indices.get("layoff_type", ""), "")
                ).strip(),
                "address": str(row.get(col_indices.get("address", ""), "")).strip(),
                "industry": str(row.get(col_indices.get("industry", ""), "")).strip(),
            }
        )
    return pd.DataFrame(rows)


def parse_warn_xlsx(xlsx_path: str) -> pd.DataFrame:
    """
    Robustly parse WARN XLSX regardless of sheet format.
    Returns a normalised DataFrame.
    """
    log.info(f"Parsing {xlsx_path} …")
    xls = pd.ExcelFile(xlsx_path)
    sheet = _detect_sheet_format(xls)
    log.info(f"Using sheet: '{sheet}'")

    df_raw = pd.read_excel(xlsx_path, sheet_name=sheet, header=None)

    if sheet == "Sheet1":
        df_named = pd.read_excel(xlsx_path, sheet_name=sheet, parse_dates=True)
        df = _parse_sheet1(df_named)
    else:
        df = _parse_detailed_sheet(df_raw)

    # Drop rows with null effective_date or < 1 employee
    df = df[df["employees"] > 0]
    df = df.dropna(subset=["company"])
    df["employees"] = df["employees"].astype(int)

    # Merge duplicate company entries on same effective date
    if "effective_date" in df.columns:
        agg_dict = {"employees": "sum", "notice_date": "first", "address": "first"}
        if "industry" in df.columns:
            agg_dict["industry"] = "first"
        df = (
            df.groupby(
                ["company", "effective_date", "county", "city", "layoff_type"],
                dropna=False,
            )
            .agg(agg_dict)
            .reset_index()
        )

    df = df.sort_values("effective_date", na_position="last").reset_index(drop=True)

    log.info(
        f"Parsed {len(df)} WARN records spanning "
        f"{df['effective_date'].min()} → {df['effective_date'].max()}"
    )
    return df


# ---------------------------------------------------------------------------
# Diff / change detection
# ---------------------------------------------------------------------------


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def detect_changes(new_df: pd.DataFrame) -> dict:
    """Compare new data vs saved snapshot. Returns diff summary."""
    new_records = _df_to_records(new_df)

    if not SNAPSHOT_FILE.exists():
        log.info("No snapshot found — treating all records as new.")
        return {
            "new_count": len(new_records),
            "removed_count": 0,
            "new_entries": new_records[:50],  # cap at 50 for display
            "removed_entries": [],
            "total_employees_new": sum(r.get("employees", 0) for r in new_records),
        }

    old_payload = json.loads(SNAPSHOT_FILE.read_text())
    old_records = old_payload.get("records", [])

    # Build lookup keys
    def key(r):
        return f"{r.get('company','')}__{r.get('effective_date','')}__{r.get('employees','')}"

    old_keys = {key(r) for r in old_records}
    new_keys = {key(r) for r in new_records}

    added = [r for r in new_records if key(r) not in old_keys]
    removed = [r for r in old_records if key(r) not in new_keys]

    return {
        "new_count": len(added),
        "removed_count": len(removed),
        "new_entries": added[:50],
        "removed_entries": removed[:50],
        "total_employees_new": sum(r.get("employees", 0) for r in added),
        "total_employees_removed": sum(r.get("employees", 0) for r in removed),
    }


def _log_change(diff: dict, dry_run: bool = False):
    """Append change event to the changelog."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        **diff,
    }
    if diff["new_count"] > 0 or diff["removed_count"] > 0:
        log.info(
            f"Changes: +{diff['new_count']} new, -{diff['removed_count']} removed records"
        )
    else:
        log.info("No data changes detected.")

    if not dry_run:
        with open(CHANGELOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def save_latest(df: pd.DataFrame, dry_run: bool = False):
    """Save current data as latest + rotate snapshot."""
    records = _df_to_records(df)
    summary = {
        "total_records": len(records),
        "total_employees": int(df["employees"].sum()),
        "date_range_start": df["effective_date"].min(),
        "date_range_end": df["effective_date"].max(),
        "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        "source_url": WARN_XLSX_URL,
        "records": records,
    }
    if not dry_run:
        # Rotate: latest → snapshot
        if LATEST_FILE.exists():
            SNAPSHOT_FILE.write_text(LATEST_FILE.read_text())
        LATEST_FILE.write_text(json.dumps(summary, indent=2, default=str))
        log.info(f"Saved {len(records)} records to {LATEST_FILE}")
    else:
        log.info(f"[DRY-RUN] Would save {len(records)} records.")
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(dry_run: bool = False, force: bool = False) -> dict:
    """
    Full monitor run. Returns a result dict with stats + diff.
    """
    log.info("=" * 60)
    log.info(f"WARN Monitor — {datetime.now(timezone.utc).isoformat()}Z")
    log.info("=" * 60)

    # 1. Download
    file_changed, xlsx_path = download_xlsx(force=force)

    # 2. Parse
    df = parse_warn_xlsx(xlsx_path)

    # 3. Detect changes
    diff = detect_changes(df)
    _log_change(diff, dry_run=dry_run)

    # 4. Persist
    summary = save_latest(df, dry_run=dry_run)

    result = {
        "file_changed": file_changed,
        "diff": diff,
        "summary": {k: v for k, v in summary.items() if k != "records"},
    }
    log.info("Monitor run complete.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CA WARN Layoff Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no writes")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, indent=2, default=str))
