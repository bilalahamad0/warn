"""
warn_digest.py
--------------
Builds the whole-US **monthly** WARN digest email.

One calendar month of the national dataset (``data/warn_national.json``),
analysed across every state the platform tracks, rendered as an email-safe HTML
document plus a stand-alone plain-text alternative.

    from warn_digest import build_monthly_digest
    digest = build_monthly_digest(2026, 6)
    digest["subject"], digest["html"], digest["text"], digest["stats"]

Design rules this module is built around:

* **A record's month is its event date** — ``notice_date`` when the state
  published one, otherwise ``effective_date``. The two are never mixed into a
  synthesized date: several states publish only one of them and the semantics
  differ (see ``warn_sources/base.py``).
* **Absence is never reported as zero.** Some jurisdictions cannot be covered at
  all (AR/WY confidential by statute, NH publishes no list, MO/TX sit behind bot
  walls), and some state feeds publish notices without headcounts (HI, OK).
  Those show as "not covered" / "counts not reported", never as 0 layoffs and
  never as 0 employees.
* **Email-safe HTML**: inline styles only, table layout, no external CSS, JS,
  fonts or images, dark text on a light background (mail clients ignore
  ``prefers-color-scheme``), ~640px wide.

CLI:

    python3 warn_digest.py                      # previous complete month
    python3 warn_digest.py --year 2026 --month 6
    python3 warn_digest.py --month 6 --html /tmp/digest.html   # preview file
"""

import argparse
import calendar
import html
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import warn_urls

log = logging.getLogger("warn_digest")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
NATIONAL_FILE = DATA_DIR / "warn_national.json"

# The national dashboard is the site root. Sourced from warn_urls rather than
# hardcoded here — this constant used to be a second, independent copy of the
# same URL that warn_notify also held, and the two could drift apart unnoticed.
US_DASHBOARD_URL = warn_urls.US_DASHBOARD_URL

# How many rows each "top N" section shows. The per-state table is never
# truncated: every state with activity in the month is listed.
TOP_NOTICES = 10
TOP_EMPLOYERS = 10
TOP_MOVERS = 5

# Jurisdictions the platform cannot report on, and why. Naming them in every
# digest is the point: a reader must never read a missing state as "no layoffs
# happened there". Reasons are sourced from EXPANSION_RESEARCH.md.
GAP_STATES = {
    "AR": "filings confidential by statute (Ark. Code § 11-10-314) — "
          "no public list exists",
    "WY": "filings confidential by statute (Wyo. Stat. § 9-2-2607) — "
          "no public list exists",
    "NH": "the state publishes no publicly available WARN list",
    "MO": "state feed sits behind a bot wall — not collected",
    "TX": "state feed sits behind a bot wall — historical records only, "
          "no current filings",
}

STATE_NAMES = {
    "AK": "Alaska", "AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DC": "District of Columbia", "DE": "Delaware", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "IA": "Iowa", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "MA": "Massachusetts", "MD": "Maryland", "ME": "Maine",
    "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri",
    "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina",
    "ND": "North Dakota", "NE": "Nebraska", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NV": "Nevada", "NY": "New York",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VA": "Virginia",
    "VT": "Vermont", "WA": "Washington", "WI": "Wisconsin",
    "WV": "West Virginia", "WY": "Wyoming",
}

# Palette — dark ink on light paper, hard-coded because email clients strip
# media queries. "Up" is red: more layoffs is not good news.
_INK = "#111827"
_MUTED = "#5b6472"
_LINE = "#e3e6ea"
_PAPER = "#ffffff"
_BG = "#f2f4f7"
_ACCENT = "#1d4ed8"
_UP = "#b42318"
_DOWN = "#047857"
_FONT = "Arial, Helvetica, sans-serif"


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def month_bounds(year: int, month: int) -> tuple:
    """Inclusive ``(first_day, last_day)`` of a calendar month."""
    if not 1 <= int(month) <= 12:
        raise ValueError(f"month must be 1-12, got {month!r}")
    year, month = int(year), int(month)
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def previous_month(year: int, month: int) -> tuple:
    """The ``(year, month)`` immediately before the given one."""
    year, month = int(year), int(month)
    return (year - 1, 12) if month == 1 else (year, month - 1)


def same_month_last_year(year: int, month: int) -> tuple:
    """The ``(year, month)`` twelve months earlier."""
    return int(year) - 1, int(month)


def previous_complete_month(today: Optional[date] = None) -> tuple:
    """The most recent month that has fully elapsed as of ``today``."""
    today = today or date.today()
    return previous_month(today.year, today.month)


def period_label(year: int, month: int) -> str:
    """Human month label, e.g. ``"June 2026"``."""
    return f"{calendar.month_name[int(month)]} {int(year)}"


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


def parse_date(value) -> Optional[date]:
    """Best-effort date parse. Returns None for blanks and unparseable text."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def event_date(record: dict) -> tuple:
    """``(date, field_used)`` for a record — notice date, else effective date.

    Never synthesizes one from the other: a state that publishes only an
    effective date is bucketed by that real published date, and a record with
    neither is excluded from every month (returns ``(None, None)``).
    """
    for field in ("notice_date", "effective_date"):
        parsed = parse_date(record.get(field))
        if parsed is not None:
            return parsed, field
    return None, None


def employee_count(value) -> Optional[int]:
    """Reported headcount, or None when the feed publishes none.

    Feeds without a headcount column (HI, OK) emit ``0`` for every notice, so a
    non-positive value means "not reported", not "zero people affected". A real
    WARN filing never affects zero workers.
    """
    if value is None or value == "":
        return None
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def pct_change(current, previous) -> Optional[float]:
    """Percent change vs a baseline; None when there is no baseline to divide by.

    Returns None for a zero (or missing) baseline rather than infinity — callers
    render that as "no baseline" / "new activity".
    """
    if previous in (None, 0):
        return None
    return (float(current) - float(previous)) / float(previous) * 100.0


def _state_name(code: str) -> str:
    return STATE_NAMES.get(code, code)


def _norm_company(name: str) -> str:
    """Grouping key for employer totals: case/whitespace/punctuation folded."""
    text = re.sub(r"\s+", " ", str(name or "")).strip().upper()
    return text.rstrip(" .,")


# ---------------------------------------------------------------------------
# Data loading + aggregation
# ---------------------------------------------------------------------------


def load_payload(national_file: Optional[Path] = None) -> dict:
    """Read the national dataset. Raises if it is missing or unreadable."""
    path = Path(national_file) if national_file else NATIONAL_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"National dataset not found at {path} — run the pipeline first."
        )
    payload = json.loads(path.read_text())
    if isinstance(payload, list):  # tolerate a bare record list
        payload = {"records": payload, "states": {}}
    return payload


def _tracked_codes(payload: dict, records: list) -> set:
    """Every state the platform has data for (so silence can be told apart)."""
    codes = {str(c).upper() for c in (payload.get("states") or {}).keys()}
    codes |= {str(r.get("state") or "").upper() for r in records}
    codes.discard("")
    return codes


def _bucket(records: list, targets: dict) -> dict:
    """One pass over the dataset → {bucket_name: [records in that month]}."""
    buckets = {name: [] for name in targets}
    wanted = {(y, m): name for name, (y, m) in targets.items()}
    for rec in records:
        when, _field = event_date(rec)
        if when is None:
            continue
        name = wanted.get((when.year, when.month))
        if name is not None:
            buckets[name].append(rec)
    return buckets


def _totals(records: list) -> dict:
    """Notice/headcount totals for a bucket of records."""
    counted = [employee_count(r.get("employees")) for r in records]
    reported = [c for c in counted if c is not None]
    return {
        "notices": len(records),
        "employees": int(sum(reported)),
        "notices_with_counts": len(reported),
        "notices_without_counts": len(records) - len(reported),
        "counts_reported": bool(reported),
    }


def _by_state(records: list) -> dict:
    """{state_code: totals} for a bucket of records."""
    grouped: dict = {}
    for rec in records:
        code = str(rec.get("state") or "").upper() or "??"
        grouped.setdefault(code, []).append(rec)
    return {code: _totals(rows) for code, rows in grouped.items()}


def _state_rows(current: dict, prior: dict) -> list:
    """Per-state table rows for every state with activity this month."""
    rows = []
    for code, cur in current.items():
        prev = prior.get(code, {})
        prev_emp = int(prev.get("employees", 0) or 0)
        prev_notices = int(prev.get("notices", 0) or 0)
        rows.append(
            {
                "code": code,
                "name": _state_name(code),
                "notices": cur["notices"],
                "employees": cur["employees"],
                "counts_reported": cur["counts_reported"],
                "notices_with_counts": cur["notices_with_counts"],
                "partial_counts": (
                    0 < cur["notices_with_counts"] < cur["notices"]
                ),
                "prev_notices": prev_notices,
                "prev_employees": prev_emp,
                "prev_counts_reported": bool(prev.get("counts_reported")),
                "notices_delta": cur["notices"] - prev_notices,
                "employees_delta": cur["employees"] - prev_emp,
                "notices_delta_pct": pct_change(cur["notices"], prev_notices),
                "employees_delta_pct": pct_change(cur["employees"], prev_emp),
                "coverage_note": GAP_STATES.get(code, ""),
            }
        )
    rows.sort(key=lambda r: (-r["employees"], -r["notices"], r["code"]))
    return rows


def _movers(rows: list, prior: dict) -> tuple:
    """(up, down) — biggest headcount swings vs the prior month.

    Only states whose headcounts are usable in *both* months qualify: a state
    that publishes no headcounts has no measurable swing, and inventing one
    would be fabrication. States that were active last month and went silent
    this month are included as declines.
    """
    candidates = []
    for row in rows:
        if not row["counts_reported"]:
            continue  # this month publishes no headcounts here
        if row["prev_notices"] > 0 and not row["prev_counts_reported"]:
            continue  # last month published none either — no usable baseline
        candidates.append(dict(row))

    active = {row["code"] for row in rows}
    for code, prev in prior.items():
        if code in active:
            continue  # already handled above
        prev_emp = int(prev.get("employees", 0) or 0)
        if prev_emp <= 0:
            continue
        candidates.append(
            {
                "code": code,
                "name": _state_name(code),
                "notices": 0,
                "employees": 0,
                "counts_reported": False,
                "prev_notices": int(prev.get("notices", 0) or 0),
                "prev_employees": prev_emp,
                "employees_delta": -prev_emp,
                "employees_delta_pct": pct_change(0, prev_emp),
                "coverage_note": GAP_STATES.get(code, ""),
            }
        )
    up = sorted(
        (c for c in candidates if c["employees_delta"] > 0),
        key=lambda c: -c["employees_delta"],
    )[:TOP_MOVERS]
    down = sorted(
        (c for c in candidates if c["employees_delta"] < 0),
        key=lambda c: c["employees_delta"],
    )[:TOP_MOVERS]
    return up, down


def _largest_notices(records: list) -> list:
    """The month's biggest single filings (only those that report a headcount)."""
    sized = []
    for rec in records:
        count = employee_count(rec.get("employees"))
        if count is None:
            continue
        when, field = event_date(rec)
        sized.append(
            {
                "company": str(rec.get("company") or "Unnamed employer"),
                "state": str(rec.get("state") or "").upper(),
                "employees": count,
                "city": str(rec.get("city") or ""),
                "county": str(rec.get("county") or ""),
                "date": when.isoformat() if when else "",
                "date_field": field or "",
            }
        )
    sized.sort(key=lambda r: (-r["employees"], r["company"]))
    return sized[:TOP_NOTICES]


def _top_employers(records: list) -> list:
    """Employers with the largest total headcount across the month's filings."""
    grouped: dict = {}
    for rec in records:
        count = employee_count(rec.get("employees"))
        if count is None:
            continue
        name = str(rec.get("company") or "Unnamed employer")
        entry = grouped.setdefault(
            _norm_company(name),
            {"company": name, "employees": 0, "notices": 0, "states": []},
        )
        entry["employees"] += count
        entry["notices"] += 1
        code = str(rec.get("state") or "").upper()
        if code and code not in entry["states"]:
            entry["states"].append(code)
    ranked = sorted(
        grouped.values(), key=lambda e: (-e["employees"], e["company"])
    )
    for entry in ranked:
        entry["states"].sort()
    return ranked[:TOP_EMPLOYERS]


# ---------------------------------------------------------------------------
# Digest assembly
# ---------------------------------------------------------------------------


def build_stats(
    year: int, month: int, national_file: Optional[Path] = None
) -> dict:
    """Every number the digest reports, for one calendar month."""
    year, month = int(year), int(month)
    start, end = month_bounds(year, month)
    prev_y, prev_m = previous_month(year, month)
    ly_y, ly_m = same_month_last_year(year, month)

    payload = load_payload(national_file)
    records = payload.get("records") or []

    buckets = _bucket(
        records,
        {
            "current": (year, month),
            "prior": (prev_y, prev_m),
            "last_year": (ly_y, ly_m),
        },
    )
    current, prior, last_year = (
        buckets["current"], buckets["prior"], buckets["last_year"]
    )

    totals = _totals(current)
    prior_totals = _totals(prior)
    ly_totals = _totals(last_year)

    cur_states = _by_state(current)
    prior_states = _by_state(prior)
    rows = _state_rows(cur_states, prior_states)
    movers_up, movers_down = _movers(rows, prior_states)

    tracked = _tracked_codes(payload, records)
    active = set(cur_states)
    quiet = sorted(c for c in tracked - active if c not in GAP_STATES)
    gaps = [
        {"code": code, "name": _state_name(code), "reason": reason}
        for code, reason in sorted(GAP_STATES.items())
        if code not in active
    ]
    gap_with_data = [
        {
            "code": code,
            "name": _state_name(code),
            "reason": GAP_STATES[code],
        }
        for code in sorted(active & set(GAP_STATES))
    ]

    return {
        "year": year,
        "month": month,
        "period_label": period_label(year, month),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "empty": totals["notices"] == 0,
        "notices": totals["notices"],
        "employees": totals["employees"],
        "notices_with_counts": totals["notices_with_counts"],
        "notices_without_counts": totals["notices_without_counts"],
        "counts_reported": totals["counts_reported"],
        "states_with_activity": len(cur_states),
        "prior": {
            "label": period_label(prev_y, prev_m),
            "year": prev_y,
            "month": prev_m,
            "notices": prior_totals["notices"],
            "employees": prior_totals["employees"],
        },
        "last_year": {
            "label": period_label(ly_y, ly_m),
            "year": ly_y,
            "month": ly_m,
            "notices": ly_totals["notices"],
            "employees": ly_totals["employees"],
        },
        "delta": {
            "notices_mom": totals["notices"] - prior_totals["notices"],
            "employees_mom": totals["employees"] - prior_totals["employees"],
            "notices_mom_pct": pct_change(
                totals["notices"], prior_totals["notices"]
            ),
            "employees_mom_pct": pct_change(
                totals["employees"], prior_totals["employees"]
            ),
            "notices_yoy": totals["notices"] - ly_totals["notices"],
            "employees_yoy": totals["employees"] - ly_totals["employees"],
            "notices_yoy_pct": pct_change(
                totals["notices"], ly_totals["notices"]
            ),
            "employees_yoy_pct": pct_change(
                totals["employees"], ly_totals["employees"]
            ),
        },
        "states": rows,
        "movers_up": movers_up,
        "movers_down": movers_down,
        "largest_notices": _largest_notices(current),
        "top_employers": _top_employers(current),
        "quiet_states": quiet,
        "gap_states": gaps,
        "gap_states_with_history": gap_with_data,
        "tracked_states": len(tracked),
        "dataset_updated": payload.get("last_updated", ""),
        "dashboard_url": US_DASHBOARD_URL,
    }


# ---------------------------------------------------------------------------
# Formatting helpers (shared by HTML + text)
# ---------------------------------------------------------------------------


def _n(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _emp_text(row: dict) -> str:
    """Headcount cell for a state row — never a bare 0."""
    if not row.get("counts_reported"):
        return "counts not reported"
    if row.get("partial_counts"):
        return f"{_n(row['employees'])}*"
    return _n(row["employees"])


def _delta_text(current, previous, label, unit="") -> str:
    """'▲ 12.3% vs May 2026 (1,234)' — with an honest zero-baseline branch."""
    pct = pct_change(current, previous)
    prev_str = _n(previous)
    if pct is None:
        if not previous and not current:
            return f"no activity in {label} either"
        return f"no {label} baseline (0 → {_n(current)}{unit})"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "▬")
    return f"{arrow} {abs(pct):.1f}% vs {label} ({prev_str}{unit})"


def _delta_color(current, previous) -> str:
    pct = pct_change(current, previous)
    if pct is None or pct == 0:
        return _MUTED
    return _UP if pct > 0 else _DOWN


def _place(row: dict) -> str:
    parts = [p for p in (row.get("city"), row.get("county")) if p]
    return ", ".join(dict.fromkeys(parts))


def _gap_sentence(stats: dict) -> str:
    """The line that stops a missing state being read as zero layoffs."""
    gaps = stats["gap_states"]
    if not gaps:
        return (
            "Every jurisdiction the platform cannot normally cover "
            "(AR, WY, NH, MO, TX) carried data this month."
        )
    listed = "; ".join(f"{g['name']} ({g['code']}) — {g['reason']}" for g in gaps)
    return (
        f"States with no data this month — not zero layoffs, no data: {listed}."
    )


def _quiet_sentence(stats: dict) -> str:
    quiet = stats["quiet_states"]
    if not quiet:
        return ""
    names = ", ".join(f"{_state_name(c)} ({c})" for c in quiet)
    return (
        f"Tracked states that recorded no filings in {stats['period_label']}: "
        f"{names}."
    )


def build_subject(stats: dict) -> str:
    """Subject line for the digest email."""
    label = stats["period_label"]
    if stats["empty"]:
        return f"US WARN monthly digest — {label}: no notices recorded"
    people = (
        f", {_n(stats['employees'])} employees"
        if stats["counts_reported"]
        else ", headcounts not reported"
    )
    return (
        f"US WARN monthly digest — {label}: {_n(stats['notices'])} notices"
        f"{people} across {stats['states_with_activity']} states"
    )


# ---------------------------------------------------------------------------
# HTML rendering (inline styles only, table layout, light background)
# ---------------------------------------------------------------------------


def _h(value) -> str:
    text = "" if value is None else str(value)
    return html.escape(text) if text else "—"


_TD = f"padding:8px 10px;border-bottom:1px solid {_LINE};font-size:13px"
_TH = (
    f"padding:8px 10px;border-bottom:2px solid {_LINE};font-size:11px;"
    f"color:{_MUTED};text-transform:uppercase;letter-spacing:.06em;"
    "text-align:left"
)


def _section(title: str, body: str) -> str:
    return (
        '<tr><td style="padding:0 24px 22px">'
        f'<h2 style="margin:0 0 10px;font-size:15px;color:{_INK}">'
        f"{_h(title)}</h2>{body}</td></tr>"
    )


def _table(headers: list, rows: list) -> str:
    head = "".join(
        f'<th style="{_TH}{";text-align:right" if i else ""}">{_h(h)}</th>'
        for i, h in enumerate(headers)
    )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'role="presentation" style="border-collapse:collapse;color:{_INK}">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _cell(value, align="left", color=_INK, bold=False) -> str:
    weight = ";font-weight:700" if bold else ""
    return (
        f'<td style="{_TD};text-align:{align};color:{color}{weight}">'
        f"{value}</td>"
    )


def _stat_card(label: str, value: str, lines: list) -> str:
    subs = "".join(
        f'<div style="font-size:12px;color:{color};margin-top:3px">{_h(text)}'
        "</div>"
        for text, color in lines
    )
    return (
        f'<td width="50%" valign="top" style="padding:14px;background:{_BG};'
        f'border:1px solid {_LINE};border-radius:8px">'
        f'<div style="font-size:11px;color:{_MUTED};text-transform:uppercase;'
        f'letter-spacing:.07em">{_h(label)}</div>'
        f'<div style="font-size:28px;font-weight:700;color:{_INK};'
        f'margin-top:4px">{_h(value)}</div>{subs}</td>'
    )


def build_html(stats: dict) -> str:
    """Email-safe HTML digest: inline styles, tables, dark ink on light paper."""
    label = stats["period_label"]
    sections = []

    # -- headline stats -----------------------------------------------------
    if stats["empty"]:
        sections.append(
            _section(
                "No notices recorded",
                f'<p style="margin:0;font-size:14px;color:{_INK}">'
                f"No WARN notice anywhere in the tracked states carries an "
                f"event date in {_h(label)}. That is what the collected data "
                "shows — it is not a statement that no layoffs occurred."
                "</p>",
            )
        )
    else:
        emp_value = (
            _n(stats["employees"]) if stats["counts_reported"]
            else "not reported"
        )
        notice_lines = [
            (
                _delta_text(
                    stats["notices"], stats["prior"]["notices"],
                    stats["prior"]["label"],
                ),
                _delta_color(stats["notices"], stats["prior"]["notices"]),
            ),
            (
                _delta_text(
                    stats["notices"], stats["last_year"]["notices"],
                    stats["last_year"]["label"],
                ),
                _delta_color(stats["notices"], stats["last_year"]["notices"]),
            ),
        ]
        emp_lines = [
            (
                _delta_text(
                    stats["employees"], stats["prior"]["employees"],
                    stats["prior"]["label"],
                ),
                _delta_color(stats["employees"], stats["prior"]["employees"]),
            ),
            (
                _delta_text(
                    stats["employees"], stats["last_year"]["employees"],
                    stats["last_year"]["label"],
                ),
                _delta_color(
                    stats["employees"], stats["last_year"]["employees"]
                ),
            ),
        ]
        if stats["notices_without_counts"]:
            emp_lines.append(
                (
                    f"{_n(stats['notices_without_counts'])} of "
                    f"{_n(stats['notices'])} notices publish no headcount",
                    _MUTED,
                )
            )
        sections.append(
            '<tr><td style="padding:20px 24px 18px">'
            '<table width="100%" cellpadding="0" cellspacing="8" border="0" '
            'role="presentation"><tr>'
            + _stat_card("Notices filed", _n(stats["notices"]), notice_lines)
            + _stat_card("Employees affected", emp_value, emp_lines)
            + "</tr></table>"
            f'<p style="margin:10px 0 0;font-size:12px;color:{_MUTED}">'
            f"{_n(stats['states_with_activity'])} states recorded activity. "
            "A notice is counted in the month of its notice date, or its "
            "effective date when the state publishes no notice date."
            "</p></td></tr>"
        )

    # -- per-state table ----------------------------------------------------
    if stats["states"]:
        rows = []
        for row in stats["states"]:
            delta = row["employees_delta"]
            if not row["counts_reported"]:
                delta_txt, color = "—", _MUTED
            elif row["employees_delta_pct"] is None:
                delta_txt, color = f"+{_n(delta)} (new)", _UP if delta else _MUTED
            else:
                sign = "+" if delta > 0 else ""
                delta_txt = (
                    f"{sign}{_n(delta)} "
                    f"({row['employees_delta_pct']:+.0f}%)"
                )
                color = _UP if delta > 0 else (_DOWN if delta < 0 else _MUTED)
            note = (
                f'<div style="font-size:11px;color:{_MUTED}">'
                f"{_h(row['coverage_note'])}</div>"
                if row["coverage_note"] else ""
            )
            rows.append(
                "<tr>"
                + _cell(f"{_h(row['name'])} ({_h(row['code'])}){note}")
                + _cell(_n(row["notices"]), "right")
                + _cell(_emp_text(row), "right", bold=True)
                + _cell(_h(delta_txt), "right", color=color)
                + "</tr>"
            )
        footnote = ""
        if any(r["partial_counts"] for r in stats["states"]):
            footnote = (
                f'<p style="margin:8px 0 0;font-size:11px;color:{_MUTED}">'
                "* some notices in this state publish no headcount, so the "
                "total covers only those that do.</p>"
            )
        sections.append(
            _section(
                f"Every state with activity in {label}",
                _table(
                    ["State", "Notices", "Employees", "vs prior month"], rows
                ) + footnote,
            )
        )

    # -- movers -------------------------------------------------------------
    def _mover_rows(movers):
        out = []
        for m in movers:
            pct = m["employees_delta_pct"]
            pct_txt = "no prior baseline" if pct is None else f"{pct:+.0f}%"
            sign = "+" if m["employees_delta"] > 0 else ""
            color = _UP if m["employees_delta"] > 0 else _DOWN
            out.append(
                "<tr>"
                + _cell(f"{_h(m['name'])} ({_h(m['code'])})")
                + _cell(
                    f"{_n(m['prev_employees'])} → {_n(m['employees'])}", "right"
                )
                + _cell(
                    f"{sign}{_n(m['employees_delta'])} ({_h(pct_txt)})",
                    "right", color=color, bold=True,
                )
                + "</tr>"
            )
        return out

    if stats["movers_up"] or stats["movers_down"]:
        blocks = []
        if stats["movers_up"]:
            blocks.append(
                f'<p style="margin:0 0 6px;font-size:13px;color:{_INK}">'
                "<strong>Rising fastest</strong></p>"
                + _table(
                    ["State", "Employees (prior → now)", "Change"],
                    _mover_rows(stats["movers_up"]),
                )
            )
        if stats["movers_down"]:
            blocks.append(
                f'<p style="margin:14px 0 6px;font-size:13px;color:{_INK}">'
                "<strong>Falling fastest</strong></p>"
                + _table(
                    ["State", "Employees (prior → now)", "Change"],
                    _mover_rows(stats["movers_down"]),
                )
            )
        blocks.append(
            f'<p style="margin:8px 0 0;font-size:11px;color:{_MUTED}">'
            "Movers cover states whose feeds publish headcounts in both "
            "months; states without published counts have no measurable "
            "swing.</p>"
        )
        sections.append(
            _section(f"Biggest movers vs {stats['prior']['label']}",
                     "".join(blocks))
        )

    # -- largest notices ----------------------------------------------------
    if stats["largest_notices"]:
        rows = []
        for r in stats["largest_notices"]:
            where = _place(r)
            sub = (
                f'<div style="font-size:11px;color:{_MUTED}">{_h(where)}</div>'
                if where else ""
            )
            rows.append(
                "<tr>"
                + _cell(f"{_h(r['company'])}{sub}")
                + _cell(_h(r["state"]), "right")
                + _cell(_h(r["date"]), "right", color=_MUTED)
                + _cell(_n(r["employees"]), "right", bold=True)
                + "</tr>"
            )
        sections.append(
            _section(
                "Largest single notices",
                _table(["Employer", "State", "Date", "Employees"], rows),
            )
        )

    # -- top employers ------------------------------------------------------
    if stats["top_employers"]:
        rows = []
        for e in stats["top_employers"]:
            rows.append(
                "<tr>"
                + _cell(_h(e["company"]))
                + _cell(_h(", ".join(e["states"])), "right", color=_MUTED)
                + _cell(_n(e["notices"]), "right")
                + _cell(_n(e["employees"]), "right", bold=True)
                + "</tr>"
            )
        sections.append(
            _section(
                "Top employers by total employees",
                _table(["Employer", "States", "Notices", "Employees"], rows),
            )
        )

    # -- coverage -----------------------------------------------------------
    coverage = [
        f'<p style="margin:0 0 8px;font-size:13px;color:{_INK}">'
        f"{_h(_gap_sentence(stats))}</p>"
    ]
    quiet = _quiet_sentence(stats)
    if quiet:
        coverage.append(
            f'<p style="margin:0 0 8px;font-size:13px;color:{_MUTED}">'
            f"{_h(quiet)}</p>"
        )
    if stats["gap_states_with_history"]:
        listed = ", ".join(
            f"{g['name']} ({g['code']})"
            for g in stats["gap_states_with_history"]
        )
        coverage.append(
            f'<p style="margin:0 0 8px;font-size:13px;color:{_MUTED}">'
            f"{_h(listed)} appear above from archived records only — there is "
            "no live feed for them, so recent months will look empty.</p>"
        )
    coverage.append(
        f'<p style="margin:0;font-size:12px;color:{_MUTED}">'
        "Feeds that publish no headcount column are shown as "
        '"counts not reported" — never as zero employees.</p>'
    )
    sections.append(_section("Coverage caveats", "".join(coverage)))

    updated = stats.get("dataset_updated") or ""
    updated_line = (
        f"Dataset last updated {_h(updated[:19].replace('T', ' '))} UTC. "
        if updated else ""
    )

    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'role="presentation" style="margin:0;padding:0;background:{_BG}">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table width="640" cellpadding="0" cellspacing="0" border="0" '
        f'role="presentation" style="width:100%;max-width:640px;'
        f'background:{_PAPER};border:1px solid {_LINE};border-radius:12px;'
        f'font-family:{_FONT};color:{_INK}">'
        f'<tr><td style="padding:22px 24px;border-bottom:1px solid {_LINE}">'
        f'<div style="font-size:11px;letter-spacing:.09em;color:{_MUTED};'
        'text-transform:uppercase">Monthly digest</div>'
        f'<h1 style="margin:6px 0 0;font-size:22px;color:{_INK}">'
        f"U.S. WARN layoff notices — {_h(label)}</h1>"
        f'<div style="margin-top:4px;font-size:13px;color:{_MUTED}">'
        f"{_h(stats['period_start'])} to {_h(stats['period_end'])} · "
        f"{_n(stats['tracked_states'])} jurisdictions tracked</div>"
        "</td></tr>"
        + "".join(sections)
        + '<tr><td style="padding:0 24px 22px">'
        f'<a href="{US_DASHBOARD_URL}" style="display:inline-block;'
        f'background:{_ACCENT};color:#ffffff;text-decoration:none;'
        'padding:11px 22px;border-radius:8px;font-size:14px;font-weight:700">'
        "Explore the US dashboard</a></td></tr>"
        f'<tr><td style="padding:16px 24px;border-top:1px solid {_LINE};'
        f'font-size:11px;color:{_MUTED}">'
        f"{updated_line}Figures come from each state's own published WARN "
        "filings, aggregated at "
        f'<a href="{US_DASHBOARD_URL}" style="color:{_ACCENT}">'
        f"{US_DASHBOARD_URL}</a>. You are receiving this because you "
        "subscribed to the whole-US monthly digest. To unsubscribe, reply "
        'with "unsubscribe".'
        "</td></tr></table></td></tr></table>"
    )


# ---------------------------------------------------------------------------
# Plain-text rendering (stands on its own)
# ---------------------------------------------------------------------------


def build_text(stats: dict) -> str:
    """Plain-text digest — complete on its own, no HTML required."""
    label = stats["period_label"]
    out = [
        f"U.S. WARN LAYOFF NOTICES — {label.upper()}",
        "=" * 64,
        f"{stats['period_start']} to {stats['period_end']} · "
        f"{stats['tracked_states']} jurisdictions tracked",
        "",
    ]

    if stats["empty"]:
        out += [
            f"No WARN notice anywhere in the tracked states carries an event "
            f"date in {label}.",
            "That is what the collected data shows — it is not a statement "
            "that no layoffs occurred.",
            "",
        ]
    else:
        emp = (
            _n(stats["employees"]) if stats["counts_reported"]
            else "not reported"
        )
        out += [
            f"Notices filed:      {_n(stats['notices'])}",
            "  " + _delta_text(
                stats["notices"], stats["prior"]["notices"],
                stats["prior"]["label"],
            ),
            "  " + _delta_text(
                stats["notices"], stats["last_year"]["notices"],
                stats["last_year"]["label"],
            ),
            f"Employees affected: {emp}",
            "  " + _delta_text(
                stats["employees"], stats["prior"]["employees"],
                stats["prior"]["label"],
            ),
            "  " + _delta_text(
                stats["employees"], stats["last_year"]["employees"],
                stats["last_year"]["label"],
            ),
            f"States with activity: {stats['states_with_activity']}",
            "",
        ]
        if stats["notices_without_counts"]:
            out += [
                f"{_n(stats['notices_without_counts'])} of "
                f"{_n(stats['notices'])} notices publish no headcount; the "
                "employee total covers only those that do.",
                "",
            ]

    if stats["states"]:
        out += [f"EVERY STATE WITH ACTIVITY IN {label.upper()}", "-" * 64]
        out.append(
            f"{'State':22} {'Notices':>8} {'Employees':>20} {'vs prior':>12}"
        )
        for row in stats["states"]:
            if not row["counts_reported"]:
                delta_txt = "—"
            elif row["employees_delta_pct"] is None:
                delta_txt = f"+{_n(row['employees_delta'])}"
            else:
                sign = "+" if row["employees_delta"] > 0 else ""
                delta_txt = (
                    f"{sign}{_n(row['employees_delta'])} "
                    f"({row['employees_delta_pct']:+.0f}%)"
                )
            name = f"{row['name']} ({row['code']})"[:22]
            out.append(
                f"{name:22} {_n(row['notices']):>8} "
                f"{_emp_text(row):>20} {delta_txt:>12}"
            )
        if any(r["partial_counts"] for r in stats["states"]):
            out.append(
                "* some notices in this state publish no headcount; the total "
                "covers only those that do."
            )
        out.append("")

    def _mover_lines(movers):
        lines = []
        for m in movers:
            pct = m["employees_delta_pct"]
            pct_txt = "no prior baseline" if pct is None else f"{pct:+.0f}%"
            sign = "+" if m["employees_delta"] > 0 else ""
            lines.append(
                f"  {m['name']} ({m['code']}): {_n(m['prev_employees'])} -> "
                f"{_n(m['employees'])} employees "
                f"[{sign}{_n(m['employees_delta'])}, {pct_txt}]"
            )
        return lines

    if stats["movers_up"] or stats["movers_down"]:
        out += [f"BIGGEST MOVERS VS {stats['prior']['label'].upper()}", "-" * 64]
        if stats["movers_up"]:
            out.append("Rising fastest:")
            out += _mover_lines(stats["movers_up"])
        if stats["movers_down"]:
            out.append("Falling fastest:")
            out += _mover_lines(stats["movers_down"])
        out += [
            "Movers cover states whose feeds publish headcounts in both "
            "months.",
            "",
        ]

    if stats["largest_notices"]:
        out += ["LARGEST SINGLE NOTICES", "-" * 64]
        for r in stats["largest_notices"]:
            where = _place(r)
            where = f" — {where}" if where else ""
            out.append(
                f"  {_n(r['employees']):>7}  {r['company']} ({r['state']})"
                f"{where} — {r['date']}"
            )
        out.append("")

    if stats["top_employers"]:
        out += ["TOP EMPLOYERS BY TOTAL EMPLOYEES", "-" * 64]
        for e in stats["top_employers"]:
            states = ", ".join(e["states"])
            plural = "s" if e["notices"] != 1 else ""
            out.append(
                f"  {_n(e['employees']):>7}  {e['company']} "
                f"[{states}] — {e['notices']} notice{plural}"
            )
        out.append("")

    out += ["COVERAGE CAVEATS", "-" * 64, _gap_sentence(stats)]
    quiet = _quiet_sentence(stats)
    if quiet:
        out.append(quiet)
    if stats["gap_states_with_history"]:
        listed = ", ".join(
            f"{g['name']} ({g['code']})"
            for g in stats["gap_states_with_history"]
        )
        out.append(
            f"{listed} appear above from archived records only — there is no "
            "live feed for them, so recent months will look empty."
        )
    out += [
        'Feeds that publish no headcount column are shown as "counts not '
        'reported" — never as zero employees.',
        "",
        f"Explore the US dashboard: {US_DASHBOARD_URL}",
        "",
        "You are receiving this because you subscribed to the whole-US "
        'monthly digest. To unsubscribe, reply with "unsubscribe".',
    ]
    return "\n".join(out)


def build_monthly_digest(
    year: int, month: int, national_file: Optional[Path] = None
) -> dict:
    """Assemble the whole-US monthly digest for one calendar month.

    Returns ``{"year", "month", "period_label", "html", "text", "subject",
    "stats"}``. An empty month yields a valid digest that says so.
    """
    stats = build_stats(year, month, national_file)
    return {
        "year": stats["year"],
        "month": stats["month"],
        "period_label": stats["period_label"],
        "subject": build_subject(stats),
        "html": build_html(stats),
        "text": build_text(stats),
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    default_year, default_month = previous_complete_month()
    parser = argparse.ArgumentParser(
        description="Build the whole-US monthly WARN digest."
    )
    parser.add_argument("--year", type=int, default=default_year)
    parser.add_argument("--month", type=int, default=default_month)
    parser.add_argument(
        "--national", type=Path, default=None,
        help="path to warn_national.json (default: data/warn_national.json)",
    )
    parser.add_argument(
        "--html", type=Path, default=None,
        help="write the HTML version to this file for preview",
    )
    parser.add_argument(
        "--stats", action="store_true", help="also print the stats dict as JSON"
    )
    args = parser.parse_args(argv)

    digest = build_monthly_digest(args.year, args.month, args.national)
    print(f"Subject: {digest['subject']}")
    print()
    print(digest["text"])
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(digest["html"])
        print(f"\n[HTML written to {args.html}]")
    if args.stats:
        print()
        print(json.dumps(digest["stats"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    raise SystemExit(main())
