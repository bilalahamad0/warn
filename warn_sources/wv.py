"""
warn_sources.wv
---------------
West Virginia — WARN notices published by WorkForce West Virginia at
https://workforcewv.org/job-seeker/layoffs-downsizing/warn-listing/

No BLN warn-scraper/transformer exists for WV — this is a custom scraper.
The state publishes two complementary artifacts on one WordPress page:

1. **Per-year link listing** — ``<details><summary>YYYY WARN Listings``
   blocks (2021-present), each holding ``<a href="...pdf">`` entries whose
   link text is the only structured data (the PDFs themselves are scanned
   employer letters, not parseable tables). From each link we extract:

       link text  -> company      (noise tokens "WARN"/"Notice"/"State"/
                                   "Update"/"Received"/"Supplemental",
                                   revision markers r1/r2, month names and
                                   the embedded date are stripped)
       M-D-YY in the link text -> notice_date  (None when absent — never
                                   synthesized from the section year)
       employees  -> 0             (the listing publishes no counts)

2. **Consolidated notices PDF** — "WV-WARN-Notices-1-1-22-to-1-3-25.pdf",
   a state-produced document of one key/value card per notice. Card
   crosswalk (state's own labels):

       Company             -> company        (required)
       Address             -> address        (multi-line, squished)
       County              -> county         (may list several counties)
       Date of Notice      -> notice_date
       Projected Date      -> effective_date (ranges like "7/12/23-7/26/23"
                              keep only the FIRST date; the state's own
                              occasionally-backwards dates are kept as
                              published, never "fixed")
       Closure/Mass Layoff -> layoff_type    (state wording kept verbatim:
                              Closure/Closing/Layoff/Mass Layoff/Idling/
                              Indefinite Idle/Terminations)
       Number Affected     -> employees      ("1 WV Resident" -> 1; the
                              multi-site Cygnus card's "Total" row -> 54;
                              per-site breakdown rows are ignored)
       Region / Contact Information -> dropped (not layoff data)

   Cards spanning a page break (Cygnus 9/23/24) are reassembled by
   carrying the current card across tables/pages; "Update"/"Postponement"
   banner rows are skipped.

De-duplication between the two artifacts: the card window is parsed from
the consolidated PDF's filename (1-1-22 .. 1-3-25). Listing sections for
years the window fully covers (2022-2024) are dropped wholesale, listing
links whose parsed date falls inside the window are dropped (the Cygnus
7-16-24 / Stonebrook 8-1-24 stragglers re-linked under "2025"), and the
undated "WARN VIMO INC" link — the card dated 12/20/24 — is dropped via
an explicit href exception. City is never populated: WV publishes only a
free-text address (often an out-of-state HQ), and per EXPANSION_RESEARCH
§5 fields are never fabricated from one another.

BLN-style guard rails adopted: browser UA, >=1 s between requests, 3
retries, ``maximum_jobs``-style 10 000 count cap, and minimum-row checks
so a broken/partial page can never reach the diff engine as a phantom
mass-withdrawal.

Backfill depth: cards 2022-01 .. 2025-01 (with counts), listing links
2021 + 2025-present (no counts) — the state's entire published history.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://workforcewv.org/job-seeker/layoffs-downsizing/warn-listing/"
)

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

# "6-4-26", "04_1_2026", "12/20/24", "6-4-2021" ... (never bare "12323").
DATE_RE = re.compile(r"(\d{1,2})[-_/.](\d{1,2})[-_/.](\d{2}|\d{4})(?!\d)")
YEAR_HEADING_RE = re.compile(r"^(\d{4})\s+WARN\s+Listings?$", re.IGNORECASE)
# The consolidated cards PDF, e.g. WV-WARN-Notices-1-1-22-to-1-3-25.pdf
CONSOLIDATED_RE = re.compile(r"WV-WARN-Notices", re.IGNORECASE)

# Tokens that are filing boilerplate, not company name, in link text.
NOISE_TOKENS = {
    "warn", "notice", "notices", "state", "update", "received",
    "supplemental", "download", "pdf", "listing", "listings",
}
MONTH_TOKENS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}
REVISION_RE = re.compile(r"^r\d+$", re.IGNORECASE)

# Link titles that defeat generic cleaning, keyed by href filename.
HREF_COMPANY_CORRECTIONS = {
    # Title is "WARN Notice State – West Virginia Conduent".
    "WARN-Notice-State-West-Virginia-Conduent.pdf": "Conduent",
}
# Undated listing links that duplicate a consolidated-PDF card.
DUPLICATE_HREFS = {
    "WARN-VIMO-INC.pdf",  # the Vimo, Inc. card dated 12/20/24
}

# Window the consolidated PDF covers, used if its filename stops parsing.
DEFAULT_CARD_WINDOW = ("2022-01-01", "2025-01-03")

MIN_LINKS = 30   # listing carries ~60 links since 2021
MIN_CARDS = 15   # consolidated PDF carries 24 cards
MAX_JOBS = 10000  # BLN warn-transformer maximum_jobs sanity cap
MIN_YEAR = 2000

PARSE_COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "address",
]


def _squish(val) -> str:
    """Cell/link text -> clean single-spaced string."""
    return re.sub(r"\s+", " ", str(val).replace("\xa0", " ")).strip()


def _squish_lines(val) -> str:
    """Like _squish but keeps line breaks (address street/city lines)."""
    lines = [_squish(line) for line in str(val).splitlines()]
    return "\n".join(line for line in lines if line)


def _valid_iso(month: int, day: int, year: int):
    """Component triple -> ISO date string, or None when implausible."""
    if len(str(year)) == 2:
        year += 2000
    if not (MIN_YEAR <= year <= date.today().year + 6):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _find_date(text):
    """First plausible M-D-Y in free text -> (iso, (start, end)) or (None, None)."""
    for m in DATE_RE.finditer(text or ""):
        iso = _valid_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso, m.span()
    return None, None


def _first_date(text):
    """Card date cell (may hold ranges/lists) -> ISO of the FIRST date."""
    iso, _ = _find_date(_squish(text or ""))
    return iso


def _clean_employees(text) -> int:
    """Count cell -> int; 0 when the state publishes no usable count."""
    m = re.search(r"\d[\d,]*", str(text or ""))
    if not m:
        return 0
    count = warn_monitor._safe_int(m.group(0))
    if count is None or not 0 <= count <= MAX_JOBS:
        return 0
    return count


def _link_company_and_date(title, href):
    """Listing link -> (company, notice_date ISO | None)."""
    title = _squish(title)
    date_iso, span = _find_date(title)
    filename = href.rstrip("/").rsplit("/", 1)[-1]
    if filename in HREF_COMPANY_CORRECTIONS:
        return HREF_COMPANY_CORRECTIONS[filename], date_iso
    if span:
        title = title[: span[0]] + " " + title[span[1]:]
    tokens = []
    for tok in title.replace("_", " ").split():
        bare = tok.strip(".,;:").lower()
        if bare in NOISE_TOKENS or bare in MONTH_TOKENS:
            continue
        if REVISION_RE.match(bare):
            continue
        if re.fullmatch(r"[-–—]+", tok):
            continue  # bare dash/en-dash separators
        tokens.append(tok)
    company = _squish(" ".join(tokens)).strip(" -–—_.,")
    return company, date_iso


def _parse_listing(html):
    """Page HTML -> (link rows, consolidated PDF URL | None).

    Each ``<details><summary>YYYY WARN Listings`` block yields
    {"year", "title", "href"} per PDF link; the consolidated notices PDF
    is pulled out separately (it is a data file, not a notice).
    """
    soup = BeautifulSoup(html, "html5lib")
    links, consolidated = [], None
    for details in soup.find_all("details"):
        summary = details.find("summary")
        if summary is None:
            continue
        m = YEAR_HEADING_RE.match(_squish(summary.get_text(" ")))
        if not m:
            continue
        year = int(m.group(1))
        for a in details.find_all("a", href=True):
            href = urljoin(PAGE_URL, a["href"])
            if not href.split("?")[0].lower().endswith(".pdf"):
                continue
            if CONSOLIDATED_RE.search(href):
                consolidated = href
                continue
            links.append(
                {"year": year, "title": _squish(a.get_text(" ")), "href": href}
            )
    if not links:
        raise ValueError("WV WARN page: no year listing sections found")
    return links, consolidated


def _card_window(href):
    """Consolidated filename -> (min ISO, max ISO) it covers.

    "WV-WARN-Notices-1-1-22-to-1-3-25.pdf" -> 2022-01-01 .. 2025-01-03;
    falls back to the known window when the pattern stops matching.
    """
    filename = str(href or "").rsplit("/", 1)[-1]
    found = []
    for m in DATE_RE.finditer(filename):
        iso = _valid_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            found.append(iso)
    if len(found) >= 2:
        return min(found), max(found)
    return DEFAULT_CARD_WINDOW


def _parse_cards(pdf_path):
    """Consolidated PDF -> list of raw card dicts (state's own labels).

    One key/value table per notice; a new card starts at a "Company" row.
    The current card carries across tables/pages so split cards (Cygnus
    9/23/24) reassemble. Continuation rows (key None) extend the Address;
    3-cell per-site count rows under "Number Affected" and banner rows
    ("Update", "Postponement of ...") are skipped. For "Number Affected"
    the LAST non-empty cell wins ("Total" | "54" -> "54").
    """
    cards, current, last_key = [], None, None

    def flush():
        nonlocal current
        if current and _squish(current.get("Company", "")):
            cards.append(current)
        current = None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    cells = [
                        None if c is None else _squish_lines(c) for c in row
                    ]
                    key = cells[0] if cells and cells[0] else None
                    values = [c for c in cells[1:] if c]
                    if key == "Company":
                        flush()
                        current = {"Company": values[0] if values else ""}
                        last_key = key
                    elif key is not None:
                        if current is None:
                            continue  # preamble outside any card
                        if key == "Number Affected":
                            current[key] = values[-1] if values else ""
                        else:
                            current[key] = values[0] if values else ""
                        last_key = key
                    else:
                        if (
                            last_key == "Number Affected"
                            and len(cells) >= 3
                        ):
                            continue  # per-site count breakdown row
                        if last_key == "Address" and current and values:
                            current["Address"] = (
                                current.get("Address", "") + "\n" + values[0]
                            )
                        # anything else: Update/Postponement banner -> skip
    flush()
    return cards


class WestVirginiaWFWV(Source):
    code = "wv"
    name = "West Virginia"
    agency = "WorkForce West Virginia"
    source_url = PAGE_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _get(self, url):
        """One URL politely: 60 s timeout, 3 attempts, backoff."""
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(1 + 2 * attempt)
            try:
                resp = session.get(url, timeout=60)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[WV] {url} attempt {attempt + 1}: {e}")
        raise RuntimeError(f"WV feed: fetch failed for {url} ({last_err})")

    def fetch(self, force: bool = False) -> tuple:
        """Scrape listing + consolidated PDF into one raw JSON file."""
        self.paths.ensure()
        links, consolidated_url = _parse_listing(self._get(PAGE_URL).text)
        if len(links) < MIN_LINKS:
            raise RuntimeError(
                f"WV feed: only {len(links)} listing links — the page "
                "layout may have changed"
            )
        if not consolidated_url:
            # Without the cards the 2022-2024 records would vanish and
            # surface as phantom withdrawals — refuse to continue.
            raise RuntimeError(
                "WV feed: consolidated notices PDF link not found"
            )

        time.sleep(1)  # max 1 request/second/host
        pdf_path = self.paths.root / "consolidated_notices.pdf"
        pdf_path.write_bytes(self._get(consolidated_url).content)
        cards = _parse_cards(pdf_path)
        if len(cards) < MIN_CARDS:
            raise RuntimeError(
                f"WV feed: only {len(cards)} cards parsed from the "
                "consolidated PDF — its layout may have changed"
            )
        log.info(f"[WV] scraped {len(links)} links, {len(cards)} cards")

        raw = {
            "source": PAGE_URL,
            "consolidated_url": consolidated_url,
            "card_window": list(_card_window(consolidated_url)),
            "listing": links,
            "cards": cards,
        }
        self.paths.raw.write_text(
            json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False)
        )

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": PAGE_URL,
                "links": len(links),
                "cards": len(cards),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        """Raw JSON -> unified-schema rows (cards first, then listing)."""
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        window = raw.get("card_window") or list(DEFAULT_CARD_WINDOW)
        wmin, wmax = window[0], window[1]
        # Listing years the card window covers end-to-end are dropped
        # wholesale (their notices live in the cards).
        covered_years = {
            y
            for y in range(int(wmin[:4]), int(wmax[:4]) + 1)
            if f"{y}-01-01" >= wmin and f"{y}-12-31" <= wmax
        }

        records = []
        for card in raw.get("cards", []):
            company = _squish(card.get("Company", ""))
            if not company:
                continue  # company is required
            address = _squish(
                re.sub(r"\s*\n\s*", ", ", str(card.get("Address", "")))
            )
            records.append(
                {
                    "company": company,
                    "notice_date": _first_date(card.get("Date of Notice")),
                    "effective_date": _first_date(
                        card.get("Projected Date")
                    ),
                    "employees": _clean_employees(
                        card.get("Number Affected")
                    ),
                    "layoff_type": _squish(
                        card.get("Closure/Mass Layoff", "")
                    ),
                    "county": _squish(card.get("County", "")),
                    "address": address,
                }
            )

        seen = set()
        for link in raw.get("listing", []):
            if link.get("year") in covered_years:
                continue  # the consolidated cards own this year
            filename = str(link.get("href", "")).rsplit("/", 1)[-1]
            if filename in DUPLICATE_HREFS:
                continue  # undated re-link of a card
            company, notice_date = _link_company_and_date(
                link.get("title", ""), link.get("href", "")
            )
            if not company:
                continue  # company is required
            if notice_date and wmin <= notice_date <= wmax:
                continue  # dated straggler already in the cards
            dedupe = (company.lower(), notice_date)
            if dedupe in seen:
                continue  # same notice re-linked under two years
            seen.add(dedupe)
            records.append(
                {
                    "company": company,
                    "notice_date": notice_date,
                    "effective_date": None,
                    "employees": 0,  # the listing publishes no counts
                    "layoff_type": "",
                    "county": "",
                    "address": "",
                }
            )

        out = pd.DataFrame(records, columns=PARSE_COLUMNS)
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
