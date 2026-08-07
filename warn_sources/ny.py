"""
warn_sources.ny
---------------
New York — NYS Department of Labor WARN dashboard (Tableau Public).

NY retired its per-year HTML/Excel listings on 2025-04-01 in favor of a
Tableau dashboard (https://dol.ny.gov/warn-dashboard). The underlying
Tableau Public workbook exposes a direct CSV export endpoint, which this
source downloads as a single file. The dashboard only carries the current
filing year — older filings live on per-year legacy pages
(https://dol.ny.gov/legacy-warn-notices), captured separately:
2016 - Mar 2025 by ``scripts/backfill/ny.py`` (merged into the NY
cumulative store), and the dashboard-era gap Apr - Dec 2025 by
``scripts/backfill/ny_2025_gap.py`` into ``history_file`` below.

Fetch URL and field crosswalk vendored from Big Local News' Apache-2.0
warn-scraper (warn/scrapers/ny.py) and warn-transformer
(warn_transformer/transformers/ny.py). Quirks honored from the BLN
transformer: company falls back from "Business Legal Name" to "Company";
the notice-date header has a trailing-space variant ("Date of WARN
Notice "); date cells may carry trailing time/junk (first-token split,
comma/semicolon strip); and a small ledger of known data-entry typos is
corrected explicitly rather than guessed at.

NY publishes: company, notice date, effective date, affected workers,
layoff-vs-closure + permanence, county, and site address. It does not
publish a separate city or industry field, so those stay empty — the
city embedded in the address is not reliably extractable ("2nd Floor
Flushing, NY" style lines) and is never synthesized.
"""

import re
from typing import Optional

import pandas as pd

import warn_monitor
from .base import DATA_DIR, Source

# Known bad date strings in NY's historical feed -> intended ISO dates.
# Vendored from Big Local News warn-transformer (Apache-2.0), transformers/ny.py.
_DATE_CORRECTIONS = {
    "929/2022": "2022-09-29",
    "3/6/3023": "2023-03-06",
    "2": "2021-02-12",
    "2/2/2024`": "2024-02-02",
    "7/29/24": "2024-07-29",
    "7/31/24": "2024-07-31",
    "8/2/24": "2024-08-02",
    "9/24/24": "2024-09-24",
    "2/12/24": "2024-12-12",  # note date shift, per BLN
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_date(val) -> Optional[str]:
    """BLN-style date cleaning -> strict ISO YYYY-MM-DD or None.

    Mirrors the BLN transformer: take the first whitespace token and strip
    stray commas/semicolons, apply the known-typo corrections, then parse.
    ``warn_monitor._safe_date`` echoes unparseable strings back, so the
    result is gated on ISO shape — a bad cell becomes None, never junk.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = str(val).strip()
    if not text:
        return None
    text = text.split()[0].replace(",", "").replace(";", "")
    if text in _DATE_CORRECTIONS:
        return _DATE_CORRECTIONS[text]
    iso = warn_monitor._safe_date(text)
    if iso is not None and _ISO_RE.match(iso):
        return iso
    return None


def _cell(row, *names) -> str:
    """First non-empty value among candidate columns, as a clean string."""
    for name in names:
        if name in row.index:
            val = row[name]
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            text = re.sub(r"\s+", " ", str(val)).strip()
            if text and text.lower() not in ("nan", "none"):
                return text
    return ""


class NewYorkDOL(Source):
    code = "ny"
    name = "New York"
    agency = "New York State Department of Labor"
    # CSV export of the NYS DOL Tableau Public workbook (via BLN warn-scraper);
    # human-facing dashboard: https://dol.ny.gov/warn-dashboard
    source_url = (
        "https://public.tableau.com/views/"
        "WorkerAdjustmentRetrainingNotificationWARN/WARN.csv"
        "?%3Adisplay_static_image=y&%3AbootstrapWhenNotified=true"
        "&%3Aembed=true&%3Alanguage=en-US"
        "&:embed=y&:showVizHome=n&:apiID=host0"
    )
    cadence = "daily"
    # Dashboard-era 2025 notices (Apr-Dec) rolled off the current-year CSV
    # export when 2026 began; scripts/backfill/ny_2025_gap.py recovered them
    # from the workbook's year-2025 export. Like CA, the file surfaces in the
    # national dataset only — the NY live pipeline stays exactly as it is.
    history_file = DATA_DIR / "historical" / "ny_history.json"

    def fetch(self, force: bool = False) -> tuple:
        # Single-file feed: the shared downloader handles conditional GETs
        # and hash-based change detection. (Tableau sends no ETag, so every
        # fetch downloads and the file hash decides ``changed``.)
        return warn_monitor.download_xlsx(
            force=force,
            url=self.source_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    def parse(self, raw_path) -> pd.DataFrame:
        df = pd.read_csv(raw_path, dtype=str)
        # Headers carry stray trailing spaces ("Date of WARN Notice ",
        # "Number of Affected Workers ") that Tableau exports verbatim; the
        # affected-workers column is even exported twice. Strip once and
        # match on the clean names ("... .1" pandas dupes stay distinct).
        df.columns = [str(c).strip() for c in df.columns]

        records = []
        for _, row in df.iterrows():
            company = _cell(row, "Business Legal Name", "Company")
            # Drop junk: blank companies and repeated header lines.
            if not company or company.lower() == "business legal name":
                continue
            layoff_type = " ".join(
                part
                for part in (
                    _cell(row, "Layoff or Closure?"),
                    _cell(row, "Permanent or Temporary Layoff?"),
                )
                if part
            )
            employees = warn_monitor._safe_int(
                _cell(row, "Number of Affected Workers")
            )
            records.append(
                {
                    "company": company,
                    "notice_date": _clean_date(
                        _cell(row, "Date of WARN Notice")
                    ),
                    "effective_date": _clean_date(
                        _cell(row, "Date Layoff/Closure Starts")
                    ),
                    "employees": employees if employees is not None else 0,
                    "layoff_type": layoff_type,
                    "county": _cell(row, "Impacted Site County"),
                    "address": _cell(row, "Impacted Site Address"),
                }
            )
        return pd.DataFrame(records)
