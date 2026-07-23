"""
warn_sources.hi
---------------
Hawaii — WARN notices published by the Workforce Development Council as
per-year WordPress news pages linked from
``https://labor.hawaii.gov/wdc/real-time-warn-updates/`` (2019-present).

Scrape flow vendored from Big Local News' Apache-2.0 warn-scraper
(warn/scrapers/hi.py) — ported, never imported: the hub page's
``div#container_main`` links one subpage per year; on each subpage every
notice is a ``<p>`` holding a PDF link. Two page generations exist and
both are honored: 2021+ pages give each notice its own paragraph, while
2019/2020 group many notices in one paragraph separated by ``<br>`` —
so each PDF link's parent paragraph is split on ``<br/>``, each chunk
re-parsed as its own row, duplicates skipped (BLN's exact algorithm; the
only deviation is deduping parent paragraphs by identity first, which
yields the same rows without re-splitting a 250-link paragraph once per
link). Row text is "Month D, YYYY – Company" with the company inside the
``<a>``; BLN's fallback for the 2024-era variant (link text = the date,
company outside) is kept too.

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/hi.py) exactly:

    Company  -> company      (required; first line, stripped)
    Date     -> notice_date  (the page's listed date — scrape-time
                              "%B %d, %Y" parse, then the vendored
                              ``date_corrections`` for the known typo /
                              amendment rows, else None; never junk)
    location -> (never populated by the state; not emitted)
    jobs     -> employees    (never populated by the state; 0 per row)

Hawaii publishes only a dated PDF link per notice: no employee counts,
no effective date, no city/county on the listing (all sit inside the
PDFs), so ``employees`` is always 0 and no other unified column is
fabricated (EXPANSION_RESEARCH.md §5: HI lacks effective dates — never
synthesize one from the notice date).

Known feed quirks honored:
- "September 10. 2021" (period for comma) on the He-Man Landscaping row
  fails strptime; BLN's correction is vendored verbatim (BLN maps it to
  2021-09-21).
- 18 asterisked 2020/2021 annotation rows ("* Hawaiian Airlines Amended
  September 16, 2020", "** Correction to FOH Hospitality Inc.", …) carry
  their whole text inside the ``<a>``, so the scraper's Company comes
  out empty — BLN emits them company-less too. Our schema requires a
  company, so these amendment pointers are dropped (the original notices
  they annotate all remain as their own rows).
- One 2021 annotation links a .docx instead of a PDF ("*Anheuser-Busch
  Sales of Hawaii, Inc email"); BLN's ``a[href*=pdf]`` selector skips it
  and so does this port.
"""

import csv
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = "https://labor.hawaii.gov/wdc/real-time-warn-updates/"

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

# BLN warn-scraper hi.py CSV header (kept for raw-file provenance).
RAW_HEADERS = ["Company", "Date", "PDF url", "location", "jobs"]

# Vendored verbatim from BLN warn-transformer transformers/hi.py
# date_corrections (as (y, m, d) tuples; None = no usable date). Keys are
# the scraper's raw Date text for rows "%B %d, %Y" cannot parse — mostly
# the 2020/2021 amendment annotations, plus the He-Man Landscaping
# period-for-comma typo (BLN maps it to 2021-09-21) and the 2025 rows the
# state briefly mislabeled "February 7, 2024" (since fixed on the live
# page; kept in case the text regresses).
DATE_CORRECTIONS = {
    "* Hawaiian Airlines Amended September 16, 2020": (2020, 9, 16),
    "*Hyatt Regency Waikiki Update December 14, 2020": (2020, 12, 14),
    "*Grand Hyatt Kauai Resort & Spa Amendment #2 August 13, 2021": (
        2021, 8, 13,
    ),
    "*American Machinery Update October 21, 2020": (2020, 10, 21),
    "** Correction to FOH Hospitality Inc.": None,
    "*Hawaiian Airlines Amendment": None,
    "* Correction to Marriott Resort Hospitality Corporation": None,
    "* DFS Update October 30, 2020": (2020, 10, 30),
    "*Alohilani Resort Amendment": None,
    "*** Correction to HV Global Management Corporation": None,
    "* Princeville Resort updated March 24, 2020": (2020, 3, 24),
    "*Grand Hyatt Kauai Resort & Spa Amendment October 30, 2020": (
        2020, 10, 30,
    ),
    "* Waikoloa Beach Marriott Resort & Spa Amended June 9, 2020": (
        2020, 6, 9,
    ),
    "*JTB Hawaii, Inc. Supplement October 30, 2020": (2020, 10, 30),
    "*Hawaiian Airlines Second Amended October 14, 2020": (2020, 10, 14),
    "*Flying Food Group, LLC Amended 10/12/2021": (2021, 10, 12),
    "*Errata to Amended WARN": None,
    "September 10. 2021 He-Man Landscaping, LLC": (2021, 9, 21),
    "September 10. 2021": (2021, 9, 21),
    "February 7, 2024 –   Ginshari, Inc. – KuruKuru Sushi": (2025, 2, 7),
    "February 7, 2025 –   Ginshari, Inc. – KuruKuru Sushi": (2025, 2, 7),
    "February 7, 2024 –   Territorial Savings Bank": (2025, 2, 7),
    "February 7, 2025 –   Territorial Savings Bank": (2025, 2, 7),
}

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Sanity window for parsed years: outside it is a typo, not data.
MIN_YEAR = 1988

_MISSING = object()


def _max_year():
    return date.today().year + 3


def _squish(val) -> str:
    """Text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _year_page_urls(html) -> list:
    """Hub page -> per-year subpage URLs, in page order (newest first).

    BLN's hi.py takes every link in ``div#container_main``; hardened to
    the year-notice pages only ("<year>-warn-notices") so a stray nav
    link can never be crawled as data.
    """
    soup = BeautifulSoup(html, features="html5lib")
    section = soup.select("div#container_main")
    if not section:
        return []
    urls = []
    for atag in section[0].find_all("a"):
        href = (atag.get("href") or "").strip()
        slug = href.rstrip("/").split("/")[-1]
        if re.match(r"^\d{4}-warn-notices$", slug) and href not in urls:
            urls.append(href)
    return urls


def _extract_rows(html, pageyear: str) -> list:
    """One year page -> list of dicts keyed by RAW_HEADERS.

    Vendored from BLN warn-scraper hi.py: select ``p a[href*=pdf]``,
    split each parent paragraph on ``<br/>`` (the 2019/2020 grouped
    format), re-parse each chunk as its own row, skip duplicates; then
    per row take the date prefix ("Month D, YYYY", ISO-formatted when it
    parses) and the company from the link text, with BLN's fallback for
    the era where the link text was the date instead.
    """
    soup = BeautifulSoup(html, features="html5lib")
    selection = soup.select("p a[href*=pdf]")

    # Same output as BLN's per-link loop: each unique parent paragraph
    # is split once instead of once per contained link.
    seen_parents, parents = set(), []
    for child in selection:
        parent = child.parent
        if parent is not None and id(parent) not in seen_parents:
            seen_parents.add(id(parent))
            parents.append(parent)

    rows = []
    for parent in parents:
        for subitem in parent.prettify().split("<br/>"):
            if len(subitem.strip()) > 5 and ".pdf" in subitem:
                subitem = (
                    subitem.replace("\xa0", " ").replace("\n", "").strip()
                )
                row = BeautifulSoup(subitem, features="html5lib")
                if row not in rows:
                    rows.append(row)

    out = []
    for row in rows:
        graftext = row.get_text().strip()
        tempdate = graftext

        # Not an amendment-style row with a 3/17/2022-format date: most
        # dates are like "March 17, 2022" and prefix the company.
        if pageyear in tempdate and f"/{pageyear}" not in tempdate:
            tempdate = (
                graftext.strip().split(pageyear)[0].strip() + f" {pageyear}"
            )

        datetext = tempdate
        try:
            datetext = datetime.strptime(tempdate, "%B %d, %Y").strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            log.debug(f"[HI] date left intact: '{tempdate}'")

        atag = row.select("a")[0]
        company = atag.get_text().strip()
        # 2024-era variant: the link text is the date, company outside.
        if company == tempdate:
            company = (
                row.get_text()
                .strip()
                .replace(tempdate, "")
                .replace("–", "")
                .strip()
            )

        out.append(
            {
                "Company": company,
                "Date": datetext,
                "PDF url": atag.get("href"),
                "location": None,
                "jobs": None,
            }
        )
    return out


def _write_raw_csv(rows, path: Path) -> None:
    """Consolidated rows -> BLN-format CSV at ``path``."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def _clean_date(val):
    """HI Date cell -> strict ISO YYYY-MM-DD or None (never junk).

    ISO passthrough (scrape-time parse already succeeded), then BLN's
    ``date_corrections`` verbatim, then one more "%B %d, %Y" attempt,
    else None — with a year sanity window so digit-mangled years can
    never be emitted as data.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = str(val).strip()
    if not text:
        return None
    if ISO_RE.match(text):
        year = int(text[:4])
        return text if MIN_YEAR <= year <= _max_year() else None
    if text in DATE_CORRECTIONS:
        ymd = DATE_CORRECTIONS[text]
        return None if ymd is None else "%04d-%02d-%02d" % ymd
    try:
        parsed = datetime.strptime(text, "%B %d, %Y")
    except ValueError:
        return None
    if MIN_YEAR <= parsed.year <= _max_year():
        return parsed.strftime("%Y-%m-%d")
    return None


class HawaiiWDC(Source):
    code = "hi"
    name = "Hawaii"
    agency = "Hawaii Workforce Development Council"
    source_url = PAGE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, session, url: str, first: bool) -> str:
        """One page politely: 1 req/s, 60 s timeout, 3 attempts."""
        last_err = None
        for attempt in range(3):
            if attempt or not first:
                time.sleep(1 + 2 * attempt)  # politeness + backoff
            try:
                resp = session.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[HI] {url} request error: {e}")
                continue
            if resp.status_code != 200:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                log.warning(f"[HI] {url} -> HTTP {resp.status_code}")
                continue
            return resp.text
        raise RuntimeError(f"HI feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape the hub + every year page into one consolidated CSV.

        Any year failing aborts the whole fetch — a partial crawl must
        never be written, or the diff engine would report the missing
        years as phantom withdrawals.
        """
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)

        hub = self._get(session, PAGE_URL, first=True)
        urls = _year_page_urls(hub)
        if not urls:
            raise RuntimeError(
                "HI feed: no year subpages found in div#container_main — "
                "hub layout may have changed"
            )

        rows = []
        for url in reversed(urls):  # oldest first, like BLN
            pageyear = url.rstrip("/").split("/")[-1][:4]
            html = self._get(session, url, first=False)
            year_rows = _extract_rows(html, pageyear)
            log.info(f"[HI] {pageyear}: {len(year_rows)} rows")
            rows.extend(year_rows)

        # The live feed has carried 450+ rows since 2019; a collapse
        # below 100 means the page layout changed, not mass rescissions.
        if len(rows) < 100:
            raise RuntimeError(
                f"HI feed: only {len(rows)} rows across all years — "
                "page layout may have changed"
            )

        self.paths.ensure()
        _write_raw_csv(rows, self.paths.raw)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": self.source_url,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Consolidated CSV -> unified-schema rows (BLN crosswalk)."""
        with open(raw_path, newline="", encoding="utf-8") as fh:
            raw_rows = list(csv.DictReader(fh))

        records = []
        for row in raw_rows:
            # BLN transform_company: first line, stripped. Empty company
            # = an asterisked annotation row (see module docstring).
            company = (row.get("Company") or "").split("\n")[0].strip()
            if not company:
                continue
            # HI publishes no employee count on the listing; jobs is
            # carried for BLN-format fidelity but is always empty.
            employees = warn_monitor._safe_int(row.get("jobs"))
            records.append(
                {
                    "company": _squish(company),
                    "notice_date": _clean_date(row.get("Date")),
                    "employees": employees if employees is not None else 0,
                }
            )
        out = pd.DataFrame(
            records, columns=["company", "notice_date", "employees"]
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
