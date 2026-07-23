"""
warn_sources.pa
---------------
Pennsylvania — WARN notices published by the Department of Labor & Industry
as one large accordion-structured HTML page (year -> month -> notice), at
https://www.pa.gov/agencies/dli/programs-services/workforce-development-home/
warn-requirements/warn-notices. The live page carries roughly 2023-present.

Fetch is a plain conditional GET — the page serves ETag/Last-Modified, so the
shared ``warn_monitor.download_xlsx`` cache machinery applies unchanged.

Parse vendors the accordion-walking logic from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/pa.py): each leaf accordion item is one notice —
the title is the company, the body's first lines are the street address, and
the remaining "KEY: value" lines carry county, affected count, effective
date, and closure-or-layoff. Keyless continuation lines (multi-phase layoff
schedules and the like) fold into the previous key, exactly as BLN does.

Field mapping follows BLN's warn-transformer crosswalk
(warn_transformer/transformers/pa.py): title -> company, COUNTY/COUNTIES ->
county, EFFECTIVE DATE(S) -> effective_date, AFFECTED -> employees.
Pennsylvania publishes no notice date, so ``notice_date`` stays None — never
derived from the effective date. The page additionally publishes the site
address and a closure/layoff tag, kept as ``address`` and ``layoff_type``.

Free-text effective dates ("beginning 3/1/2026; ending 6/30/2026", phased
schedules, ranges) resolve to the FIRST date in the string — the same rule
BLN encodes by hand in ``date_corrections``. All 140 BLN date corrections
were replayed against this parser: 126 reproduce exactly via the first-date
rule; the 12 that cannot be derived (dates without years, month-only spans,
a year typo in the source) are vendored verbatim below; the remaining 2 are
demonstrable transcription typos in BLN's own dict ("5/26/25-5/30/25" ->
2025-01-31, "Beginning 9/29/23..." -> 2023-09-23) and are deliberately not
reproduced — the first-date rule yields the string's actual date instead.
BLN's ``jobs_corrections`` are vendored verbatim (PA-resident counts beat
nationwide totals; "Unknown"/"TBD" become no-count rows).
"""

import logging
import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_URL = (
    "https://www.pa.gov/agencies/dli/programs-services/"
    "workforce-development-home/warn-requirements/warn-notices"
)

# Garbage-character cleanup vendored from BLN warn-scraper warn/scrapers/pa.py.
_TEXT_FIXES = {
    "# AFFECTED": "AFFECTED",
    "\u2013": "--",  # en dash
    "\u200b": "",  # zero-width space (litters CLOSURE OR LAYOFF tags)
    "\u00a0": " ",  # no-break space
    "\u2039": " ",  # single left-pointing angle quote
    "\u2019": "'",  # right single quote
}

_WS = re.compile(r"\s+")
_DATE_TOKEN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_MONTH_DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}"
)

# Vendored from BLN warn-transformer transformers/pa.py date_corrections —
# only the entries the first-date rule cannot derive (keys are
# whitespace-normalized; values are the ISO date of BLN's datetime).
_DATE_CORRECTIONS = {
    "May 19, 23, 30 ... June 6, 20, 27 ... July 11, 18 ... August 8, 22, 29 "
    "... September 12": "2025-05-19",
    "first round -- 5/16 through 5/30; second round -- 6/23 through 7/7; "
    "final round -- sometime in 2026": "2025-05-16",
    "11/22 /2024": "2024-11-22",
    "6/7 /2024": "2024-06-07",
    "8/9 /2024": "2024-08-09",
    "8/16 /2024": "2024-08-16",
    "Beginning February/March 2024; Ending July 1, 2024": "2024-02-01",
    "04/14 -- 11 Employees ... 05/05 -- 20 Employees ... 06/17 -- 40 "
    "Employees ... 07/07 -- 20 Employees ... 08/04 -- 20 Employees ... "
    "09/08 -- 20 Employees ... 10/06 -- 20 Employees ... 11/03 -- 69 "
    "Employees ... 12/29 -- 40 Employees": "2023-04-14",
    "04/14": "2023-04-14",
    "Phase 1: 4/14 ... Phase 2: 5/13 -- 5/27 ... Phase 3: 6/12 -- 8/11":
        "2023-04-14",
    "Phase 1: 4/14": "2023-04-14",
    "7/3/20223 - 10/16/2023": "2023-07-03",  # year typo in the source page
}

# Vendored verbatim from BLN warn-transformer transformers/pa.py
# jobs_corrections (keys whitespace-normalized). None = state published no
# usable PA count for the row (becomes employees=0 in the unified schema).
_JOBS_CORRECTIONS = {
    "Unknown": None,
    "TBD": None,
    "unknown": None,
    "To be determined": None,
    "60 total": 60,
    "72 (54 PA residents impacted)": 54,
    "9 Pennsylvania workers (209 total) ... EFFECTIVE DATE: Beginning: "
    "7/15/25; Ending: 7/29/25": 9,
    "501 @ Etters location; 595 @ Philadelphia location": 1096,
    "14 Pennsylvania residents": 14,
    "430 nationwide; unknown number of PA residents impacted": None,
    "Cooked Plant -- 110 ... Raw Plant - 119": 229,
    "420 ... EFFECTIVE DATE: Beginning: 12/9/2024; Ending: 12/21/2024": 420,
    "124 ... EFFECTIVE DATE: Commencing: 5/30/2024; Ending: 7/29/2024": 124,
    "645 (**ONLY FIVE PA RESIDENTS AFFECTED**)": 5,
    "253 (173 @ Allentown and 80 @ Greensburg)": 253,
    "9 Pennsylvania workers (209 total)": 9,
    "105 (91 Temporary Layoffs and 14 Permanent Layoffs)": 105,
    "60 (all employees work remotely)": None,
    "206 (198 P/T and 8 F/T Employees)": 206,
    "54 (All employees can be relocated to other Amazon Delivery Service "
    "Partners)": 54,
    "179 (80 Marsden Employees and 99 Temporary Employees from both Express "
    "Labor & Integrated Staffing Agencies)": 179,
    "9236 Nationwide; PA total pending verification": None,
    "81 Total -- 13 of which reside in PA": 81,
    "5 (within PA)": 5,
}


def _extract_rows(html: str) -> list:
    """Notice dicts from the accordion page.

    Vendored from BLN warn-scraper warn/scrapers/pa.py: leaf accordion items
    (those not containing further items) are notices; the body splits on
    "COUNT" into address vs. detail lines; "KEY: value" lines set fields
    ("COUNTIES" folds into "COUNTY"); keyless lines and PHASE schedules
    append to the previous key with " ... " separators.
    """
    html = html.replace("\r\n", "\n").replace("\r", "\n")
    for bad, good in _TEXT_FIXES.items():
        html = html.replace(bad, good)
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for entry in soup.find_all("div", class_="cmp-accordion__item"):
        if "cmp-accordion__item" in str(entry.decode_contents()):
            continue  # year/month container accordion, not a notice
        title = entry.find("span", class_="cmp-accordion__title")
        body = entry.find("div", class_="text")
        if title is None or body is None:
            continue  # not a notice leaf
        line = {"Company": title.get_text().strip()}
        blob = body.get_text().strip()
        line["addressfull"] = blob.split("COUNT")[0].strip().replace("\n", ", ")
        deets = "COUNT" + "COUNT".join(blob.split("COUNT")[1:])
        deets = deets.split("\n")
        if deets == ["COUNT"]:
            deets = []  # notice with no detail lines (address only)
        lastkey = None
        for deet in deets:
            has_key = (
                ":" in deet
                and "PHASE" not in deet.upper()
                and not deet.upper().startswith("ENDING")
            )
            if has_key:
                key = deet.split(":")[0].strip()
                if key == "COUNTIES":
                    key = "COUNTY"
                line[key] = ": ".join(deet.split(":")[1:]).strip()
                lastkey = key
            elif lastkey is not None:  # continuation / PHASE line: fold in
                if line[lastkey]:
                    line[lastkey] += " ... "
                line[lastkey] += deet.strip()
        rows.append(line)
    return rows


def _clean_date(value):
    """First effective date in the string as ISO, or None.

    Order: vendored BLN corrections, a bare m/d/y(yyy) value, the first
    m/d/y token inside free text, then the first "Month D, YYYY" date.
    """
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    if not text:
        return None
    if text in _DATE_CORRECTIONS:
        return _DATE_CORRECTIONS[text]
    if _DATE_TOKEN.fullmatch(text):
        return warn_monitor._safe_date(text)
    match = _DATE_TOKEN.search(text) or _MONTH_DATE.search(text)
    if match:
        return warn_monitor._safe_date(match.group(0))
    return None


def _clean_jobs(value):
    """Affected-worker count as int, or None when the state published none."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    if not text:
        return None
    for candidate in (text, text.replace(",", "")):
        if candidate in _JOBS_CORRECTIONS:
            return _JOBS_CORRECTIONS[candidate]
    count = warn_monitor._safe_int(text)
    if count is None:
        # New annotated counts ("60 total"-style) keep the leading figure.
        match = re.match(r"\d[\d,]*", text)
        if match:
            count = warn_monitor._safe_int(match.group(0))
    return count


class PennsylvaniaDLI(Source):
    code = "pa"
    name = "Pennsylvania"
    agency = "Pennsylvania Department of Labor & Industry"
    source_url = _URL
    cadence = "daily"

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        return warn_monitor.download_xlsx(
            force=force,
            url=self.source_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    def parse(self, raw_path) -> pd.DataFrame:
        html = Path(raw_path).read_bytes().decode("utf-8", errors="replace")
        records = []
        for row in _extract_rows(html):
            company = _WS.sub(" ", row.get("Company", "")).strip()
            if not company:
                continue  # company is required
            employees = _clean_jobs(row.get("AFFECTED"))
            effective = row.get("EFFECTIVE DATE", row.get("EFFECTIVE DATES"))
            records.append(
                {
                    "company": company,
                    # PA publishes no notice date — never derived.
                    "effective_date": _clean_date(effective),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": (row.get("CLOSURE OR LAYOFF") or "").strip(),
                    "county": (row.get("COUNTY") or "").strip(),
                    "address": (row.get("addressfull") or "").strip(),
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "effective_date",
                "employees",
                "layoff_type",
                "county",
                "address",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
