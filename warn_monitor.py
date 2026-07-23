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
from collections import defaultdict
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
CUMULATIVE_FILE = DATA_DIR / "warn_cumulative.json"
CHANGELOG_FILE = DATA_DIR / "changelog.jsonl"
# Cumulative ledger of every notice we have ever alerted on. Change detection
# for *alerts* keys off this, not a single prior run, because the EDD feed
# intermittently flip-flops between two versions of the spreadsheet across
# consecutive fetches (see detect_changes for the full rationale).
NOTIFIED_FILE = DATA_DIR / "notified_keys.json"
# Cumulative ledger of notices we have already reported as *amended*. The EDD
# feed oscillates between two versions of the spreadsheet on consecutive fetches
# (see detect_changes), so a single genuine amendment — e.g. an effective date
# revised from one day to another — keeps looking "newly amended" on every swing.
# Keying amendment alerts off this ledger guarantees each revision is reported
# at most once, and identifies the canonical (post-amendment) version of a
# notice so the cumulative store can evict the superseded one.
AMENDED_FILE = DATA_DIR / "amended_keys.json"

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


def _load_meta(meta_file: Optional[Path] = None) -> dict:
    meta_file = meta_file if meta_file is not None else META_FILE
    if meta_file.exists():
        return json.loads(meta_file.read_text())
    return {}


def _save_meta(meta: dict, meta_file: Optional[Path] = None):
    meta_file = meta_file if meta_file is not None else META_FILE
    meta_file.write_text(json.dumps(meta, indent=2, default=str))


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


def download_xlsx(
    force: bool = False,
    *,
    url: Optional[str] = None,
    meta_file: Optional[Path] = None,
    local_path: Optional[Path] = None,
):
    """
    Download a WARN data file with ETag/Last-Modified caching.
    Returns (changed: bool, local_path: str).

    Defaults resolve at call time to this module's CA constants, so the
    historical single-state behavior (and tests that patch the globals) are
    unchanged; warn_sources passes per-state paths through the keywords.
    """
    url = url or WARN_XLSX_URL
    local_path = local_path if local_path is not None else LOCAL_XLSX
    meta = _load_meta(meta_file)
    headers = {"User-Agent": "WARNMonitor/2.0"}
    if not force and meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if not force and meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]

    log.info(f"Requesting WARN data from {url} …")
    resp = requests.get(url, headers=headers, timeout=60)

    if resp.status_code == 304:
        log.info("Server: 304 Not Modified — data unchanged.")
        return False, str(local_path)

    resp.raise_for_status()

    # Write file
    local_path.write_bytes(resp.content)
    new_hash = _file_hash(local_path)
    old_hash = meta.get("file_hash", "")

    meta.update(
        {
            "etag": resp.headers.get("ETag", ""),
            "last_modified": resp.headers.get("Last-Modified", ""),
            "file_hash": new_hash,
            "last_checked": datetime.now(timezone.utc).isoformat() + "Z",
            "url": url,
        }
    )
    _save_meta(meta, meta_file)

    changed = new_hash != old_hash
    if changed:
        log.info(f"File changed (hash: {old_hash[:8]} → {new_hash[:8]})")
    else:
        log.info("File downloaded but content hash identical — no change.")
    return changed, str(local_path)


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


def _notice_key(r: dict) -> str:
    """Stable identity for a single WARN notice (company + date + headcount)."""
    return "__".join(
        str(r.get(k, "")) for k in ("company", "effective_date", "employees")
    )


def _anchor_key(r: dict) -> tuple:
    """Identity of a *filing* that survives an EDD amendment.

    Excludes the mutable fields (effective_date, employees) that EDD revises so
    a notice whose effective date or headcount is later corrected still maps to
    the same filing. company + county + city + notice_date anchors it; multi-site
    notices for one company stay distinct via county/city.
    """
    return (
        str(r.get("company", "")).strip().lower(),
        str(r.get("county", "")).strip().lower(),
        str(r.get("city", "")).strip().lower(),
        str(r.get("notice_date", ""))[:10],
    )


def _load_keys_file(path: Path) -> set:
    """Load a cumulative key ledger, tolerating a missing or corrupt file."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        keys = data.get("keys", []) if isinstance(data, dict) else data
        return set(keys)
    except Exception as e:
        log.warning(f"Could not read {path.name} ({e}) — treating as empty.")
        return set()


def _save_keys_file(keys: set, path: Path) -> None:
    """Persist a key ledger (sorted, for small/stable git diffs)."""
    payload = {
        "count": len(keys),
        "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        "keys": sorted(keys),
    }
    path.write_text(json.dumps(payload, indent=2))


def _record_keys(keys, path: Path, what: str) -> None:
    keys = [k for k in (keys or []) if k]
    if not keys:
        return
    ledger = _load_keys_file(path)
    before = len(ledger)
    ledger.update(keys)
    added_n = len(ledger) - before
    if added_n:
        _save_keys_file(ledger, path)
        log.info(f"Recorded {added_n} {what} notice(s) to {path.name}.")


def _load_notified_keys(notified_file: Optional[Path] = None) -> set:
    """Load the cumulative set of notice keys we have already alerted on."""
    return _load_keys_file(notified_file if notified_file is not None else NOTIFIED_FILE)


def _save_notified_keys(keys: set, notified_file: Optional[Path] = None) -> None:
    """Persist the notified-keys ledger (sorted, for small/stable git diffs)."""
    _save_keys_file(keys, notified_file if notified_file is not None else NOTIFIED_FILE)


def record_notified_keys(keys, notified_file: Optional[Path] = None) -> None:
    """Add keys to the ledger so those notices never trigger another alert.

    Called by warn_publish *after* an alert email is sent successfully, so a
    failed send is retried on the next run rather than silently swallowed.
    """
    _record_keys(keys, notified_file if notified_file is not None else NOTIFIED_FILE, "new")


def _load_amended_keys(amended_file: Optional[Path] = None) -> set:
    """Load the cumulative set of notice keys we have already alerted on as amended."""
    return _load_keys_file(amended_file if amended_file is not None else AMENDED_FILE)


def _save_amended_keys(keys: set, amended_file: Optional[Path] = None) -> None:
    """Persist the amended-keys ledger (sorted, for small/stable git diffs)."""
    _save_keys_file(keys, amended_file if amended_file is not None else AMENDED_FILE)


def record_amended_keys(keys, amended_file: Optional[Path] = None) -> None:
    """Add keys to the amended ledger so those amendments are never re-reported.

    Called by warn_publish *after* an alert email sends successfully, mirroring
    record_notified_keys — a failed send is retried next run rather than lost.
    """
    _record_keys(keys, amended_file if amended_file is not None else AMENDED_FILE, "amended")


def detect_changes(
    new_df: pd.DataFrame,
    dry_run: bool = False,
    *,
    latest_file: Optional[Path] = None,
    notified_file: Optional[Path] = None,
    amended_file: Optional[Path] = None,
) -> dict:
    """Classify how the feed changed vs the previous run into three buckets:
    genuinely NEW filings, AMENDMENTS (a known filing whose details were
    revised), and genuine REMOVALS (a filing withdrawn entirely).

    'new' is measured against a *cumulative* ledger of every notice key we have
    ever alerted on (``notified_keys.json``) — deliberately NOT a single prior
    run. The EDD spreadsheet intermittently serves two different versions across
    consecutive fetches (CDN/cache churn): the live record count repeatedly
    jumps (e.g. 1263 → 1342) and reverts on the very next run. A naive
    run-over-run diff therefore re-reports the same notices as "new" every time
    the feed swings up — which is exactly what produced duplicate email alerts
    on consecutive days. Keying off a cumulative ledger guarantees each unique
    notice can trigger at most one alert, regardless of how the feed churns.

    'amendments' get the same oscillation-proof treatment. When EDD revises a
    filing (most commonly its effective date), the revised line has a brand-new
    ``_notice_key`` while its *anchor* (company + county + city + notice_date) is
    unchanged. The naive run-over-run diff reports the old line as "removed" and
    the new line as "new" on every feed swing — re-surfacing the same single
    amendment indefinitely. Here, an anchor present in both the previous run and
    the current feed with a changed notice_key is recognised as an amendment and
    reported at most once, via ``amended_keys.json``. A guard against the revised
    key already living in either ledger suppresses the feed's reversions.

    'removed' is now restricted to filings whose *whole anchor* vanished from the
    feed (a real withdrawal), never a mere revision. It is informational and does
    not raise an alert on its own.
    """
    latest_file = latest_file if latest_file is not None else LATEST_FILE

    new_records = _df_to_records(new_df)
    feed_keys = {_notice_key(r) for r in new_records}

    # Previous run's published records (warn_latest.json, not yet rotated into
    # the snapshot when this runs).
    prev_records: list[dict] = []
    if latest_file.exists():
        try:
            prev_records = json.loads(latest_file.read_text()).get("records", [])
        except Exception:
            prev_records = []

    notified = _load_notified_keys(notified_file)

    if not notified:
        # No ledger yet (fresh clone / first deploy). Treat everything currently
        # known as already-seen so we don't alert for the entire backlog, and
        # seed the ledger. Nothing is "new" on this baseline run.
        baseline = feed_keys | {_notice_key(r) for r in prev_records}
        if not dry_run:
            _save_notified_keys(baseline, notified_file)
        log.info(
            f"No notified-keys ledger yet — established baseline of {len(baseline)} "
            "notice(s); suppressing alerts for this run."
        )
        return {
            "new_count": 0,
            "removed_count": 0,
            "amendment_count": 0,
            "new_keys": [],
            "amendment_keys": [],
            "new_entries": [],
            "removed_entries": [],
            "amendments": [],
            "amend_superseded": [],
            "total_employees_new": 0,
            "total_employees_removed": 0,
        }

    amended_ledger = _load_amended_keys(amended_file)

    # Anchor → records, for both sides, so a revised filing maps to its old self.
    prev_by_anchor: dict = defaultdict(list)
    for r in prev_records:
        prev_by_anchor[_anchor_key(r)].append(r)
    feed_by_anchor: dict = defaultdict(list)
    for r in new_records:
        feed_by_anchor[_anchor_key(r)].append(r)

    amendments: list[dict] = []
    amend_superseded: list[dict] = []  # old records to evict from the cumulative store
    amend_new_keys: set = set()        # canonical post-amendment notice keys
    for anchor, feed_recs in feed_by_anchor.items():
        prev_recs = prev_by_anchor.get(anchor)
        # Only an unambiguous 1:1 filing can be paired as an amendment; multi-site
        # notices that share an anchor fall through to the new/removed paths.
        if not prev_recs or len(feed_recs) != 1 or len(prev_recs) != 1:
            continue
        new_r, old_r = feed_recs[0], prev_recs[0]
        new_k, old_k = _notice_key(new_r), _notice_key(old_r)
        if new_k == old_k:
            continue  # filing unchanged

        # Report only a genuinely-unseen revision. If the revised key is already
        # in either ledger this is the feed oscillating back to a state we have
        # handled — surface nothing.
        report = new_k not in notified and new_k not in amended_ledger
        # The forward (canonical) version of an amendment is the one we report,
        # or one we have already recorded as amended. Only then do we evict the
        # superseded record — this keeps the cumulative store from flip-flopping
        # while the feed oscillates.
        canonical = report or new_k in amended_ledger
        if canonical:
            amend_superseded.append(old_r)
            amend_new_keys.add(new_k)
        if not report:
            continue
        amendments.append(
            {
                "company": new_r.get("company"),
                "county": new_r.get("county"),
                "city": new_r.get("city", ""),
                "notice_date": new_r.get("notice_date"),
                "effective_date": new_r.get("effective_date"),
                "employees": new_r.get("employees"),
                "old_effective_date": old_r.get("effective_date"),
                "new_effective_date": new_r.get("effective_date"),
                "old_employees": old_r.get("employees"),
                "new_employees": new_r.get("employees"),
                "key": new_k,
            }
        )

    added = [r for r in new_records if _notice_key(r) not in notified]
    # Genuinely new filings exclude amendment new-sides (reported as amendments).
    genuine_new = [r for r in added if _notice_key(r) not in amend_new_keys]
    # Genuine removals = the filing's whole anchor disappeared from the feed (a
    # real withdrawal), not merely a revised effective date or headcount.
    genuine_removed = [r for r in prev_records if _anchor_key(r) not in feed_by_anchor]

    # Record amendment new-keys in the notified ledger too, so a revised notice
    # never later alerts as a brand-new filing.
    new_keys = [_notice_key(r) for r in genuine_new] + sorted(amend_new_keys)

    return {
        "new_count": len(genuine_new),
        "removed_count": len(genuine_removed),
        "amendment_count": len(amendments),
        "new_keys": new_keys,
        "amendment_keys": [a["key"] for a in amendments],
        "new_entries": genuine_new[:50],
        "removed_entries": genuine_removed[:50],
        "amendments": amendments[:50],
        "amend_superseded": amend_superseded,
        "total_employees_new": sum(r.get("employees", 0) for r in genuine_new),
        "total_employees_removed": sum(r.get("employees", 0) for r in genuine_removed),
    }


def _log_change(diff: dict, dry_run: bool = False, changelog_file: Optional[Path] = None):
    """Append change event to the changelog."""
    changelog_file = changelog_file if changelog_file is not None else CHANGELOG_FILE
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        # amend_superseded is an internal threading field (full old records used
        # to evict from the cumulative store) — keep it out of the changelog.
        **{k: v for k, v in diff.items() if k != "amend_superseded"},
    }
    if diff["new_count"] > 0 or diff["removed_count"] > 0 or diff.get("amendment_count", 0) > 0:
        log.info(
            f"Changes: +{diff['new_count']} new, "
            f"~{diff.get('amendment_count', 0)} amended, "
            f"-{diff['removed_count']} removed records"
        )
    else:
        log.info("No data changes detected.")

    if not dry_run:
        with open(changelog_file, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def save_latest(
    df: pd.DataFrame,
    dry_run: bool = False,
    *,
    latest_file: Optional[Path] = None,
    snapshot_file: Optional[Path] = None,
    source_url: Optional[str] = None,
):
    """Save current data as latest + rotate snapshot."""
    latest_file = latest_file if latest_file is not None else LATEST_FILE
    snapshot_file = snapshot_file if snapshot_file is not None else SNAPSHOT_FILE
    records = _df_to_records(df)
    summary = {
        "total_records": len(records),
        "total_employees": int(df["employees"].sum()),
        "date_range_start": df["notice_date"].dropna().min() if "notice_date" in df.columns else df["effective_date"].dropna().min(),
        "date_range_end": df["notice_date"].dropna().max() if "notice_date" in df.columns else df["effective_date"].dropna().max(),
        "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        "source_url": source_url or WARN_XLSX_URL,
        "records": records,
    }
    if not dry_run:
        # Rotate: latest → snapshot
        if latest_file.exists():
            snapshot_file.write_text(latest_file.read_text())
        latest_file.write_text(json.dumps(summary, indent=2, default=str))
        log.info(f"Saved {len(records)} records to {latest_file}")
    else:
        log.info(f"[DRY-RUN] Would save {len(records)} records.")
    return summary


def _record_key(r: dict) -> tuple:
    """Stable identity for a single WARN notice line.

    EDD's published XLSX occasionally drops recently-added notices when it is
    re-exported, so identity must survive across files. County + notice_date
    keep multi-site notices for the same company distinct.
    """
    return (
        str(r.get("company", "")).strip().lower(),
        str(r.get("county", "")).strip().lower(),
        str(r.get("city", "")).strip().lower(),
        str(r.get("notice_date", ""))[:10],
        str(r.get("effective_date", ""))[:10],
        str(r.get("employees", "")),
    )


def _summarise(records: list[dict], source_url: Optional[str] = None) -> dict:
    """Build the standard summary envelope around a record list."""
    notices = [
        str(r.get("notice_date") or "")[:10] for r in records if r.get("notice_date")
    ]
    effs = [
        str(r.get("effective_date") or "")[:10]
        for r in records
        if r.get("effective_date")
    ]
    dates = notices or effs
    return {
        "total_records": len(records),
        "total_employees": int(sum(r.get("employees") or 0 for r in records)),
        "date_range_start": min(dates) if dates else None,
        "date_range_end": max(dates) if dates else None,
        "last_updated": datetime.now(timezone.utc).isoformat() + "Z",
        "source_url": source_url or WARN_XLSX_URL,
        "records": records,
    }


def update_cumulative(
    records: list[dict],
    dry_run: bool = False,
    superseded: list[dict] | None = None,
    *,
    cumulative_file: Optional[Path] = None,
    amended_file: Optional[Path] = None,
    source_url: Optional[str] = None,
) -> dict:
    """Merge the latest records into the cumulative store (union of all
    notices ever observed) and persist it.

    The official EDD file is the source of truth for *current* contents, but
    it is not append-only: a re-export can silently drop notices that were
    published days earlier. The cumulative store guarantees that once a notice
    has been seen it stays on the dashboard, so historical filings never vanish
    between runs. The latest version of a record wins on conflict.

    ``superseded`` are the pre-amendment versions of notices EDD has revised
    (from ``detect_changes``). Without eviction both the old and the revised
    line would linger in the union — e.g. a notice whose effective date moved
    would show up twice on the dashboard — so each superseded record is dropped.
    """
    cumulative_file = cumulative_file if cumulative_file is not None else CUMULATIVE_FILE

    existing: dict[tuple, dict] = {}
    if cumulative_file.exists():
        payload = json.loads(cumulative_file.read_text())
        for r in payload.get("records", []):
            existing[_record_key(r)] = r

    before = len(existing)
    for r in records:
        existing[_record_key(r)] = r
    evicted = 0
    # Evict the pre-amendment versions detected this run (handles the run where
    # an amendment is first seen, before it lands in the amended ledger).
    for r in superseded or []:
        if existing.pop(_record_key(r), None) is not None:
            evicted += 1
    # Then deterministically collapse any filing with a *recorded* amendment to
    # its canonical (amended) version, so the EDD feed's version oscillation can
    # never reintroduce a superseded line on a later union. Only anchors that
    # actually have a recorded amendment are touched, so genuine multi-site
    # filings sharing an anchor are left alone.
    amended = _load_amended_keys(amended_file)
    if amended:
        by_anchor: dict = defaultdict(list)
        for k, r in existing.items():
            by_anchor[_anchor_key(r)].append(k)
        for keys in by_anchor.values():
            if len(keys) < 2:
                continue
            canonical = [k for k in keys if _notice_key(existing[k]) in amended]
            if canonical:
                for k in keys:
                    if k not in canonical and existing.pop(k, None) is not None:
                        evicted += 1
    merged = list(existing.values())
    added = len(merged) - before

    summary = _summarise(merged, source_url=source_url)
    if not dry_run:
        cumulative_file.write_text(json.dumps(summary, indent=2, default=str))
        log.info(
            f"Cumulative store: {len(merged)} records "
            f"(+{added} new, -{evicted} superseded, {len(records)} in latest file)"
        )
    else:
        log.info(f"[DRY-RUN] Cumulative would hold {len(merged)} records.")
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
    diff = detect_changes(df, dry_run=dry_run)
    _log_change(diff, dry_run=dry_run)

    # 4. Persist
    summary = save_latest(df, dry_run=dry_run)

    # 5. Merge into the cumulative store so notices dropped by a later EDD
    #    re-export are never lost from the dashboard. Evict pre-amendment
    #    versions so a revised notice does not appear twice.
    cumulative = update_cumulative(
        summary["records"],
        dry_run=dry_run,
        superseded=diff.get("amend_superseded"),
    )

    result = {
        "file_changed": file_changed,
        "diff": diff,
        "summary": {k: v for k, v in summary.items() if k != "records"},
        "cumulative": {k: v for k, v in cumulative.items() if k != "records"},
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
