"""
warn_sources.mi
---------------
Michigan — LEO Workforce Development WARN notices (Sitecore search JSON).

The human-facing page at
https://www.michigan.gov/leo/bureaus-agencies/wd/data-public-notices/warn-notices
is fed by a Sitecore ``sxa/search`` JSON endpoint whose ``p`` parameter is a
page size; requesting a huge page returns every notice currently listed in a
single response. Each result carries an HTML fragment with the company name
plus labelled "Key: value" lines. Two fragment formats coexist:

* new (post-2025-11-25): company in ``<h3>``, fields as ``<li>`` bullets,
  multi-site addresses in a nested ``<ul>``;
* older: company as the ``content-title-link`` anchor text, fields in a
  ``<p>`` separated by ``<br>``.

The feed is a rolling window (roughly the trailing ~20 months); the shared
engine's cumulative store retains older filings once seen.

Field crosswalk (per the BLN transformer, followed exactly):

* company        <- fragment heading (``h3`` / title-link anchor)
* effective_date <- ``date_start`` ("Layoff date(s)" / "Commencing date")
* employees      <- ``jobs`` ("Number of jobs impacted/affected")
* city           <- "City"/"Cities" (BLN ``location``); trailing
  ", Michigan"/", MI" suffix stripped
* layoff_type    <- "Type of company action" (state's own wording)
* county, address <- "County/Counties", "Site address(es)"

Michigan publishes **no notice date** (EXPANSION_RESEARCH.md §5) and no
industry — those fields are omitted, never synthesized. "Closure date" is a
distinct concept (BLN ``date_close``, unused by the transformer) and is
deliberately NOT copied into effective_date. "Reason for company action",
"Impacted job titles" and "Includes the following" stay in the raw download.

Fetch/parse logic and the label/date/jobs tables are ported from Big Local
News' Apache-2.0 warn-scraper (warn/scrapers/mi.py) and warn-transformer
(warn_transformer/transformers/mi.py) — vendored, not imported. Deliberate
divergences: unknown labels are logged and skipped (BLN raises KeyError);
missing dates/counts become None/0 (BLN backfills them with the notice PDF's
URL as a placeholder key for manual correction).
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://www.michigan.gov/leo/bureaus-agencies/wd/"
    "data-public-notices/warn-notices"
)
# The page's own search JSON; p= is a page size, oversized to get everything
# in one response (same trick as BLN warn-scraper warn/scrapers/mi.py).
API_URL = (
    "https://www.michigan.gov/leo/sxa/search/results/"
    "?s={8E97AB1D-D2D4-47F8-8CC4-3F1039C8854F}"
    "&itemid={BE81F7C2-36A8-4FDE-853C-B05B6E090055}"
    "&sig=&autoFireSearch=true"
    "&v={1FFFCC21-5151-4A2B-ABFC-F7FE4E5C9783}"
    "&p=54321&o=Created%20Date%20sort%2CDescending"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Label -> internal key crosswalk, vendored from BLN warn-scraper mi.py.
FIELD_MAP = {
    "Cities": "city",
    "City": "city",
    "Closure date": "date_close",
    "Commencing date": "date_start",
    "Counties": "county",
    "County": "county",
    "Includes the following": "includes",
    "Layoff (permanent) - City": "city",
    "Layoff (temporary) - City": "city",
    "Layoff - Cities": "city",
    "Layoff - City": "city",
    "Layoff date": "date_start",
    "Layoff dates": "date_start",
    "Layoff- City": "city",
    "Layoffs - Cities": "city",
    "Mass Layoff/Plant Closure - City": "city",
    "Number of jobs affected": "jobs",
    "Number of jobs impacted": "jobs",
    "Reduction in Hours - Cities": "city",
    "Total number of jobs impacted": "jobs",
    "Type of company action": "action",
    "Site address": "address",
    "Site addresses": "address",
    "Reason for company action": "reason",
    "Impacted job titles": "titles",
}

# Unicode cleanup, vendored from BLN warn-scraper mi.py.
TEXTFIXES = {
    "–": "--",
    "—": "--",
    "​": "",
    " ": " ",
    "‹": " ",
    "’": "'",
    "é": "e",
}

# Candidate date formats per the BLN transformer, plus "%B %Y" so month-only
# values ("June 2025") resolve generically to the 1st of the month.
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d, %y", "%B %Y"]

# Manual corrections for malformed source dates, vendored from BLN
# warn-transformer transformers/mi.py (string keys only: BLN's URL keys
# correct its own PDF-link placeholder hack, which we do not reproduce).
DATE_CORRECTIONS = {
    "June 17, 2024 - July 31, 2024": "2024-06-17",
    "Commencing June 2025": "2025-06-01",
    "Beginning April 12, 2025": "2025-04-12",
    "June 30, 2024 (approximate)": "2024-06-30",
    "Beginning February 2, 2025": "2025-02-02",
    "Beginning April 21, 2025": "2025-04-21",
    "Beginning February 6, 2025": "2025-02-06",
    "Beginning July 7, 2025": "2025-07-07",
    "Beginning January 31, 2025": "2025-01-31",
    "April 31, 2019": None,  # not a real date
}

# Vendored from BLN warn-transformer transformers/mi.py jobs corrections
# (the second key is the same value as seen live, comma included).
JOBS_CORRECTIONS = {
    "138 (133 Zeeland 5 Traverse City)": 138,
    "138 (133 Zeeland, 5 Traverse City)": 138,
    "163 204 130 191": 688,  # multi-site total
}

# BLN sanity guards.
MAX_FUTURE_DAYS = 730  # MI layoff dates run well over a year ahead
MINIMUM_YEAR = 1988
MAXIMUM_JOBS = 10000

_JUNK_COMPANIES = {"company", "n/a", "none", "tbd"}
_QUALIFIER_RE = re.compile(
    r"^(beginning|commencing|approximately|approx\.?|estimated|on or about|on)\s+",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\([^)]*\)")
_CITY_SUFFIX_RE = re.compile(r",?\s*(michigan|mi)\.?\s*$", re.IGNORECASE)
_INT_RE = re.compile(r"\d[\d,]*")

COLUMNS = [
    "company",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
]


def _clean_text(s: str) -> str:
    for bad, good in TEXTFIXES.items():
        s = s.replace(bad, good)
    return " ".join(s.split())


def _transform_date(value) -> Optional[str]:
    """MI date text -> ISO ``YYYY-MM-DD`` or ``None`` (BLN mi.py logic)."""
    if value is None:
        return None
    raw = _clean_text(str(value))
    if not raw:
        return None
    if raw in DATE_CORRECTIONS:
        return DATE_CORRECTIONS[raw]
    v = _PAREN_RE.sub(" ", raw).strip()
    v = _QUALIFIER_RE.sub("", v).strip()
    # Ranges / lists resolve to their start date: "2/23/2026 -- 5/31/2026",
    # "June 17, 2024 - July 31, 2024", "12/5/25, 1/16/26, and ...",
    # "5/9/26-6/19/26" (numeric dates never contain a hyphen themselves).
    for sep in ("--", " through ", " to "):
        v = v.split(sep)[0].strip()
    if "/" in v:
        v = v.split("-")[0].split(",")[0].strip()
    else:
        v = v.split(" - ")[0].strip()
    dt = None
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            break
        except ValueError:
            continue
    if dt is not None:
        plausible = (
            dt.year >= MINIMUM_YEAR
            and dt <= datetime.today() + timedelta(days=MAX_FUTURE_DAYS)
        )
        if plausible:
            return dt.strftime("%Y-%m-%d")
    log.debug(f"[MI] unparseable date {raw!r} — recording as None")
    return None


def _transform_jobs(value) -> int:
    """MI worker-count text -> int; 0 when the state publishes no count."""
    if value is None:
        return 0
    raw = _clean_text(str(value))
    if raw in JOBS_CORRECTIONS:
        corrected = JOBS_CORRECTIONS[raw]
        return int(corrected) if corrected is not None else 0
    # "1 remote Michigan worker" -> 1; "1,140" -> 1140; breakdowns in
    # parentheses are ignored in favour of the leading total.
    m = _INT_RE.search(_PAREN_RE.sub(" ", raw))
    if not m:
        if raw:
            log.debug(f"[MI] unparseable worker count {raw!r} — recording 0")
        return 0
    n = int(m.group(0).replace(",", ""))
    if n < 0 or n > MAXIMUM_JOBS:
        log.debug(f"[MI] implausible worker count {raw!r} — recording 0")
        return 0
    return n


def _fragment_lines(guts) -> list:
    """Flatten a result fragment into "Label: value" text lines.

    Handles both fragment formats: ``<li>`` bullets (with multi-site
    addresses in a nested ``<ul>``, joined with "; ") and ``<p>`` blocks
    whose lines are separated by ``<br>``.
    """
    lines = []
    for li in guts.find_all("li"):
        if li.find_parent("li") is not None:
            continue  # nested address bullet — handled via its parent
        nested = li.find("ul")
        if nested is not None:
            vals = [
                _clean_text(x.get_text(" ", strip=True))
                for x in nested.find_all("li")
            ]
            nested.extract()
            lines.append(_clean_text(li.get_text()) + " " + "; ".join(vals))
        else:
            lines.append(_clean_text(li.get_text()))
    for p in guts.find_all("p"):
        seg = ""
        for node in p.descendants:
            if getattr(node, "name", None) == "br":
                lines.append(_clean_text(seg))
                seg = ""
            elif isinstance(node, str):
                seg += node
        lines.append(_clean_text(seg))
    return [ln for ln in lines if ln]


def _parse_fragment(html: str) -> Optional[dict]:
    """One search-result HTML fragment -> raw field dict (or None)."""
    soup = BeautifulSoup(html, "lxml")
    guts = soup.select_one("div.search-results__section-content")
    if guts is None:
        return None
    h3 = guts.find("h3")
    company = _clean_text(h3.get_text(" ", strip=True)) if h3 else ""
    if not company:
        anchor = guts.select_one("a.content-title-link")
        if anchor is not None:
            company = _clean_text(anchor.get_text(" ", strip=True))
    if not company or company.lower() in _JUNK_COMPANIES:
        return None
    fields = {"company": company}
    for line in _fragment_lines(guts):
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip()
        if label not in FIELD_MAP:
            if 0 < len(label) < 60:
                log.debug(f"[MI] unknown field label {label!r} — skipped")
            continue
        key = FIELD_MAP[label]
        if key not in fields:  # first occurrence wins, as in BLN
            fields[key] = value.strip()
    return fields


def _polite_get(session, url, attempts=3, timeout=90):
    """GET with 1 req/s politeness and up to ``attempts`` tries."""
    last_err = None
    for attempt in range(1, attempts + 1):
        time.sleep(1)  # max 1 request/second/host
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            log.warning(
                f"[MI] request failed (attempt {attempt}/{attempts}): {e}"
            )
            time.sleep(2 * attempt)
    raise RuntimeError(f"MI LEO fetch failed after {attempts} attempts: {last_err}")


class MichiganLEO(Source):
    code = "mi"
    name = "Michigan"
    agency = "Michigan Department of Labor and Economic Opportunity"
    source_url = PAGE_URL
    cadence = "twice-daily"

    def fetch(self, force: bool = False) -> tuple:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        resp = _polite_get(session, API_URL)
        payload = resp.json()
        results = payload.get("Results") or []
        if not results:
            raise RuntimeError("MI LEO search API returned no WARN records")

        self.paths.ensure()
        # Persist only the stable Results list — the envelope's Signature /
        # QueryTime / TotalTime churn on every call and would defeat the
        # change hash.
        self.paths.raw.write_text(
            json.dumps({"Results": results}, indent=2, sort_keys=True)
        )

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": pd.Timestamp.utcnow().isoformat(),
                "url": API_URL,
                "records_seen": len(results),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        data = json.loads(Path(raw_path).read_text())
        rows = []
        for entry in data.get("Results", []):
            fields = _parse_fragment(entry.get("Html") or "")
            if fields is None:
                continue
            city = fields.get("city", "")
            city = _CITY_SUFFIX_RE.sub("", city).strip()
            rows.append(
                {
                    "company": fields["company"],
                    "effective_date": _transform_date(fields.get("date_start")),
                    "employees": _transform_jobs(fields.get("jobs")),
                    "layoff_type": fields.get("action", "").strip(),
                    "county": fields.get("county", "").strip(),
                    "city": city,
                    "address": fields.get("address", "").strip(),
                }
            )
        df = pd.DataFrame(rows, columns=COLUMNS)
        # pandas turns None into NaN during construction; restore None so
        # dates are strictly "ISO string or None".
        df["effective_date"] = df["effective_date"].astype(object).replace(
            {float("nan"): None}
        )
        return df
