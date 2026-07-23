"""
warn_sources.il
---------------
Illinois — Illinois WorkNet (IEBS) public layoff export API.

The Department of Commerce and Economic Opportunity publishes every layoff
event (WARN and non-WARN) through the Illinois WorkNet "IEBS" application,
whose public export endpoint returns a full-history XLSX workbook — one row
per affected *location* (IL reports per-site, so one company filing can span
several rows), reaching back to 1987. The endpoint is extremely slow (tens of
seconds to minutes is normal), so we fetch with a 180 s timeout and retries.

The server sends ``Cache-Control: no-cache`` and no ETag/Last-Modified, so
conditional requests are impossible; change detection is by content hash of
the downloaded workbook. The workbook embeds a generation timestamp, so the
hash (and hence ``file_changed``) can flip even when no record changed — the
shared engine's notified/amended ledgers make that harmless.

Field crosswalk (per the BLN transformer, followed exactly):

* company        <- ``Location Name``
* notice_date    <- ``Initial Date Reported``, falling back to the first
  entry of ``Notification(s) Received`` (renamed ``Notification Date(s)``
  in the current export, a newest-first comma-separated list)
* effective_date <- ``Impact Date`` (blank for most rows — IL rarely
  publishes one; recorded as None, never synthesized)
* employees      <- ``Revised Layoff`` (the state's running revised count)
* layoff_type    <- ``Reason`` ("Plant Closure" / "Mass Layoff" / "Layoff" —
  the column BLN's ``check_if_closure`` keys off)
* county/city/address/industry <- ``County`` / ``Location City`` /
  ``Location Address`` / ``Industry``

Only the first worksheet ("Layoffs") is parsed — the workbook's other sheets
("Scheduled Layoffs", "Industry Totals") are derived summaries — matching
BLN's ``parse_excel`` which reads only ``workbook.worksheets[0]``.

Fetch/parse logic and the sanity guards (minimum year 1987, 100 000 job cap)
are ported from Big Local News' Apache-2.0 warn-scraper (warn/scrapers/il.py)
and warn-transformer (warn_transformer/transformers/il.py) — vendored, not
imported. One deliberate divergence: where BLN raises ``KeyError`` on a value
it has no manual correction for, we log and degrade to ``None``/0 so one new
messy value can never break the pipeline.
"""

import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

EXPORT_URL = (
    "https://apps.illinoisworknet.com/iebs/api/public/export"
    "?search=&layoffTypes=&trade=0"
    "&dateReportedStart=Invalid%20Date&dateReportedEnd=Invalid%20Date"
    "&statuses=4&reasons=&eventCauses=&naicsCodes=1&naicIndustries="
    "&naics=&unionsInvolved=0&geolocation=1&cities=&counties=&lwias="
    "&includeAdditionalLwias=false&edrs=&lat=0&lng=0&distance=.5"
    "&memberType=1&users=&accessList=&bookmarked=false"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# The endpoint routinely takes tens of seconds to build the workbook.
TIMEOUT = 180
ATTEMPTS = 3  # 1 try + 2 retries

# BLN sanity guards (warn-transformer transformers/il.py): dates before the
# WARN Act era or far in the future, and implausible head-counts, are treated
# as unparseable rather than trusted.
MINIMUM_YEAR = 1987
MAX_FUTURE_DAYS = 365
MAXIMUM_JOBS = 100000

# The notice-date fallback column: renamed by the state at some point, so
# both names are recognised (BLN's crosswalk used the older one).
_NOTIF_COLUMNS = ("Notification(s) Received", "Notification Date(s)")

# Placeholder/header values that mark a non-record row.
_JUNK_COMPANIES = {"location name", "company", "n/a", "none", "tbd"}

COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
    "industry",
]


def _clean_str(val) -> str:
    """Cell value -> whitespace-collapsed string ('' for blank/NaN)."""
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return " ".join(str(val).split())


def _il_date(val):
    """IL date cell -> ISO ``YYYY-MM-DD`` or ``None``, BLN-guarded."""
    if not _clean_str(val):
        return None
    iso = warn_monitor._safe_date(val)
    if not iso:
        return None
    # _safe_date falls back to the raw string when unparseable; enforce ISO.
    try:
        dt = datetime.strptime(str(iso), "%Y-%m-%d")
    except ValueError:
        log.debug(f"[IL] unparseable date {val!r} — recording as None")
        return None
    if dt.year < MINIMUM_YEAR:
        return None
    if dt > datetime.today() + timedelta(days=MAX_FUTURE_DAYS):
        return None
    return iso


def _il_jobs(val) -> int:
    """IL worker-count cell -> int; 0 when absent or implausible."""
    n = warn_monitor._safe_int(val)
    if n is None or n < 0 or n > MAXIMUM_JOBS:
        return 0
    return n


class IllinoisWorkNet(Source):
    code = "il"
    name = "Illinois"
    agency = "Illinois Department of Commerce and Economic Opportunity"
    source_url = EXPORT_URL
    cadence = "twice-daily"

    def fetch(self, force: bool = False) -> tuple:
        # No conditional caching is possible (no ETag/Last-Modified, and the
        # response is generated per-request), so ``force`` changes nothing:
        # every fetch downloads and the content hash decides ``changed``.
        headers = {"User-Agent": USER_AGENT}
        last_err = None
        content = None
        for attempt in range(1, ATTEMPTS + 1):
            time.sleep(1)  # max 1 request/second/host
            try:
                log.info(
                    f"[IL] requesting export (attempt {attempt}/{ATTEMPTS}, "
                    f"timeout {TIMEOUT}s) …"
                )
                resp = requests.get(
                    EXPORT_URL, headers=headers, timeout=TIMEOUT
                )
                resp.raise_for_status()
                if not resp.content.startswith(b"PK"):
                    raise requests.RequestException(
                        "response is not an XLSX workbook "
                        f"(content-type {resp.headers.get('Content-Type')!r})"
                    )
                content = resp.content
                break
            except requests.RequestException as e:
                last_err = e
                log.warning(
                    f"[IL] fetch failed (attempt {attempt}/{ATTEMPTS}): {e}"
                )
                time.sleep(5 * attempt)
        if content is None:
            raise RuntimeError(
                f"IL export fetch failed after {ATTEMPTS} attempts: {last_err}"
            )

        self.paths.ensure()
        self.paths.raw.write_bytes(content)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": pd.Timestamp.utcnow().isoformat(),
                "url": EXPORT_URL,
                "bytes": len(content),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        # First worksheet only ("Layoffs") — the others are derived summaries.
        # (Ported from BLN warn-scraper utils.parse_excel.)
        df = pd.read_excel(raw_path, sheet_name=0)
        df.columns = [str(c).strip() for c in df.columns]
        notif_col = next(
            (c for c in _NOTIF_COLUMNS if c in df.columns), None
        )

        rows = []
        for r in df.to_dict("records"):
            company = _clean_str(r.get("Location Name"))
            if not company or company.lower() in _JUNK_COMPANIES:
                continue
            notice = _il_date(r.get("Initial Date Reported"))
            if notice is None and notif_col:
                # Newest-first comma-separated list; take the first entry.
                first = _clean_str(r.get(notif_col)).split(",")[0].strip()
                notice = _il_date(first)
            rows.append(
                {
                    "company": company,
                    "notice_date": notice,
                    "effective_date": _il_date(r.get("Impact Date")),
                    "employees": _il_jobs(r.get("Revised Layoff")),
                    "layoff_type": _clean_str(r.get("Reason")),
                    "county": _clean_str(r.get("County")),
                    "city": _clean_str(r.get("Location City")),
                    "address": _clean_str(r.get("Location Address")),
                    "industry": _clean_str(r.get("Industry")),
                }
            )
        out = pd.DataFrame(rows, columns=COLUMNS)
        # Keep missing dates as real None (not NaN) through pandas' string
        # dtype inference, matching the other sources' convention.
        return out.astype(object).where(pd.notna(out), None)
