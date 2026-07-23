"""
warn_sources.nv
---------------
Nevada — Department of Employment, Training and Rehabilitation (DETR).

No Big Local News scraper exists for Nevada — this is a custom scraper
built from live probes of https://detr.nv.gov/Page/WARN (2026-07). DETR
publishes one master PDF per calendar year ("WARN and Non-WARN" since
2022's successor files; plain "WARN Notices" before that). The current
year's master is updated in place at a stable URL; prior years live at
frozen dated URLs (hardcoded below — the landing page itself sits behind
an Akamai bot wall that 403s non-browser clients, so link discovery is
not possible in CI, but the ``/content/media/`` PDF paths download fine).
Oddity worth recording: Akamai *passes* the pipeline's own
``WARNMonitor/2.0`` User-Agent yet 403s a bare Chrome UA string (a
Chrome UA without matching ``sec-ch-ua`` client hints looks spoofed), so
``fetch`` reuses ``warn_monitor.download_xlsx`` for the conditional
current-year download and a full browser-header set for the frozen
backfill files.

Two table generations:

* 2017-2020 — real table grids; ``pdfplumber.extract_table`` works.
  Columns: ``Received Date | [Notice Date (2020 only)] | Effective Date |
  Type | Affected Total | Employer | City | County``. The 2020-only
  "Notice Date" (letter date) maps to no unified field and is dropped;
  per EXPANSION_RESEARCH §5, ``notice_date`` is the received-by-state
  date, which every year publishes as "Received Date".
* 2022-present — gridless text. Column boundaries for Employer/City/
  County/Notification are derived per file from word-start x-position
  modes (the columns are left-aligned at constant x). Cells with no
  intervening whitespace jam into single words ("3/10/2026Layoff",
  "1102Levy", "Vegas/RenoClark/Washoe"); the left region is untangled
  with vocabulary regexes (dates, Layoff/Closure, counts) and the
  city/county/notification regions are re-split at the derived
  boundaries character-by-character.

2021 is published only as an image scan (no text layer, OCR out of
scope) — that year is a documented gap. The state's own quirks are kept
as published, never repaired: a "1/8/2025" received date on a 2026
notice, an "8/25/2028" effective date, clipped overflow cells
("Carson City/Elko/R"), "Humbolt" spellings, and rows with no dates at
all (Intuit 2026). "Unknown"/"Multiple"/month-only effective dates
normalize to None (never copied from the received date); "unknown"/"NR"
(the file legend says NR = Not Reported) counts become 0. The
WARN/Non-WARN label — published since 2023 — is appended to
``layoff_type`` ("Layoff (WARN)", "Closure (Non-WARN)"); earlier years
publish only the bare type (2020 also used "Reduced" for reduced-hours
events, kept verbatim). Nevada publishes no street address or industry.

``fetch`` archives every source PDF under ``data/states/nv/archive/``
(states silently replace files) and snapshots the mutable current-year
master to ``archive/<year>.pdf`` as well, so a January rollover of the
stable URL to the new year cannot make December's notices look like mass
withdrawals — the archived copy keeps contributing rows, mirroring the
SC/MS multi-file pattern. All extracted rows are consolidated into one
CSV at ``paths.raw``; ``parse`` normalizes that CSV to the unified
schema (dates via strict formats + corrections, counts via
``warn_monitor._safe_int``).
"""

import csv
import dataclasses
import logging
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
import requests

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

PAGE_URL = "https://detr.nv.gov/Page/WARN"

# Stable URL of the in-place-updated current-year master (the landing
# page's "<year> WARN Act Notices" link; trailing underscore is DETR's).
CURRENT_URL = (
    "https://detr.nv.gov/content/media/WARN_and_Non_WARN_Master_w_Logo_.pdf"
)

# Frozen prior-year master PDFs, transcribed from the landing page
# (2026-07 probe). 2021 exists only as an image-only scan
# (https://detr.nv.gov/Content/Media/WARN_2021.pdf) and is omitted.
FROZEN_URLS = {
    2017: "https://detr.nv.gov/Content/Media/2017.pdf",
    2018: "https://detr.nv.gov/Content/Media/2018.pdf",
    2019: "https://detr.nv.gov/Content/Media/2019.pdf",
    2020: "https://detr.nv.gov/Content/Media/2020.pdf",
    2022: (
        "https://detr.nv.gov/Content/Media/"
        "WARN_and_Non-WARN_Master_12.31.22.pdf"
    ),
    2023: (
        "https://detr.nv.gov/Content/Media/"
        "WARN_and_Non-WARN_Master_w_Logo_12082023.pdf"
    ),
    2024: (
        "https://detr.nv.gov/content/media/"
        "WARN_and_Non_WARN_Master_w_Logo_12_30.24.pdf"
    ),
    2025: (
        "https://detr.nv.gov/content/media/"
        "WARN_and_Non_WARN_Master_w_Logo_12_04.25.pdf"
    ),
}

# Full browser header set. A bare Chrome UA gets 403'd by DETR's Akamai;
# the matching client-hint headers are what let it through.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": (
        '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# Raw columns of the consolidated CSV written by fetch().
RAW_COLUMNS = [
    "received_date",
    "effective_date",
    "type",
    "affected",
    "employer",
    "city",
    "county",
    "notification",
    "source_year",
]

DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]

# Free-text effective-date cells as published. None = the state gave no
# usable date (never synthesized from the received date). "Jun-26" is the
# state's month-year shorthand for June 2026 (XS Nightclub, 2025 file).
DATE_CORRECTIONS = {
    "Unknown": None,
    "unknown": None,
    "Multiple": None,
    "Jun-26": datetime(2026, 6, 1),
}

_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
_TYPE_RE = re.compile(r"(Layoff|Closure|Reduced)")
_COUNT_STANDALONE_RE = re.compile(r"^([\d,]+\+?|unknown|NR)$", re.I)
_COUNT_JAM_RE = re.compile(r"^(\d[\d,]*\+?)([^\d\s,].*)$")
_TITLE_YEAR_RE = re.compile(r"(20\d{2})\s+WARN\s+Notices", re.I)

# (header substring, raw column) for the 2017-2020 grid tables, matched
# case-insensitively. "Notice Date" (2020 only) is deliberately unmapped.
_GRID_HEADER_MAP = [
    ("received", "received_date"),
    ("effective", "effective_date"),
    ("type", "type"),
    ("affected", "affected"),
    ("employer", "employer"),
    ("city", "city"),
    ("county", "county"),
    ("notificati", "notification"),
]


def _clean_text(text) -> str:
    """Collapse newlines/whitespace in a cell."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ---------------------------------------------------------------------------
# Gridless text extraction (2022-present files)
# ---------------------------------------------------------------------------


def _word_lines(page, tol: float = 2.5) -> list:
    """Cluster the page's words into visual lines by top coordinate."""
    lines: list = []
    words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
    for w in words:
        if lines and abs(lines[-1][-1]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(ln, key=lambda w: w["x0"]) for ln in lines]


def _is_data_line(line) -> bool:
    """Data rows carry a Layoff/Closure token; headers say 'Received'."""
    text = " ".join(w["text"] for w in line)
    if "Received" in text:
        return False
    return any(
        w["text"] == t or w["text"].endswith(t)
        for w in line
        for t in ("Layoff", "Closure", "Reduced")
    )


def _column_anchors(data_lines, has_notif: bool) -> tuple:
    """Derive (city_x0, county_x0, notif_x0) from word-start geometry.

    The City/County/Notification columns are left-aligned at a constant x
    per file, so their starts are the dominant word-x0 modes: the
    notification x0 comes from the WARN/Non-WARN vocabulary words, the
    county x0 from the last word left of it, and the city x0 is the
    frequent (>=70% of rows) word-start nearest below the county column —
    shifted left through near-adjacent frequent starts so multi-word
    cities ("Las Vegas") anchor on their first word.
    """
    notif_x0 = None
    if has_notif:
        c = Counter(
            round(w["x0"])
            for ln in data_lines
            for w in ln
            if w["text"] in ("WARN", "Non-WARN")
        )
        if c:
            notif_x0 = c.most_common(1)[0][0]
    c = Counter()
    for ln in data_lines:
        ws = [w for w in ln if notif_x0 is None or w["x0"] < notif_x0 - 2]
        if ws:
            c[round(ws[-1]["x0"])] += 1
    county_x0 = c.most_common(1)[0][0]
    n = len(data_lines)
    c = Counter(
        round(w["x0"])
        for ln in data_lines
        for w in ln
        if w["x0"] < county_x0 - 2
    )
    below = sorted(x for x, k in c.items() if k >= 0.7 * n)
    if not below:
        raise ValueError("NV PDF: could not derive city column anchor")
    city_x0 = below[-1]
    for x in reversed(below[:-1]):
        if city_x0 - x <= 20:
            city_x0 = x
        else:
            break
    return city_x0, county_x0, notif_x0


def _region_text(page, line, x_lo, x_hi) -> str:
    """Rebuild one line's text between x bounds from raw characters.

    Character-level so cells that visually touch across a column boundary
    ("Vegas/RenoClark/Washoe") split correctly at the derived anchor.
    """
    top = min(w["top"] for w in line) - 1.5
    bottom = max(w["bottom"] for w in line) + 1.5
    chars = sorted(
        (
            ch
            for ch in page.chars
            if top <= ch["top"] <= bottom
            and ch["x0"] >= x_lo - 2
            and (x_hi is None or ch["x0"] < x_hi - 2)
        ),
        key=lambda ch: ch["x0"],
    )
    out: list = []
    prev_x1 = None
    for ch in chars:
        if prev_x1 is not None and ch["x0"] - prev_x1 > 1.0:
            out.append(" ")
        out.append(ch["text"])
        prev_x1 = ch["x1"]
    return _clean_text("".join(out))


def _parse_left_region(text: str) -> Optional[tuple]:
    """Untangle 'received effective type count employer...' text.

    Handles the jammed cells ("2/3/2025Closure", "2/14/2025Unknown",
    "1102Levy") via vocabulary regexes. Returns (received, effective_raw,
    type, affected_raw, employer) or None if no Layoff/Closure token.
    """
    m = _TYPE_RE.search(text)
    if not m:
        return None
    pre, kind, post = text[: m.start()], m.group(1), text[m.end():]
    dates = _DATE_RE.findall(pre)
    received = dates[0] if dates else None
    if len(dates) > 1:
        effective = dates[1]
    else:
        effective = _clean_text(_DATE_RE.sub("", pre)) or None
    post = post.strip()
    affected, employer = None, post
    tokens = post.split(None, 1)
    if tokens:
        t0 = tokens[0]
        rest = tokens[1] if len(tokens) > 1 else ""
        if _COUNT_STANDALONE_RE.match(t0):
            affected, employer = t0, rest
        else:
            m2 = _COUNT_JAM_RE.match(t0)
            if m2:
                affected = m2.group(1)
                employer = _clean_text(m2.group(2) + " " + rest)
    return received, effective, kind, affected, employer


def _extract_textual_page(page) -> list:
    """One gridless page -> raw row dicts."""
    lines = _word_lines(page)
    data = [ln for ln in lines if _is_data_line(ln)]
    if not data:
        return []
    has_notif = any(
        w["text"].startswith("Notificati") for ln in lines for w in ln
    )
    city_x0, county_x0, notif_x0 = _column_anchors(data, has_notif)
    rows = []
    for ln in data:
        left = " ".join(w["text"] for w in ln if w["x0"] < city_x0 - 2)
        parsed = _parse_left_region(left)
        if parsed is None:
            continue
        received, effective, kind, affected, employer = parsed
        rows.append(
            {
                "received_date": received or "",
                "effective_date": effective or "",
                "type": kind,
                "affected": affected or "",
                "employer": _clean_text(employer),
                "city": _region_text(page, ln, city_x0, county_x0),
                "county": _region_text(page, ln, county_x0, notif_x0),
                "notification": (
                    _region_text(page, ln, notif_x0, None)
                    if notif_x0 is not None
                    else ""
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Grid extraction (2017-2020 files)
# ---------------------------------------------------------------------------


def _extract_grid(pdf) -> list:
    """Grid-table PDF -> raw row dicts (header only on page 1)."""
    rows: list = []
    col_idx = None
    for page in pdf.pages:
        for row in page.extract_table() or []:
            texts = [_clean_text(c) for c in row]
            if not any(texts):
                continue
            low = [t.lower() for t in texts]
            if any("received" in t for t in low):
                col_idx = {}
                for j, t in enumerate(low):
                    for needle, field in _GRID_HEADER_MAP:
                        if needle in t and field not in col_idx:
                            col_idx[field] = j
                            break
                continue
            if col_idx is None:
                continue
            rec = {
                f: texts[j] if j < len(texts) else ""
                for f, j in col_idx.items()
            }
            if rec.get("employer"):
                rows.append(rec)
            else:
                log.warning(f"[NV] dropping employerless grid row: {row!r}")
    return rows


def _extract_pdf(pdf_path) -> list:
    """One master PDF -> raw row dicts, auto-detecting the table style."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        if len(pdf.pages[0].extract_table() or []) > 1:
            rows = _extract_grid(pdf)
        else:
            rows = []
            for page in pdf.pages:
                rows.extend(_extract_textual_page(page))
    return rows


def _title_year(pdf_path) -> Optional[int]:
    """Year from the '<year> WARN Notices' page title, if present."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        m = _TITLE_YEAR_RE.search(pdf.pages[0].extract_text() or "")
    return int(m.group(1)) if m else None


class NevadaDETR(Source):
    code = "nv"
    name = "Nevada"
    agency = "Nevada Department of Employment, Training and Rehabilitation"
    source_url = PAGE_URL
    cadence = "as-filed"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.csv")

    # -- fetch --------------------------------------------------------------

    @staticmethod
    def _download(session, url: str, dest: Path) -> None:
        """Politely download one frozen PDF (retries, magic check)."""
        last_exc = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = session.get(url, timeout=90)
                if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                    dest.write_bytes(resp.content)
                    return
                last_exc = RuntimeError(
                    f"HTTP {resp.status_code} / not a PDF for {url}"
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning(f"[NV] attempt {attempt + 1} for {url}: {exc}")
        raise RuntimeError(f"NV feed: could not download {url}: {last_exc}")

    def fetch(self, force: bool = False) -> tuple:
        """Download current + frozen masters, consolidate to one CSV.

        Frozen years are fetched once into ``archive/`` (``--force``
        refreshes them); the current-year master is a conditional
        ETag/Last-Modified download every run, snapshotted to its
        content-year archive slot so a January URL rollover never drops
        December's rows. A partial download aborts the whole fetch.
        """
        self.paths.ensure()
        archive = self.paths.root / "archive"
        archive.mkdir(parents=True, exist_ok=True)

        changed = False
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        for year, url in sorted(FROZEN_URLS.items()):
            dest = archive / f"{year}.pdf"
            if force or not dest.exists():
                self._download(session, url, dest)
                changed = True
                time.sleep(1.2)  # politeness: ~1 request/second/host

        current = archive / "current.pdf"
        if not current.exists():
            force = True  # cached ETag but no local file: 304 would strand
        cur_changed, _ = warn_monitor.download_xlsx(
            force=force,
            url=CURRENT_URL,
            meta_file=self.paths.meta,
            local_path=current,
        )
        changed = changed or cur_changed
        cur_year = _title_year(current) or datetime.now(timezone.utc).year
        shutil.copyfile(current, archive / f"{cur_year}.pdf")

        rows = []
        for pdf_path in sorted(archive.glob("[0-9][0-9][0-9][0-9].pdf")):
            year = int(pdf_path.stem)
            source = current if year == cur_year else pdf_path
            year_rows = _extract_pdf(source)
            if not year_rows:
                raise RuntimeError(f"NV feed: no rows parsed from {source}")
            for rec in year_rows:
                rec["source_year"] = year
            rows.extend(year_rows)
            log.info(f"[NV] {year}: {len(year_rows)} rows")

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=RAW_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(val) -> Optional[str]:
        """Cell -> ISO YYYY-MM-DD or None, honoring the corrections."""
        text = _clean_text(val)
        if not text:
            return None
        if text in DATE_CORRECTIONS:
            fixed = DATE_CORRECTIONS[text]
            return fixed.strftime("%Y-%m-%d") if fixed else None
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        log.warning(f"[NV] unparseable date {text!r} -> None")
        return None

    @staticmethod
    def _clean_jobs(val) -> int:
        """Cell -> int count; 0 when the state published none.

        "unknown"/"NR" (Not Reported per the file legend) and blank cells
        are 0; a "20+" floor keeps the stated minimum.
        """
        text = _clean_text(val).rstrip("+")
        n = warn_monitor._safe_int(text)
        return n if n is not None else 0

    def parse(self, raw_path) -> pd.DataFrame:
        raw = pd.read_csv(
            raw_path, dtype=str, keep_default_na=False, encoding="utf-8"
        )
        records = []
        for row in raw.to_dict(orient="records"):
            company = _clean_text(row.get("employer"))
            if not company:
                continue
            kind = _clean_text(row.get("type"))
            label = _clean_text(row.get("notification"))
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("received_date")),
                    "effective_date": self._clean_date(
                        row.get("effective_date")
                    ),
                    "employees": self._clean_jobs(row.get("affected")),
                    "layoff_type": f"{kind} ({label})" if label else kind,
                    "county": _clean_text(row.get("county")),
                    "city": _clean_text(row.get("city")),
                }
            )
        log.info(f"[NV] parsed {len(records)} records")
        df = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "county",
                "city",
            ],
        )
        for col in ("notice_date", "effective_date"):
            df[col] = df[col].astype(object).where(df[col].notna(), None)
        return df
