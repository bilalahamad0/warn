"""
warn_sources.ga
---------------
Georgia — WARN notices published by the Technical College System of
Georgia (TCSG) at https://www.tcsg.edu/warn-public-view/.

Georgia historically had two feeds. The Georgia Department of Labor
search list (https://www.dol.state.ga.us/public/es/warn/searchwarns/list)
returned 404 when probed on 2026-07-21 — that era's records survive in
Big Local News' public historical CSV (1988-2022), which this source
merges in for backfill. The live feed is TCSG's WordPress/GravityView
page: scraping logic vendored from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/ga.py):

    1. GET  the public-view page  -> DataTables ajax nonce (+ view/post id)
    2. POST wp-admin/admin-ajax.php (gv_datatables_data, length=-1)
            -> JSON index whose first column links each notice's
               /warn-public-view/entry/<id>/ detail page
    3. GET  each detail page       -> full field table per notice

Detail pages are fetched once per entry and the parsed fields cached in
``detail_cache.json`` (BLN caches the raw HTML; a parsed-field ledger
keeps the committed data tree small). Post-publication edits to an
already-cached entry are therefore not observed — same trade-off as BLN.
Entries that drop out of the TCSG index drop out of the feed, so the
shared engine still sees genuine withdrawals.

Field crosswalk follows BLN's warn-transformer
(warn_transformer/transformers/ga.py) exactly: Georgia publishes NO
notice date (none is ever synthesized); effective_date is "First Date of
Separation" (%m/%d/%Y); employees is "Total Number of Affected
Employees"; layoff_type is "Type of Layoff or Closure" (live feed only —
the historical CSV lacks it). One deviation from BLN's single "location"
column (which flattens the historical City into "First Location
Address"): this schema keeps city and address distinct, so historical
rows carry City -> city and live rows carry the TCSG street address ->
address. A city is never extracted from the address blob. The trailing
" County" on TCSG county values is stripped so live and historical rows
aggregate together ("Fulton", not "Fulton County").
"""

import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

_VIEW_URL = "https://www.tcsg.edu/warn-public-view/"
_AJAX_URL = "https://www.tcsg.edu/wp-admin/admin-ajax.php"
_ENTRY_RE = re.compile(
    r'href="(https://www\.tcsg\.edu/warn-public-view/entry/(\d+)/)"[^>]*>'
    r"([^<]*)</a>"
)
# Fallback ids observed live 2026-07-21 if the page config ever hides them.
_DEFAULT_VIEW_ID = 77460
_DEFAULT_POST_ID = 77462

# Static 1988-2022 backfill hosted by Big Local News (Apache-2.0 project);
# includes the defunct GDOL search-list era.
_HISTORICAL_URL = (
    "https://storage.googleapis.com/bln-data-public/warn-layoffs/"
    "ga_historical.csv"
)
# Historical CSV -> detail-page vocabulary, vendored from BLN warn-scraper
# (headermatcher), except City which stays a city here (see module docstring).
_HISTORICAL_MAP = {
    "ID": "GA WARN ID",
    "Company Name": "Company Name",
    "City": "City",
    "County": "County",
    "Est. Impact": "Total Number of Affected Employees",
    "Separation Date": "First Date of Separation",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Consolidated raw-CSV columns (City is historical-only; address live-only).
_COLUMNS = [
    "GA WARN ID",
    "Company Name",
    "First Date of Separation",
    "Total Number of Affected Employees",
    "Type of Layoff or Closure",
    "County",
    "City",
    "First Location Address",
]

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG_RE = re.compile(r"<[^>]+>")


def _squish(val) -> str:
    """Whitespace-normalized string ('' for None/NaN)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return re.sub(r"\s+", " ", str(val)).strip()


def _extract_ajax_config(page_html: str) -> tuple:
    """(nonce, view_id, post_id) from the GravityView DataTables config.

    The page carries several WordPress nonces; the DataTables one lives in
    the ajax ``data`` blob next to ``"action":"gv_datatables_data"``.
    """
    blob = re.search(
        r'"action":"gv_datatables_data".{0,400}?"nonce":"([^"]+)"',
        page_html,
        re.S,
    )
    if not blob:  # BLN's original fallback: first nonce on the page
        blob = re.search(r'"nonce":"([^"]+)"', page_html)
    if not blob:
        raise ValueError("TCSG page: DataTables nonce not found")
    nonce = blob.group(1)

    view = re.search(r'"view_id":"?(\d+)', page_html)
    post = re.search(r'"post_id":"?(\d+)', page_html)
    view_id = int(view.group(1)) if view else _DEFAULT_VIEW_ID
    post_id = int(post.group(1)) if post else _DEFAULT_POST_ID
    return nonce, view_id, post_id


def _datatables_payload(nonce: str, view_id: int, post_id: int) -> dict:
    """Flat DataTables POST body (columns vendored from BLN warn-scraper)."""
    payload = {
        "draw": 1,
        "order[0][column]": 0,
        "order[0][dir]": "asc",
        "start": 0,
        "length": -1,  # every record in one response
        "action": "gv_datatables_data",
        "view_id": view_id,
        "post_id": post_id,
        "nonce": nonce,
    }
    for i, name in enumerate(["gv_96", "gv_4", "gv_date_created", "gv_97"]):
        payload[f"columns[{i}][data]"] = i
        payload[f"columns[{i}][name]"] = name
    return payload


def _extract_entry_links(ajax_text: str) -> list:
    """[(entry_id, url, link_text)] from the ajax JSON, in response order.

    The index rows are structurally chaotic (lists, dicts, nested lists —
    see BLN's ga.py war story), so, as BLN does, anchors are pulled from
    the raw response text; the strict entry-URL pattern keeps out any
    other markup. De-duplicated by entry id, first occurrence wins.
    """
    text = ajax_text.replace("\\", "")
    entries, seen = [], set()
    for url, entry_id, link_text in _ENTRY_RE.findall(text):
        if entry_id not in seen:
            seen.add(entry_id)
            entries.append((entry_id, url, link_text.strip()))
    return entries


def _parse_detail_fields(html: str) -> dict:
    """Field table of one TCSG entry page -> {label: text}.

    Row-walk vendored from BLN warn-scraper ga.py: skip the header row,
    nested-table sideshows and label-less rows; drop Email / Submitter
    Information / Acknowledgement rows; address cells are rebuilt from
    markup (lines joined with ", ", trailing "Map It" link dropped).
    """
    soup = BeautifulSoup(html, features="html5lib")
    table = soup.find("table", {"class": "gv-table-view-content"})
    if not isinstance(table, Tag):
        raise ValueError("TCSG detail page: field table not found")
    fields = {}
    lastrowname = "Placeholder"
    for row in table.find_all("tr")[1:]:
        if (
            row.find_all("table")
            or not row.find_all("th")
            or not row.find_all("td")
        ):
            continue
        rowname = row.find("th").text
        if not rowname:
            rowname = lastrowname + "."
        lastrowname = rowname
        if (
            "Email" in rowname
            or "Submitter Information" in rowname
            or "Acknowledgement" in rowname
        ):
            continue
        rowcontent = row.find("td").text
        if "Location Address" in rowname or rowname == "Company Address":
            rowcontent = (
                str(row.find("td"))
                .split("<br/><a")[0]
                .replace("<td>", "")
                .replace("<br/>", ", ")
            )
            # Safety net beyond BLN: never let residual markup through.
            rowcontent = _TAG_RE.sub(" ", rowcontent)
        fields[rowname.strip()] = _squish(rowcontent)
    return fields


def _clean_date(val) -> Optional[str]:
    """'%m/%d/%Y' (BLN's GA date_format) -> ISO; anything unparseable -> None."""
    text = _squish(val)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    iso = warn_monitor._safe_date(text)
    if iso is not None and _ISO_RE.match(iso):
        return iso
    return None


def _clean_county(val) -> str:
    """Strip the TCSG-style ' County' suffix so both eras aggregate."""
    text = _squish(val)
    return re.sub(r"\s+county$", "", text, flags=re.I)


class GeorgiaTCSG(Source):
    code = "ga"
    name = "Georgia"
    agency = "Technical College System of Georgia, Office of Workforce Development"
    source_url = _VIEW_URL
    cadence = "daily"

    # -- fetch --------------------------------------------------------------

    def _request(self, session, method, url, **kwargs):
        """One polite request: 60s timeout, 3 attempts, backoff."""
        kwargs.setdefault("timeout", 60)
        last_error = None
        for attempt in range(1, 4):
            try:
                resp = session.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                log.warning(f"[GA] {method} {url} attempt {attempt}: {e}")
                time.sleep(2 * attempt)
        raise last_error

    def _fetch_index(self, session) -> list:
        """Live TCSG index -> [(entry_id, url, link_text)]."""
        resp = self._request(session, "GET", _VIEW_URL)
        nonce, view_id, post_id = _extract_ajax_config(resp.text)
        time.sleep(1)  # max 1 request/second/host
        resp = self._request(
            session,
            "POST",
            _AJAX_URL,
            data=_datatables_payload(nonce, view_id, post_id),
            headers={"Origin": "https://www.tcsg.edu", "Referer": _VIEW_URL},
        )
        entries = _extract_entry_links(resp.text)
        if not entries:
            raise ValueError("TCSG index returned no entry links")
        return entries

    def _load_detail_cache(self, cache_path: Path) -> dict:
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text())
            except Exception as e:
                log.warning(f"[GA] unreadable detail cache, refetching: {e}")
        return {}

    def _fetch_details(self, session, entries, cache_path: Path) -> dict:
        """Parsed fields for every entry, fetching only uncached pages."""
        cache = self._load_detail_cache(cache_path)
        missing = [e for e in entries if e[0] not in cache]
        if missing:
            log.info(f"[GA] fetching {len(missing)} new TCSG detail pages …")
        for entry_id, url, link_text in missing:
            time.sleep(1)  # max 1 request/second/host
            resp = self._request(session, "GET", url)
            fields = _parse_detail_fields(resp.text)
            if not fields.get("GA WARN ID"):
                fields["GA WARN ID"] = link_text  # index anchor text
            cache[entry_id] = {
                col: fields.get(col, "") for col in _COLUMNS if col != "City"
            }
            cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True))
        return cache

    def _historical_rows(self, session, cache_file: Path) -> list:
        """BLN historical CSV rows mapped onto the detail vocabulary."""
        if not cache_file.exists():
            try:
                time.sleep(1)
                resp = self._request(session, "GET", _HISTORICAL_URL)
                cache_file.write_bytes(resp.content)
            except Exception as e:  # backfill optional; live feed still runs
                log.warning(f"[GA] historical backfill unavailable: {e}")
                return []
        with open(cache_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [
                {out: _squish(row.get(src, "")) for src, out in
                 _HISTORICAL_MAP.items()}
                for row in reader
            ]

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()
        session = requests.Session()
        session.headers["User-Agent"] = _UA

        entries = self._fetch_index(session)
        cache = self._fetch_details(
            session, entries, self.paths.root / "detail_cache.json"
        )
        rows = [dict(cache[eid]) for eid, _, _ in entries]

        live_ids = {r.get("GA WARN ID", "") for r in rows} - {""}
        for hist in self._historical_rows(
            session, self.paths.root / "historical.csv"
        ):
            if hist.get("GA WARN ID", "") not in live_ids:
                rows.append(hist)

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS, restval="")
            writer.writeheader()
            writer.writerows(rows)

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": _VIEW_URL,
                "rows": len(rows),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        records = []
        for _, row in df.iterrows():
            company = _squish(row.get("Company Name", ""))
            if not company or company.lower() == "company name":
                continue  # company is required; drop junk/header rows
            employees = warn_monitor._safe_int(
                row.get("Total Number of Affected Employees", "")
            )
            records.append(
                {
                    "company": company,
                    # GA publishes no notice date -> no notice_date column.
                    "effective_date": _clean_date(
                        row.get("First Date of Separation")
                    ),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": _squish(
                        row.get("Type of Layoff or Closure", "")
                    ),
                    "county": _clean_county(row.get("County", "")),
                    "city": _squish(row.get("City", "")),
                    "address": _squish(row.get("First Location Address", "")),
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
                "city",
                "address",
            ],
        )
        # Keep absent dates as real None (pandas coerces them to NaN).
        return out.astype(object).where(pd.notna(out), None)
