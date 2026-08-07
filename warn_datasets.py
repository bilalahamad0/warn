"""
warn_datasets.py
----------------
Resolves which records back the California dashboard, and derives them.

Why this exists
~~~~~~~~~~~~~~~
California had two disjoint stores and the dashboards disagreed about it:

* ``data/warn_cumulative.json`` — the live EDD feed, whose earliest notice is
  2025-01-29. It backed the California dashboard's KPIs, table and nine of its
  twelve charts.
* ``data/historical/ca_national_history.json`` — the deep backfill, merged into
  ``data/warn_national.json`` by ``warn_sources.aggregate``. It contributes 71
  notices dated 2025-01-03 → 2025-01-28, worth 5,475 employees, that the live
  feed never had.

The merge dedupes nothing because the two sets are disjoint, so the US
dashboard counted 827 California notices for calendar 2025 while the California
dashboard counted 756 — same state, same year, two numbers, on one site.

The fix is to stop treating the live feed as California's source of truth and
derive the California view from the national dataset instead, which already
contains everything. One upstream, so the two pages cannot drift again.

Why a coverage boundary
~~~~~~~~~~~~~~~~~~~~~~~
The national CA slice reaches back to 2008, but only its 2025-onward rows carry
the ``industry`` field (the backfill has none at all) and pre-2025 rows are
missing county on ~15% of records. The California dashboard's industry chart,
industry filter and county filter would all silently degrade across that
boundary. So the dashboard covers 2025 onward — a calendar-year edge, chosen so
the 2025 KPI year is whole and directly comparable to the US dashboard's own
California slice — and says so on the page.

The invariant that keeps this honest is in ``tests/test_datasets.py``: *every*
California record in the national dataset dated on or after
``CA_COVERAGE_START`` must appear in the derived payload. A constant cannot
promise no-drift; that test can, and it fails loudly if the backfill is
extended, the merge changes, or the boundary moves.

Nothing is written to ``data/``. The payload is derived in memory and published
as ``docs/ca/data.json``, which is the inspectable artifact — persisting a
second copy under ``data/`` would just churn ~640 KB into git twice a day.

Imports nothing else from the project: ``warn_charts`` and ``warn_publish``
both depend on it, and ``warn_charts`` must not be made to pull in the whole
47-module ``warn_sources`` package just to resolve a path.
"""

import json
import logging
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

LATEST_FILE = DATA_DIR / "warn_latest.json"
CUMULATIVE_FILE = DATA_DIR / "warn_cumulative.json"
NATIONAL_FILE = DATA_DIR / "warn_national.json"

# Earliest notice date the California dashboard covers. See the module
# docstring for why this boundary exists and why it is a calendar-year edge.
CA_COVERAGE_START = "2025-01-01"

# The record shape ``docs/ca/data.json`` publishes. Fixed and exhaustive: the
# 71 backfilled records omit ``city`` and ``industry`` as *keys* rather than
# leaving them empty, and this file is a public API whose consumers should not
# have to cope with a field appearing and disappearing by row.
CA_RECORD_FIELDS = (
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
    "industry",
    "state",
)

log = logging.getLogger("warn_datasets")

_ca_cache = None


def _records(payload) -> list:
    """Records out of either payload shape (``{"records": [...]}`` or a list)."""
    if isinstance(payload, dict):
        return payload.get("records", [])
    return payload if isinstance(payload, list) else []


def _read(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _normalise(record: dict) -> dict:
    """Project a record onto CA_RECORD_FIELDS, filling absent keys with ""."""
    out = {}
    for field in CA_RECORD_FIELDS:
        value = record.get(field)
        out[field] = "" if value is None else value
    out["employees"] = _int(record.get("employees"))
    out["state"] = "CA"
    return out


def build_ca_dashboard(national_file: Path = None,
                       start: str = CA_COVERAGE_START) -> dict:
    """Derive the California dashboard payload from the national CA slice.

    Returns a payload in the same shape the dashboard has always consumed
    (``{"records": [...], "total_records": N, ...}``) so every downstream
    reader is unchanged. Totals are recomputed here rather than inherited from
    the national payload, which counts 47 jurisdictions.

    Raises ``FileNotFoundError`` when the national dataset is absent — callers
    fall back via :func:`load_ca_dashboard`.
    """
    path = Path(national_file) if national_file else NATIONAL_FILE
    national = _read(path)
    if national is None:
        raise FileNotFoundError(f"National dataset not found — {path}")

    records = [
        _normalise(r)
        for r in _records(national)
        if str(r.get("state") or "").upper() == "CA"
        and str(r.get("notice_date") or r.get("effective_date") or "")[:10] >= start
    ]
    # Newest first, so a rebuilt docs/ca/data.json diffs cleanly run to run.
    records.sort(key=lambda r: str(r.get("notice_date") or ""), reverse=True)

    dates = sorted(str(r.get("notice_date") or "")[:10] for r in records
                   if r.get("notice_date"))

    # Keep the live feed's provenance field on the published API even though the
    # records now come via the national dataset — the underlying filing source
    # is still the EDD.
    live = _read(CUMULATIVE_FILE) or {}

    return {
        "scope": "ca",
        "state": "CA",
        "coverage_start": start,
        "last_updated": (national.get("last_updated")
                         if isinstance(national, dict) else "") or "",
        "source_url": live.get("source_url", ""),
        "total_records": len(records),
        "total_employees": sum(_int(r.get("employees")) for r in records),
        "date_range_start": dates[0] if dates else "",
        "date_range_end": dates[-1] if dates else "",
        "records": records,
    }


def load_ca_dashboard(national_file: Path = None, refresh: bool = False) -> dict:
    """The California dashboard payload, cached per process.

    ``warn_publish`` and ``warn_charts`` both call this, so the page's KPIs,
    its table and its charts are guaranteed to describe the same records.

    Falls back to the raw cumulative store (then the latest download) if the
    national dataset is missing or unreadable — degraded but correct-shaped,
    which is what the dashboard did before this module existed. The fallback
    payload is returned as-is and carries no ``coverage_start``, which is how
    ``warn_publish`` detects the degraded state and says so on the page.
    """
    global _ca_cache
    if _ca_cache is not None and not refresh:
        return _ca_cache

    try:
        _ca_cache = build_ca_dashboard(national_file)
        return _ca_cache
    except Exception as exc:  # noqa: BLE001 — a degraded page beats no page
        log.warning(
            "Falling back to the raw California store — the dashboard will "
            "under-report early 2025 (%s)", exc,
        )

    for path in (CUMULATIVE_FILE, LATEST_FILE):
        payload = _read(path)
        if payload is not None:
            _ca_cache = payload
            return _ca_cache

    raise FileNotFoundError(
        "Run warn_monitor.py first — no California dataset found "
        f"({NATIONAL_FILE}, {CUMULATIVE_FILE}, {LATEST_FILE})"
    )


def ca_dashboard_records(national_file: Path = None) -> list:
    """Convenience: just the records the California dashboard shows."""
    return _records(load_ca_dashboard(national_file))


def ca_yearly_summary(national_file: Path = None, today: date = None) -> list:
    """Per-calendar-year California totals, for the year-over-year chart.

    Not bounded by CA_COVERAGE_START. The dashboard's boundary exists because
    pre-2025 records carry no ``industry`` and patchy ``county``, which the
    industry and county visuals depend on — but a year-over-year chart needs
    only ``notice_date`` and ``employees``, and both are present on 100% of the
    16,176 historical California records. So this reaches back as far as the
    data honestly goes.

    **Calendar years, not fiscal years.** The chart used to bucket by CA EDD's
    July–June fiscal year purely because its source was EDD's fiscal-year PDFs.
    That source is retired here, and every other figure on the dashboard — the
    KPI date-range selector, the US dashboard's per-year views — is calendar.
    The old fiscal framing was already broken in practice: the single "complete"
    bar, labelled ``FY 2025-26 (Live)``, actually held 19 months spanning three
    fiscal years.

    Years before a gap in coverage are dropped, not shown as near-zero bars:
    the backfill has one stray 2008 notice and then nothing until 2014, and
    plotting that lone record next to 2020 would read as "2008 was quiet"
    rather than "2008 is not covered". What gets dropped is logged.

    A year is flagged ``partial`` when it is still running, or when some month
    in it recorded no filings at all. California files 18–500 WARN notices in a
    normal month, so an empty month means missing data, never a quiet month —
    and two such gaps exist today that would otherwise be read as real declines:

    * 2014 — the backfill effectively starts in July; Jan–Jun holds 7 records
      against a ~55/month run rate afterwards.
    * 2025 — February, March and April are absent from the EDD feed *and* from
      the backfill, so the year's 827 notices undercount. Charted flat against
      2024's 1,502 that reads as a 45% drop that did not happen.

    Returns oldest-first, each entry::

        {"year": 2020, "label": "2020", "records": 6066, "employees": 656501,
         "partial": False, "gap_months": []}

    Returns ``[]`` when the national dataset is unavailable. The caller renders
    an empty state rather than falling back to the PDF summary in
    ``warn_all_years.json`` — that sample captured 3–5% of actual filings
    (FY2019-20: 17 notices where 5,143 were filed), and a chart that wrong is
    worse than no chart, which is the whole reason this function exists.
    """
    path = Path(national_file) if national_file else NATIONAL_FILE
    national = _read(path)
    if national is None:
        log.warning("No national dataset at %s — year-over-year chart skipped", path)
        return []

    totals = {}
    months = {}
    for record in _records(national):
        if str(record.get("state") or "").upper() != "CA":
            continue
        stamp = str(record.get("notice_date") or "")[:10]
        if len(stamp) != 10 or not stamp[:4].isdigit():
            continue
        year = int(stamp[:4])
        count, employees = totals.get(year, (0, 0))
        totals[year] = (count + 1, employees + _int(record.get("employees")))
        months.setdefault(year, set()).add(stamp[5:7])

    if not totals:
        return []

    current_year = (today or date.today()).year
    # A typo'd future notice_date (a lone 2103 record, say) must not become
    # the anchor of the contiguous-run scan below — it would break the chain
    # at the fake year and silently wipe every real year off the chart. Years
    # beyond next year cannot be legitimate filings; drop them up front.
    # (current_year + 1 stays: December filings for January layoffs are real.)
    bogus = sorted(y for y in totals if y > current_year + 1)
    if bogus:
        log.warning(
            "Year-over-year: ignoring %d record(s) in impossible future "
            "year(s) %s — likely feed typos",
            sum(totals[y][0] for y in bogus), ", ".join(map(str, bogus)),
        )
        for y in bogus:
            del totals[y]
    if not totals:
        return []

    years = sorted(totals)
    # Keep the longest unbroken run ending at the most recent year — that is
    # the span the dataset actually covers continuously.
    start = years[-1]
    for year in reversed(years):
        if year == start or year == start - 1:
            start = year
        else:
            break
    dropped = [y for y in years if y < start]
    if dropped:
        log.info(
            "Year-over-year: ignoring %d pre-coverage year(s) before %d (%s) — "
            "isolated records ahead of a gap, not complete years",
            len(dropped), start, ", ".join(str(y) for y in dropped),
        )

    summary = []
    for year in years:
        if year < start:
            continue
        gaps = sorted({f"{m:02d}" for m in range(1, 13)} - months.get(year, set()))
        running = year >= current_year
        if running:
            # Months after today are not gaps, they simply have not happened.
            gaps = []
        summary.append({
            "year": year,
            "label": str(year),
            "records": totals[year][0],
            "employees": totals[year][1],
            "partial": running or bool(gaps),
            "gap_months": gaps,
        })
        if gaps:
            log.info(
                "Year-over-year: %d has no filings in %s — charted as "
                "incomplete so the total is not read as a real decline",
                year, ", ".join(gaps),
            )
    return summary


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# A state whose feed produces filings in at least this share of its elapsed
# months is "dense": an empty month there is evidence of a broken or switched
# feed, not a quiet month. Sparse states (a handful of filings a year) have
# genuinely empty months all the time and are never flagged on months alone.
_DENSE_STATE_THRESHOLD = 0.75
_MIN_SUSPECT_MONTHS = 3


def format_month_gaps(codes: list) -> str:
    """['02','03','04','12'] -> 'Feb–Apr, Dec' (for hover captions)."""
    nums = sorted(int(c) for c in codes)
    runs, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i + 1 == len(nums) or nums[i + 1] != n + 1:
            a, b = _MONTH_ABBR[start - 1], _MONTH_ABBR[n - 1]
            runs.append(a if a == b else f"{a}–{b}")
            start = None
    return ", ".join(runs)


def state_year_coverage(records: list, today: date = None) -> dict:
    """Per-state, per-year gap assessment for the national dataset.

    Generalises the California rule ("an empty month is missing data, never a
    quiet month") to every state — calibrated by each state's own filing rate,
    because the rule is only true of states that file constantly. Exists so
    the US map and the per-state trend chart can render a source gap as
    *missing data* instead of an affirmative zero: before this, Ohio's dead
    2023-2025 feed hovered as "no WARN notices recorded in 2023", and New
    York's seven silent months of 2025 (the DOL's Tableau migration) read as
    an 80% collapse in filings.

    Uses the same date resolution as the charts (notice_date, else
    effective_date). Returns::

        {"OH": {"span": (2016, 2026), "density": 0.94,
                "years": {2023: {"records": 0, "missing_months": [..12..],
                                 "empty": True, "suspect_gap": True}, ...}},
         ...}

    Flag rules, deliberately simple enough to explain in a hover:

    * A year with **zero records strictly inside** the state's span is
      ``suspect_gap`` when the state normally files enough that a silent year
      is implausible (>= 6 records/year over its active years). A state that
      files three times a year can genuinely have none; Ohio at ~80/year
      cannot.
    * A year **with** records is ``suspect_gap`` only when the state is dense
      — files in >= 75% of the months its feed is alive (fully-empty years
      are excluded from that denominator: they are the *output* of the
      empty-year rule and must not dilute the input of this one, or a long
      dead stretch would disguise a later partial outage, as California's
      2009-13 backfill gap otherwise would) — and at least three elapsed
      months of that year are empty.
    * Months after the current one have not happened and are never missing;
      the running year's density window ends at last month.
    """
    now = today or date.today()
    by_state = {}
    for r in records:
        code = str(r.get("state") or "").upper()
        stamp = str(r.get("notice_date") or r.get("effective_date") or "")[:7]
        if len(code) != 2 or len(stamp) != 7 or not stamp[:4].isdigit():
            continue
        by_state.setdefault(code, {}).setdefault(int(stamp[:4]), set()).add(stamp[5:])

    def elapsed_months(year: int) -> list:
        if year < now.year:
            return [f"{m:02d}" for m in range(1, 13)]
        if year > now.year:
            return []
        return [f"{m:02d}" for m in range(1, now.month)]

    out = {}
    for code, years_months in by_state.items():
        counts = {}
        for r in records:
            if str(r.get("state") or "").upper() != code:
                continue
            stamp = str(r.get("notice_date") or r.get("effective_date") or "")[:7]
            if len(stamp) == 7:
                counts[stamp] = counts.get(stamp, 0) + 1

        lo, hi = min(years_months), max(years_months)
        total = active = 0
        for year in range(lo, min(hi, now.year) + 1):
            if not years_months.get(year):
                continue  # fully-empty year: the empty-year rule's business
            for m in elapsed_months(year):
                total += 1
                if m in years_months.get(year, set()):
                    active += 1
        density = (active / total) if total else 0.0

        active_years = [y for y in years_months if years_months[y]]
        annual_mean = (
            sum(counts.get(f"{y}-{m}", 0)
                for y in active_years for m in years_months[y])
            / len(active_years)
        ) if active_years else 0.0

        years = {}
        for year in range(lo, hi + 1):
            present = years_months.get(year, set())
            missing = [m for m in elapsed_months(year) if m not in present]
            empty = not present
            suspect = (
                (empty and lo < year < hi and annual_mean >= 6)
                or (not empty
                    and density >= _DENSE_STATE_THRESHOLD
                    and len(missing) >= _MIN_SUSPECT_MONTHS)
            )
            years[year] = {
                "records": sum(counts.get(f"{year}-{m}", 0)
                               for m in years_months.get(year, set())),
                "missing_months": missing,
                "empty": empty,
                "suspect_gap": suspect,
            }
        out[code] = {"span": (lo, hi), "density": round(density, 3),
                     "years": years}
    return out


def reset_cache() -> None:
    """Drop the cached payload (tests, and any in-process rebuild)."""
    global _ca_cache
    _ca_cache = None
