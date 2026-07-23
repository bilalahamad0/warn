"""
warn_sources.mn
---------------
Minnesota — Department of Employment and Economic Development (DEED).

No Big Local News scraper exists for Minnesota (gap state) — this is a
custom scraper built from live probes of mn.gov (2026-07). DEED publishes
one "Plant Closings/Mass Layoffs/WARN" PDF per month (plus annual roll-ups)
under https://mn.gov/deed/assets/plant-closing-*.pdf.

Access: every HTML page on mn.gov sits behind a ShieldSquare/Radware bot
wall (302 → validate.perfdrive.com) even with full browser headers, so the
report *listing* page cannot be scraped. The PDF assets themselves are NOT
walled and download fine with a browser UA. Discovery therefore goes
through the Internet Archive's CDX index (IA's crawler does get through
the wall): a wildcard query for ``mn.gov/deed/assets/plant-closing*``
returns every monthly/annual report ever archived — 2021→present —
including their unpredictable Tridion ``_tcmNNNN-NNNNNN`` suffixes, which
cannot be guessed. Each PDF is then fetched from live mn.gov (Wayback raw
copy as fallback) and cached under ``data/states/mn/archive/``; the newest
two months are re-downloaded every run to catch in-place revisions.
Freshness caveat: a brand-new month becomes discoverable only once IA
crawls it (historically days–weeks after posting, on top of DEED's own
~1 month reporting lag) — hence ``cadence = "monthly"``.

Report semantics: the report lists ALL closings/mass layoffs the State
Rapid Response Team started working that month — not only WARN Act
filings. The "WARN Act" column (TRUE/FALSE or YES/NO) flags true WARN
notices and "WARN Received" (→ ``notice_date``) is only populated for
those; "Layoff Start" (→ ``effective_date``) is DEED's own best-guess
start date and is never copied into ``notice_date`` (or vice versa).
"TBD"/"-" dates and counts stay None/0 as published. Minnesota publishes
no county or street address; ``layoff_type`` keeps DEED's wording
("Closing", "Workforce Reduction", "Temporary Layoff", …).

Two table generations, auto-detected per file:

* ~Oct 2025-present — real bordered grids; ``pdfplumber.extract_tables``
  works (column map carried across the page's sub-tables and pages).
* 2021-2025 — gridless text whose default lattice extraction fragments
  into 20+ pseudo-columns. Columns are rebuilt from word geometry: the
  header keywords ("Layoff Name", "City", …, "Affected Workers") anchor
  each column's x-extent (unioned with stacked/adjacent prefix words such
  as "**Layoff" over "Start"), and data words are assigned by x-position.
  Wrapped cells merge into the previous row; "RR Start Date: <Month>
  <Year> (N records)" group headers are tracked (they date the rows of
  annual files) and "Grand Totals (N records)" both stops parsing and
  cross-checks the extracted row count.

Annual roll-ups overlap the monthlies, so their rows are kept only for
months with no dedicated monthly file (all of 2021; Jan + Mar 2022).
``fetch`` consolidates everything into one CSV at ``paths.raw``;
``parse`` normalizes that CSV to the unified schema.
"""

import bisect
import csv
import dataclasses
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
import requests

import warn_monitor
from .base import Source, StatePaths

log = logging.getLogger("warn_sources")

PAGE_URL = (
    "https://mn.gov/deed/programs-services/dislocated-worker-program/reports/"
)

# Internet Archive CDX index: every archived plant-closing report asset.
CDX_URL = (
    "http://web.archive.org/cdx/search/cdx"
    "?url=mn.gov/deed/assets/plant-closing*"
    "&output=json&filter=statuscode:200&collapse=urlkey"
    "&fl=timestamp,original"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Raw columns of the consolidated CSV written by fetch().
RAW_COLUMNS = [
    "company",
    "city",
    "industry",
    "layoff_start",
    "warn_act",
    "warn_received",
    "layoff_type",
    "layoff_status",
    "affected_workers",
    "period",
    "source_file",
]

MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}

_TCM_RE = re.compile(r"tcm\d+-(\d+)", re.I)
# Start of the report's trailing legend ("*** The Plant Closings/Mass
# Layoffs Report is a summary …", "• Suspend/Hold: …"). Nothing but
# definitions follows it, so hitting one ends the whole document. The
# title line ("***PLANT …") has no space after the asterisks and is
# filtered separately.
_LEGEND_RE = re.compile(
    r"^(\*{1,3}\s*The\b|•|o\s+The\b|Layoff Status:|Needs Assessment:)"
)
_YEAR_RE = re.compile(r"20\d{2}")
_RR_GROUP_RE = re.compile(r"RR Start Date:\s*([A-Za-z]+)\s+(20\d{2})")
_GRAND_RE = re.compile(r"Grand Totals\s*\((\d+)\s*records?\)")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# WARN Act flag vocabulary; anything else in that column marks a junk line.
_ACT_OK = {"", "yes", "no", "true", "false", "tbd", "-"}
_WORKERS_OK = re.compile(r"^(|-|tbd|[\d,]+)$", re.I)
_SKIP_PREFIXES = (
    "rr start date", "grand total", "layoff name", "***plant",
    "plant closings", "layoff status:",
)

# Text columns a wrapped cell may continue into; the rest must be empty
# on a continuation line.
_TEXT_FIELDS = ("company", "city", "industry", "layoff_type", "layoff_status")
_HARD_FIELDS = (
    "layoff_start", "warn_act", "warn_received", "affected_workers",
)

# Normalized lattice header cell -> raw field ("Account: City" and bare
# "City" are era variants; TAA Related / Federal Impact / Layoff Count are
# deliberately unmapped).
_LATTICE_HEADER_MAP = {
    "layoff name": "company",
    "account: city": "city",
    "city": "city",
    "account: industry": "industry",
    "industry": "industry",
    "layoff start": "layoff_start",
    "warn act": "warn_act",
    "warn received": "warn_received",
    "layoff type": "layoff_type",
    "layoff status": "layoff_status",
    "affected workers": "affected_workers",
}

# Word-geometry column anchors: (raw field, header keyword candidates,
# first found wins). Underscored fields exist only so their values don't
# bleed into neighbors. Some months title the TAA column just "TAA" (or
# hyphenate "Relat-ed" across lines), hence the fallback candidate.
_WORD_KEYWORDS = [
    ("city", ("City",)),
    ("industry", ("Industry",)),
    ("layoff_start", ("Start",)),
    ("warn_act", ("Act",)),
    ("warn_received", ("Received",)),
    ("layoff_type", ("Type",)),
    ("layoff_status", ("Status",)),
    ("_taa", ("Related", "TAA")),
    ("_federal", ("Impact",)),
    ("affected_workers", ("Workers",)),
    ("_count", ("Count",)),
]

# Header words that prefix a keyword (stacked above it or adjacent left)
# and widen its column extent: "**Layoff" over "Start", "Account:" over
# "City", "Affected" over "Workers", "Layoff Status" on one line, …
_PREFIX_WORDS = {
    "Account:", "Layoff", "**Layoff", "WARN", "TAA", "Affected",
    "*Affected", "Federal",
}


def _clean_text(text) -> str:
    """Collapse newlines/whitespace in a cell."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _file_period(name: str):
    """Report basename -> (year, month|None); None if no year found."""
    stem = name.lower().split("_tcm")[0]
    m = _YEAR_RE.search(stem)
    if not m:
        return None
    year = int(m.group(0))
    month = next((i for mn, i in MONTHS.items() if mn in stem), None)
    return year, month


def _tcm_id(name: str) -> int:
    m = _TCM_RE.search(name)
    return int(m.group(1)) if m else 0


def _group_period(text: str) -> str:
    """'RR Start Date: June 2021 …' -> '2021-06' ('' if not a group)."""
    m = _RR_GROUP_RE.search(text)
    if not m:
        return ""
    month = MONTHS.get(m.group(1).lower())
    return f"{m.group(2)}-{month:02d}" if month else ""


def _looks_like_data(rec: dict) -> bool:
    """True for a real report row; filters headers/totals/legend text."""
    company = rec.get("company", "").strip()
    if not company or company.startswith(("*", "•")):
        return False
    low = company.lower()
    if any(low.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if rec.get("warn_act", "").strip().lower() not in _ACT_OK:
        return False
    if not _WORKERS_OK.match(rec.get("affected_workers", "").strip()):
        return False
    filled = sum(
        bool(rec.get(f, "").strip())
        for f in ("city", "industry", "layoff_status", "layoff_start")
    )
    return filled >= 2


def _is_continuation(rec: dict) -> bool:
    """A wrapped-cell remnant: text-column words only, no hard fields."""
    if not any(rec.get(f, "").strip() for f in _TEXT_FIELDS):
        return False
    return not any(rec.get(f, "").strip() for f in _HARD_FIELDS)


# ---------------------------------------------------------------------------
# Engine A: bordered grid tables (≈ Oct 2025-present)
# ---------------------------------------------------------------------------


def _map_lattice_header(cells: list) -> Optional[dict]:
    """Header row cells -> {field: index}, or None if fragmented."""
    colmap: dict = {}
    for j, cell in enumerate(cells):
        key = _clean_text(cell).lower().strip("*")
        field = _LATTICE_HEADER_MAP.get(key)
        if field and field not in colmap:
            colmap[field] = j
    required = {"company", "city", "affected_workers"}
    return colmap if required <= set(colmap) else None


def _extract_lattice(pdf) -> list:
    """Grid-table PDF -> raw row dicts (column map carried forward)."""
    rows: list = []
    colmap = None
    group = ""
    for page in pdf.pages:
        for table in page.extract_tables():
            for raw_row in table:
                cells = [_clean_text(c) for c in raw_row]
                text = " ".join(c for c in cells if c)
                if not text:
                    continue
                if "Grand Totals" in text or _LEGEND_RE.match(text):
                    return rows
                g = _group_period(text)
                if g:
                    group = g
                    continue
                if any(c.lower().strip("*") == "layoff name" for c in cells):
                    colmap = _map_lattice_header(cells)
                    continue
                if colmap is None:
                    continue
                rec = {
                    f: (cells[j] if j < len(cells) else "")
                    for f, j in colmap.items()
                }
                if _looks_like_data(rec):
                    rec["_group"] = group
                    rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# Engine B: gridless text, columns rebuilt from word geometry (2021-2025)
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


def _widen(ext: list, band: list, line_of: dict) -> None:
    """Union a keyword's extent with its stacked/adjacent prefix words."""
    for v in band:
        if v["text"] not in _PREFIX_WORDS:
            continue
        lo, hi = max(v["x0"], ext[0]), min(v["x1"], ext[1])
        stacked = hi - lo > 0.4 * (v["x1"] - v["x0"])
        adjacent = (
            abs(line_of[id(v)] - ext[2]) <= 3
            and v["x1"] <= ext[0]
            and ext[0] - v["x1"] <= 8
        )
        if stacked or adjacent:
            ext[0] = min(ext[0], v["x0"])
            ext[1] = max(ext[1], v["x1"])


# Columns whose values are left-aligned under the header start; all other
# columns print centered/right-aligned values that overhang the header.
_LEFT_ALIGNED = {"company", "city", "industry"}


def _find_word_columns(lines: list) -> Optional[dict]:
    """Locate the header and each column's keyword x-extent, or None."""
    name_pair = None
    for line in lines:
        for i, w in enumerate(line[:-1]):
            if (
                w["text"].lstrip("*") == "Layoff"
                and line[i + 1]["text"].rstrip(":") == "Name"
            ):
                name_pair = (w, line[i + 1])
                break
        if name_pair:
            break
    if not name_pair:
        return None
    top = name_pair[0]["top"]
    band, line_of = [], {}
    for line in lines:
        for w in line:
            if top - 16 <= w["top"] <= top + 32:
                band.append(w)
                line_of[id(w)] = w["top"]
    exts = {"company": [0.0, name_pair[1]["x1"]]}
    bottom = max(w["bottom"] for w in name_pair)
    for field, kws in _WORD_KEYWORDS:
        hit = next(
            (w for kw in kws for w in band if w["text"].strip("*:") == kw),
            None,
        )
        if hit is None:
            continue
        ext = [hit["x0"], hit["x1"], hit["top"]]
        _widen(ext, band, line_of)
        exts[field] = ext[:2]
        bottom = max(bottom, hit["bottom"])
    if "city" not in exts or "affected_workers" not in exts:
        return None
    order = sorted(exts.items(), key=lambda kv: kv[1][0])
    return {"order": order, "bottom": bottom}


def _coverage(lines: list, header_bottom) -> list:
    """Merged x-intervals covered by the page's candidate data words."""
    spans = []
    for line in lines:
        if header_bottom is not None and line[0]["top"] <= header_bottom:
            continue
        text = " ".join(w["text"] for w in line)
        if "Grand Totals" in text:
            break
        if _RR_GROUP_RE.search(text):
            continue
        spans.extend((w["x0"], w["x1"]) for w in line)
    spans.sort()
    merged: list = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _derive_lefts(order: list, lines: list, header_bottom) -> list:
    """Column split points: the widest word-free gutter between anchors.

    The report's cells are variously left-, center- and right-aligned, so
    header positions alone misplace boundaries (a right-aligned date can
    start left of its own header). The gutters between the actual data
    words are unambiguous; header geometry is only the fallback when a
    page has too little data to show a gutter.
    """
    merged = _coverage(lines, header_bottom)
    lefts = [order[0][1][0] - 4]
    for (_, pext), (field, ext) in zip(order, order[1:]):
        if field in _LEFT_ALIGNED:
            fallback = ext[0] - 4
        else:
            fallback = (pext[1] + ext[0]) / 2
        lo, hi = pext[0] + 1, ext[1] - 1
        gaps = []  # word-free gutters (>= 3pt) between the two anchors
        prev_end = lo
        for a, b in merged:
            if b <= lo:
                continue
            if a >= hi:
                break
            gap_lo, gap_hi = max(prev_end, lo), min(a, hi)
            if gap_hi - gap_lo >= 3:
                gaps.append((gap_lo, gap_hi))
            prev_end = max(prev_end, min(b, hi))
        if hi - prev_end >= 3:
            gaps.append((prev_end, hi))
        left = fallback
        if gaps:
            # the gutter containing the header-derived point — or near it.
            # A single long value can bridge the truthful gutter (e.g. a
            # company name running into the City column), leaving only
            # far-away gutters; trusting those once misplaced the company
            # boundary at x≈3 and blanked a whole page. Beyond 30pt the
            # header-derived fallback is more credible than any gutter.
            def _dist(g):
                return max(g[0] - fallback, fallback - g[1], 0.0)

            g = min(gaps, key=_dist)
            if _dist(g) <= 30:
                left = (g[0] + g[1]) / 2
        lefts.append(max(left, lefts[-1] + 1))
    return lefts


def _assign_line(line: list, cols: dict) -> dict:
    """Distribute one visual line's words into columns by x-position."""
    vals: dict = {f: [] for f, _ in cols["order"]}
    for w in line:
        i = max(bisect.bisect_right(cols["lefts"], w["x0"]) - 1, 0)
        vals[cols["order"][i][0]].append(w["text"])
    return {f: " ".join(v) for f, v in vals.items() if not f.startswith("_")}


def _extract_words(pdf) -> list:
    """Gridless PDF -> raw row dicts via word-geometry columns."""
    rows: list = []
    order = None
    group = ""
    for page in pdf.pages:
        lines = _word_lines(page)
        found = _find_word_columns(lines)
        header_bottom = None
        if found:
            order = found["order"]
            header_bottom = found["bottom"]
        if order is None:
            continue
        cols = {
            "order": order,
            "lefts": _derive_lefts(order, lines, header_bottom),
        }
        prev = None  # never merge a wrap across a page boundary
        for line in lines:
            text = " ".join(w["text"] for w in line)
            if "PLANT CLOSINGS" in text:
                # page title; only page 1's sits above the header filter
                continue
            if "Grand Totals" in text or _LEGEND_RE.match(text):
                return rows
            g = _group_period(text)
            if g:
                group = g
                prev = None
                continue
            if header_bottom is not None and line[0]["top"] <= header_bottom:
                continue
            rec = _assign_line(line, cols)
            if _looks_like_data(rec):
                rec["_group"] = group
                rows.append(rec)
                prev = rec
            elif prev is not None and _is_continuation(rec):
                for f in _TEXT_FIELDS:
                    if rec.get(f, "").strip():
                        prev[f] = _clean_text(prev.get(f, "") + " " + rec[f])
    return rows


# ---------------------------------------------------------------------------
# Per-file extraction: try the grid engine, cross-check, fall back
# ---------------------------------------------------------------------------


def _extract_pdf(pdf_path) -> list:
    """One report PDF -> raw row dicts, choosing the matching engine.

    The old files print their own row count ("Grand Totals (N records)");
    whichever engine reproduces it wins. Files without the footer (the
    modern grid era) trust the lattice pass when it yields rows.
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        expected_m = _GRAND_RE.search(full_text)
        expected = int(expected_m.group(1)) if expected_m else None
        rows = _extract_lattice(pdf)
        if expected is None:
            if rows:
                return rows
            return _extract_words(pdf)
        if len(rows) == expected:
            return rows
        word_rows = _extract_words(pdf)
        if len(word_rows) == expected:
            return word_rows
        chosen = word_rows if len(word_rows) > len(rows) else rows
        log.warning(
            f"[MN] {Path(str(pdf_path)).name}: extracted {len(chosen)} rows, "
            f"file says {expected}"
        )
        return chosen


class MinnesotaDEED(Source):
    code = "mn"
    name = "Minnesota"
    agency = "Minnesota Department of Employment and Economic Development"
    source_url = PAGE_URL
    cadence = "monthly"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        paths = StatePaths.for_state(self.code, data_dir)
        return dataclasses.replace(paths, raw=paths.root / "raw_download.csv")

    # -- fetch --------------------------------------------------------------

    @staticmethod
    def _get(url: str, timeout: int = 90):
        """Polite GET with retries; returns the response or None."""
        last = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = requests.get(
                    url, headers=BROWSER_HEADERS, timeout=timeout
                )
                time.sleep(1.1)  # politeness: ~1 request/second/host
                if resp.status_code == 200:
                    return resp
                last = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                last = str(exc)
        log.warning(f"[MN] giving up on {url}: {last}")
        return None

    def _discover(self) -> Optional[list]:
        """CDX index -> catalog entries, deduped one file per period."""
        resp = self._get(CDX_URL, timeout=60)
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("[MN] CDX returned non-JSON")
            return None
        best: dict = {}
        for ts, original in data[1:]:
            name = original.rsplit("/", 1)[-1]
            period = _file_period(name)
            if not name.lower().endswith(".pdf") or period is None:
                log.warning(f"[MN] skipping unrecognized asset {name}")
                continue
            entry = {
                "name": name,
                "url": original,
                "ts": ts,
                "year": period[0],
                "month": period[1],
            }
            prior = best.get(period)
            if prior is None or _tcm_id(name) > _tcm_id(prior["name"]):
                best[period] = entry
        return sorted(
            best.values(), key=lambda e: (e["year"], e["month"] or 0)
        )

    def _download(self, entry: dict, dest: Path) -> bool:
        """Fetch one PDF: live mn.gov first, Wayback raw copy second."""
        urls = [
            entry["url"],
            f"http://web.archive.org/web/{entry['ts']}id_/{entry['url']}",
        ]
        for url in urls:
            resp = self._get(url)
            if resp is not None and resp.content[:5] == b"%PDF-":
                dest.write_bytes(resp.content)
                return True
        return False

    def fetch(self, force: bool = False) -> tuple:
        """Discover, download, extract and consolidate to one CSV.

        Archived PDFs are immutable monthly snapshots and are fetched
        once; the two most recent months are re-downloaded every run in
        case DEED revised them in place. Annual roll-up rows are kept
        only for months with no dedicated monthly file. ``changed`` is
        a content hash of the consolidated CSV.
        """
        self.paths.ensure()
        archive = self.paths.root / "archive"
        archive.mkdir(parents=True, exist_ok=True)

        catalog = self._discover()
        if catalog is None:
            # CDX outage: fall back to whatever is already cached.
            catalog = [
                {
                    "name": p.name,
                    "url": "",
                    "ts": "",
                    "year": _file_period(p.name)[0],
                    "month": _file_period(p.name)[1],
                }
                for p in sorted(archive.glob("*.pdf"))
                if _file_period(p.name) is not None
            ]
            if not catalog:
                raise RuntimeError(
                    "MN feed: CDX discovery failed and no cached PDFs"
                )
            log.warning(
                f"[MN] CDX unavailable — using {len(catalog)} cached PDFs"
            )

        monthly = [e for e in catalog if e["month"]]
        refresh = {
            e["name"]
            for e in sorted(monthly, key=lambda e: (e["year"], e["month"]))[-2:]
        }

        monthly_rows: list = []
        annual_batches: list = []
        coverage: set = set()
        for entry in catalog:
            dest = archive / entry["name"]
            if entry["url"] and (
                force or entry["name"] in refresh or not dest.exists()
            ):
                if not self._download(entry, dest) and not dest.exists():
                    log.warning(f"[MN] could not fetch {entry['name']}")
                    continue
            if not dest.exists():
                continue
            try:
                rows = _extract_pdf(dest)
            except Exception as exc:  # noqa: BLE001 — one bad PDF only
                log.warning(f"[MN] failed to parse {entry['name']}: {exc}")
                continue
            period = (
                f"{entry['year']}-{entry['month']:02d}"
                if entry["month"]
                else ""
            )
            for rec in rows:
                rec["period"] = period or rec.pop("_group", "")
                rec.pop("_group", None)
                rec["source_file"] = entry["name"]
            if entry["month"]:
                coverage.add(period)
                monthly_rows.extend(rows)
            else:
                annual_batches.append((entry, rows))
            log.info(f"[MN] {entry['name']}: {len(rows)} rows")

        all_rows = monthly_rows
        for entry, rows in annual_batches:
            kept = [
                r for r in rows if r["period"] and r["period"] not in coverage
            ]
            dateless = sum(1 for r in rows if not r["period"])
            if dateless:
                log.warning(
                    f"[MN] {entry['name']}: dropped {dateless} rows with no "
                    "RR Start Date group"
                )
            all_rows.extend(kept)
        if not all_rows:
            raise RuntimeError("MN feed: no rows extracted from any PDF")
        all_rows.sort(key=lambda r: (r["period"], r["source_file"]))

        with open(self.paths.raw, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=RAW_COLUMNS)
            writer.writeheader()
            for rec in all_rows:
                writer.writerow({f: rec.get(f, "") for f in RAW_COLUMNS})

        digest = hashlib.sha256(self.paths.raw.read_bytes()).hexdigest()
        meta = warn_monitor._load_meta(self.paths.meta)
        changed = digest != meta.get("file_hash", "")
        meta.update(
            {
                "file_hash": digest,
                "last_checked": datetime.now(timezone.utc).isoformat() + "Z",
                "url": PAGE_URL,
                "source_files": len(catalog),
            }
        )
        warn_monitor._save_meta(meta, self.paths.meta)
        return changed, str(self.paths.raw)

    # -- parse --------------------------------------------------------------

    @staticmethod
    def _clean_date(val) -> Optional[str]:
        """Cell -> ISO YYYY-MM-DD or None ("TBD"/"-" stay None).

        YES/NO/TRUE/FALSE are also silent Nones: two vintages print the
        WARN *flag* in the received column (Dec 2023 literally publishes
        "NO"; Jul 2025 has no separate "WARN Act" column and its "WARN
        Received" holds the flag) — no date exists on those rows.
        """
        text = _clean_text(val)
        nulls = {"tbd", "-", "n/a", "na", "yes", "no", "true", "false"}
        if not text or text.lower() in nulls:
            return None
        # A long neighboring cell can push a stray word into the date
        # column ("Assist 11/1/23"); keep just the date token.
        m = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text)
        if m:
            text = m.group(0)
        iso = warn_monitor._safe_date(text)
        if iso and _ISO_RE.match(iso) and 2000 <= int(iso[:4]) <= 2100:
            return iso
        log.warning(f"[MN] unparseable date {text!r} -> None")
        return None

    @staticmethod
    def _clean_jobs(val) -> int:
        """Cell -> int count; 0 when the state published none ("TBD")."""
        n = warn_monitor._safe_int(_clean_text(val))
        return n if n is not None and n > 0 else 0

    @staticmethod
    def _clean_type(val) -> str:
        """Layoff type; strips a leading "-" (the report's null marker
        for the neighboring WARN Received cell, which prints so close to
        the Type column that some vintages bleed it across)."""
        return re.sub(r"^[-–\s]+", "", _clean_text(val))

    def parse(self, raw_path) -> pd.DataFrame:
        raw = pd.read_csv(
            raw_path, dtype=str, keep_default_na=False, encoding="utf-8"
        )
        records = []
        for row in raw.to_dict(orient="records"):
            company = _clean_text(row.get("company"))
            if not company:
                continue
            records.append(
                {
                    "company": company,
                    "notice_date": self._clean_date(row.get("warn_received")),
                    "effective_date": self._clean_date(
                        row.get("layoff_start")
                    ),
                    "employees": self._clean_jobs(row.get("affected_workers")),
                    "layoff_type": self._clean_type(row.get("layoff_type")),
                    "city": _clean_text(row.get("city")),
                    "industry": _clean_text(row.get("industry")),
                }
            )
        log.info(f"[MN] parsed {len(records)} records")
        df = pd.DataFrame(
            records,
            columns=[
                "company",
                "notice_date",
                "effective_date",
                "employees",
                "layoff_type",
                "city",
                "industry",
            ],
        )
        for col in ("notice_date", "effective_date"):
            df[col] = df[col].astype(object).where(df[col].notna(), None)
        return df
