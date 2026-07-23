"""
warn_sources.va
---------------
Virginia — WARN notices published by Virginia Works (the rebranded
Virginia Employment Commission) at
https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/

The page server-renders the complete notice table and links a CSV export
whose filename embeds a per-render timestamp (``/warn_notices_<unix>.csv``),
so there is no stable file URL to conditional-GET. Fetch loads the page,
extracts the current CSV link, downloads the CSV one polite second later,
and detects change by content hash. The old vec.virginia.gov endpoints
(``/warn-notices``, ``warn_notices.csv``) are dead (404), and the JS
bot-wall Big Local News fought there through 2024-2025 (their scraper
needed stealth Selenium under Xvfb) is gone on the new site — plain
requests with a browser User-Agent work.

Field crosswalk vendored from Big Local News' Apache-2.0 warn-transformer
(warn_transformer/transformers/va.py, date_format %m/%d/%Y):

    Company            -> company         (required)
    Notice Date        -> notice_date
    Impact Date        -> effective_date
    Employees Affected -> employees       (0 when not published)
    Location           -> city            (see below)
    Notice Type        -> layoff_type     (verbatim: "Closure", "Layoff",
                                           "Permanent Reduction",
                                           "Realignment", and run-together
                                           multi-type values like
                                           "ClosureLayoff"; often blank)

BLN's ``date_corrections`` entry ("10/01/1973" -> None, a placeholder the
feed still carries on one Impact Date) is vendored below. Location is free
text "City ST" — usually a Virginia city plus a redundant trailing " VA"
token (which is stripped), sometimes multi-city ("Clintwood & Nora"), a
county, or "VA-Statewide"; occasional out-of-state suffixes ("Hoffman
Estates IL", "Washington, DC DC") are kept verbatim. CSV cells carry HTML
entities (``&amp;`` in five company names), which are unescaped. "Contact
Person" and "Collective Bargaining Unit" have no unified-schema field and
are dropped. Virginia publishes no county, street address, or industry.
Backfill: the CSV serves the full history (2010-present, ~1100 notices as
of 2026).
"""

import html
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_PAGE_URL = (
    "https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/"
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# The page links its CSV export with a render-time unix timestamp in the name.
_CSV_HREF = re.compile(r'href="([^"]*warn_notices_\d+\.csv)"')

# Vendored from BLN warn-transformer transformers/va.py date_corrections:
# the feed's lone placeholder date means "no real date published".
_DATE_CORRECTIONS = {"10/01/1973": None}

_WS = re.compile(r"\s+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_text(value) -> str:
    """Collapse a CSV cell to one clean line; the export carries HTML
    entities (&amp; ...) which are unescaped."""
    if value is None:
        return ""
    return _WS.sub(" ", html.unescape(str(value))).strip()


def _clean_date(value):
    """ISO date or None. BLN corrections first, then the feed's %m/%d/%Y."""
    text = _clean_text(value)
    if not text:
        return None
    if text in _DATE_CORRECTIONS:
        return _DATE_CORRECTIONS[text]
    try:
        return datetime.strptime(text, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        iso = warn_monitor._safe_date(text)
        if iso and _ISO_DATE.match(iso):
            return iso
        return None


def _clean_location(value) -> str:
    """Location minus the redundant trailing " VA" state token(s).

    "Charlottesville VA" -> "Charlottesville"; "VA-Statewide VA" ->
    "VA-Statewide"; the doubled form "Sandston, VA VA" -> "Sandston";
    a bare "VA" (one row) -> ""; out-of-state suffixes ("Hoffman
    Estates IL") stay verbatim.
    """
    text = _clean_text(value)
    while True:
        stripped = re.sub(r"[\s,]+VA\.?$", "", text)
        if stripped == text:
            break
        text = stripped
    return "" if text == "VA" else text


class VirginiaWorks(Source):
    code = "va"
    name = "Virginia"
    agency = "Virginia Works (Virginia Employment Commission)"
    source_url = _PAGE_URL
    cadence = "twice-daily"

    # -- fetch --------------------------------------------------------------

    def _request(self, session, url):
        """One polite GET: 60s timeout, 3 attempts, backoff."""
        last_error = None
        for attempt in range(1, 4):
            try:
                resp = session.get(url, timeout=60)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[VA] GET {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        page = self._request(session, _PAGE_URL)
        match = _CSV_HREF.search(page.text)
        if match is None:
            raise ValueError(
                "VA WARN page: no warn_notices_<ts>.csv link found "
                "(page layout changed?)"
            )
        csv_url = urljoin(_PAGE_URL, match.group(1))
        log.info(f"[VA] CSV export link: {csv_url}")

        time.sleep(1)  # max 1 request/second/host
        resp = self._request(session, csv_url)
        head = resp.content[:200].lstrip().lower()
        if not head.startswith(b"company"):
            raise ValueError(
                f"VA CSV export does not look like the notice CSV "
                f"(starts with {resp.content[:40]!r})"
            )
        self.paths.raw.write_bytes(resp.content)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": csv_url,
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _clean_text(row.get("Company", ""))
            if not company or company == "Company":
                continue  # company is required; drop stray header rows
            employees = warn_monitor._safe_int(row.get("Employees Affected"))
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(row.get("Notice Date")),
                    "effective_date": _clean_date(row.get("Impact Date")),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": _clean_text(row.get("Notice Type", "")),
                    "city": _clean_location(row.get("Location", "")),
                }
            )
        out = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "city",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
