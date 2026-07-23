"""
warn_sources.al
---------------
Alabama — Alabama Department of Workforce WARN notices.

The state serves its entire WARN list (2001-present, ~1,000 rows) as one
headerless CSV from ``/documents/warn-list/`` on workforce.alabama.gov. The
old madeinalabama.com address started failing in June 2026 and the new site
briefly blocked automation, but the state said the CSV download itself would
stay open — so fetch mimics a reader: visit the human-facing list page first
(session cookies), pause, then pull the CSV. Scrape flow, endpoint pair, and
the headerless column order are vendored from Big Local News' Apache-2.0
warn-scraper (scrapers/al.py); the field crosswalk plus the known-bad date
correction come from warn-transformer (transformers/al.py):
company="company", notice_date="date_notice", effective_date="date_action",
employees="affected", dates "%m/%d/%Y", and the feed's literal
"01/01/0001" placeholder corrected to 2020-01-01 (both such rows are 2020
filings). The single ``location`` value is a city name (or "Statewide"),
kept in ``city``; county is never guessed from it. ``action_type`` (current
"Closure"/"Layoff", historical "Closing *"/"Layoff *" — the asterisk is the
state's own footnote marker, rendered verbatim on their page) maps to
``layoff_type`` untouched. Alabama publishes no county, address, or
industry, so those unified fields are omitted (never fabricated). The two
leading/trailing id columns (notice id, row id) are dropped, as BLN does.
"""

import csv
import io
import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://workforce.alabama.gov/warn-list/"
CSV_URL = "https://workforce.alabama.gov/documents/warn-list/"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# The feed CSV ships with no header line; column order vendored from BLN
# warn-scraper scrapers/al.py. The consolidated raw file written by fetch()
# adds this header so parse() (and humans) can read it plainly.
RAW_COLUMNS = [
    "_id1",          # state notice id, e.g. AL202600003 / S-6-1742
    "action_type",
    "date_notice",
    "date_action",
    "company",
    "location",
    "affected",
    "_id2",          # internal row id
]

# Known-bad date strings that appear verbatim in the Alabama feed (vendored
# from BLN warn-transformer transformers/al.py date_corrections; datetime
# rendered as ISO).
DATE_CORRECTIONS = {
    "01/01/0001": "2020-01-01",
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# company values that are really repeated-header/junk rows, lowercased.
_JUNK_COMPANIES = {"company", "nan", "none"}


def _read_feed_csv(text: str) -> list:
    """Headerless feed CSV text -> list of row dicts keyed by RAW_COLUMNS.

    Blank/short lines are dropped; a header line (should the state ever add
    one, as BLN predicts) is filtered later by the junk-company guard.
    """
    clean = [line for line in text.splitlines() if line.strip(", ")]
    return list(
        csv.DictReader(io.StringIO("\n".join(clean)), fieldnames=RAW_COLUMNS)
    )


def _clean_date(value):
    """Raw Alabama date cell -> ISO YYYY-MM-DD string or None.

    The feed is uniformly %m/%d/%Y (BLN transformer date_format) apart from
    the corrected placeholder; anything unparseable becomes None — never raw
    text kept as a date.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    iso = warn_monitor._safe_date(raw)
    if iso and _ISO_RE.match(iso) and 1988 <= int(iso[:4]) <= 2100:
        return iso
    return None


class AlabamaWorkforce(Source):
    code = "al"
    name = "Alabama"
    agency = "Alabama Department of Workforce"
    source_url = PAGE_URL
    cadence = "monthly"

    # -- fetch ---------------------------------------------------------------

    def _request(self, session, url, tries=3):
        """Polite GET with retries/backoff; returns a Response or None."""
        resp = None
        for attempt in range(tries):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                log.warning(f"[AL] request error for {url}: {e}")
                continue
            if resp.status_code == 200:
                return resp
            log.warning(f"[AL] HTTP {resp.status_code} for {url}")
            if resp.status_code == 404:
                return resp  # hard miss — no point retrying
        return resp

    def fetch(self, force: bool = False) -> tuple:
        """Visit the list page (session), pull the CSV feed, write one
        consolidated headered CSV at ``self.paths.raw``."""
        self.paths.ensure()
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        # Vendored BLN flow: the human page first (cookies), then the CSV.
        resp = self._request(session, PAGE_URL)
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            log.warning(f"[AL] list page {status} — trying the CSV anyway")

        time.sleep(2)  # politeness: max 1 request/second/host
        resp = self._request(session, CSV_URL)
        if resp is None or resp.status_code != 200:
            status = "unreachable" if resp is None else f"HTTP {resp.status_code}"
            raise RuntimeError(f"AL feed: CSV endpoint {status} ({CSV_URL})")

        rows = _read_feed_csv(resp.text)
        if not rows:
            raise RuntimeError("AL feed: CSV parsed to zero rows")

        with open(self.paths.raw, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=RAW_COLUMNS, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
        return True, str(self.paths.raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV -> unified-schema rows (BLN field crosswalk)."""
        raw = pd.read_csv(Path(raw_path), dtype=str, keep_default_na=False)
        missing = {"company", "date_notice", "date_action"} - set(raw.columns)
        if missing:
            raise ValueError(f"AL feed: expected columns missing: {missing}")

        rows = []
        for _, r in raw.iterrows():
            company = str(r.get("company", "")).strip()
            if not company or company.lower() in _JUNK_COMPANIES:
                continue  # blank / repeated-header / junk row
            emp = warn_monitor._safe_int(r.get("affected", ""))
            rows.append(
                {
                    "company": company,
                    "notice_date": _clean_date(r.get("date_notice", "")),
                    "effective_date": _clean_date(r.get("date_action", "")),
                    "employees": emp if emp is not None else 0,
                    "layoff_type": str(r.get("action_type", "")).strip(),
                    "city": str(r.get("location", "")).strip(),
                }
            )

        out = pd.DataFrame(
            rows,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "city",
            ],
        )
        # pandas coerces None -> NaN on construction; restore real None so
        # missing dates serialize as JSON null, not the string "nan".
        return out.astype(object).where(pd.notna(out), None)
