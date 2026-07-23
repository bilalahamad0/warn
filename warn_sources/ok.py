"""
warn_sources.ok
---------------
Oklahoma — WARN notices from the Employ Oklahoma participant portal
(https://www.employoklahoma.gov/Participants/s/warnnotices), a Salesforce
Experience Cloud site run by the Oklahoma Employment Security Commission.

Oklahoma's old JobLink app (okjobmatch.com/search/warn_lookups) is retired:
2026-07 probes find its TLS certificate expired 2025-09-02 and the path
404ing off a bare IIS box, with the domain root reduced to a redirect page.
The live feed is the Salesforce portal that Big Local News' current scraper
targets.

Fetch strategy vendored from BLN's Apache-2.0 warn-scraper
(warn/scrapers/ok.py): the page's aura ApexAction
``OESC_JS_getWARNLayoffNotices.getListofLayoffAccService`` returns every
notice as JSON in a single response. BLN hard-codes the aura context tokens
(their comment: "seem to work for at least a day"); this module instead
extracts the ``fwuid`` and loaded-app markup version live from the portal
HTML on every run, so the call survives Salesforce framework releases. The
response is the complete history (2001-present, ~218 rows), so backfill
exceeds the 2019 target for free.

Field crosswalk vendored exactly from BLN's Apache-2.0 warn-transformer
(warn_transformer/transformers/ok.py): company <- OESC_Employer_Name__c,
notice_date <- Launchpad__Notice_Date__c (their date_format "%Y-%m-%d"),
location <- ``row["city"] or row["workforce_board"]`` — carried here as
city, falling back to Select_Local_Workforce_Board__c (e.g.
"CentralRegion") when the city is blank, per their lambda — and
layoff_type <- Launchpad__Layoff_Closure_Type__c, the field their
check_if_closure reads ("Plant Closing" / "Mass Layoff" / "Other").
Oklahoma publishes NO employee counts (BLN maps jobs to a key deliberately
absent from the feed, so their jobs column is always null) — employees is
0 on every row — and no effective date, county, street address, or
industry, so those columns are never emitted, let alone synthesized. ZIP
and RecordTypeId are dropped (not unified-schema fields).
"""

import html as html_mod
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import warn_monitor
from .base import Source

log = logging.getLogger("warn_sources")

PORTAL_URL = "https://www.employoklahoma.gov/Participants/s/warnnotices"
AURA_URL = (
    "https://www.employoklahoma.gov/Participants/s/sfsites/aura"
    "?r=2&aura.ApexAction.execute=1"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# BLN warn-transformer ok.py: date_format = "%Y-%m-%d" (feed dates are ISO).
_DATE_FORMAT = "%Y-%m-%d"

_MIN_YEAR = 2000  # earliest genuine feed rows are 2001; older is a typo

_DELAY = 1.0  # politeness: max 1 request/second/host
_TRIES = 3

# Oklahoma publishes no employee counts, effective date, county, street
# address, or industry.
_OUTPUT_COLUMNS = [
    "company",
    "notice_date",
    "employees",
    "layoff_type",
    "city",
]


def _clean_str(val) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", html_mod.unescape(str(val))).strip()


def _clean_date(val):
    """Raw feed date text -> ISO YYYY-MM-DD string or None (never junk)."""
    s = _clean_str(val)
    if not s:
        return None
    try:
        iso = datetime.strptime(s, _DATE_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        iso = warn_monitor._safe_date(s)
    if not iso or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(iso)):
        return None
    if not _MIN_YEAR <= int(str(iso)[:4]) <= date.today().year + 10:
        return None
    return str(iso)


def _extract_aura_context(page_html: str) -> dict:
    """Build the aura.context dict from live portal HTML.

    BLN ships these values hard-coded; extracting them fresh each run is
    what keeps the Apex call working across Salesforce releases.
    """
    fwuid_match = re.search(r'"fwuid":"([^"]+)"', page_html)
    if not fwuid_match:
        raise RuntimeError(
            "OK: no fwuid in the portal page (Salesforce layout changed?)"
        )
    loaded_match = re.search(r'"loaded":\{([^{}]*)\}', page_html)
    loaded = (
        json.loads("{" + loaded_match.group(1) + "}") if loaded_match else {}
    )
    return {
        "mode": "PROD",
        "fwuid": fwuid_match.group(1),
        "app": "siteforce:communityApp",
        "loaded": loaded,
        "dn": [],
        "globals": {},
        "uad": True,
    }


def _aura_form_data(context: dict) -> dict:
    """POST body for the WARN-notice ApexAction (BLN scrapers/ok.py)."""
    action = {
        "id": "1;a",
        "descriptor": "aura://ApexActionController/ACTION$execute",
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": "",
            "classname": "OESC_JS_getWARNLayoffNotices",
            "method": "getListofLayoffAccService",
            "cacheable": False,
            "isContinuation": False,
        },
    }
    return {
        "message": json.dumps({"actions": [action]}),
        "aura.context": json.dumps(context),
        "aura.pageURI": "/Participants/s/warnnotices",
        "aura.token": "null",
    }


def _unwrap_records(payload: dict) -> list:
    """Aura response JSON -> list of notice dicts, or raise loudly."""
    actions = payload.get("actions") or []
    if not actions or actions[0].get("state") != "SUCCESS":
        raise RuntimeError(
            f"OK: aura action did not succeed: "
            f"{json.dumps(payload)[:500]}"
        )
    inner = actions[0].get("returnValue") or {}
    records = inner.get("returnValue") if isinstance(inner, dict) else inner
    if not isinstance(records, list) or not records:
        raise RuntimeError("OK: aura action returned no notice records")
    return records


class EmployOklahoma(Source):
    code = "ok"
    name = "Oklahoma"
    agency = "Oklahoma Employment Security Commission"
    source_url = PORTAL_URL
    cadence = "daily"

    def _request(self, method: str, url: str, tries: int = _TRIES, **kwargs):
        """Polite call: browser UA, 60s timeout, throttled, backed-off tries."""
        last_err = None
        for attempt in range(tries):
            time.sleep(_DELAY + 2 * attempt)
            try:
                resp = requests.request(
                    method,
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=60,
                    **kwargs,
                )
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_err = e
                log.warning(f"[OK] {method} {url} failed (try {attempt + 1}): {e}")
        raise last_err

    def fetch(self, force: bool = False) -> tuple:
        self.paths.ensure()

        page_html = self._request("GET", PORTAL_URL).text
        context = _extract_aura_context(page_html)
        resp = self._request("POST", AURA_URL, data=_aura_form_data(context))
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"OK: aura endpoint returned non-JSON: {resp.text[:300]}"
            ) from e
        records = _unwrap_records(payload)

        # Deterministic raw content so the change hash only moves on real
        # feed changes, not response ordering.
        records.sort(key=lambda r: str(r.get("Id", "")))
        self.paths.raw.write_text(json.dumps(records, indent=1))

        meta = warn_monitor._load_meta(self.paths.meta)
        new_hash = warn_monitor._file_hash(self.paths.raw)
        changed = force or new_hash != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": new_hash,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "url": PORTAL_URL,
                "row_count": len(records),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    def parse(self, raw_path) -> pd.DataFrame:
        rows = json.loads(Path(raw_path).read_text())
        out = []
        for r in rows:
            company = _clean_str(r.get("OESC_Employer_Name__c"))
            if not company:
                continue
            # BLN transformer quirk, vendored: location is the employer city,
            # falling back to the Local Workforce Board region when blank.
            city = _clean_str(r.get("OESC_Employer_City__c")) or _clean_str(
                r.get("Select_Local_Workforce_Board__c")
            )
            out.append(
                {
                    "company": company,
                    "notice_date": _clean_date(
                        r.get("Launchpad__Notice_Date__c")
                    ),
                    # Oklahoma publishes no employee counts (BLN's jobs
                    # column is always null for OK).
                    "employees": 0,
                    "layoff_type": _clean_str(
                        r.get("Launchpad__Layoff_Closure_Type__c")
                    ),
                    "city": city,
                }
            )
        df = pd.DataFrame(out, columns=_OUTPUT_COLUMNS)
        # Keep missing dates as real None (pandas would coerce to NaN).
        return df.astype(object).where(pd.notna(df), None)
