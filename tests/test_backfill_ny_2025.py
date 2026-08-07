"""Tests for the NY dashboard-era 2025 gap backfill.

The capture script (scripts/backfill/ny_2025_gap.py) recovers the
2025-04-01 .. 2025-12-31 span that rolled off NY's current-year Tableau
export, writing the history file warn_sources.aggregate merges into the
national dataset. These tests drive its pure ``capture`` step with a
truncated real export fixture, and pin the committed history file to the
gap window so a re-run can never double-count the live store's records.
"""

import importlib.util
import json
import re
from pathlib import Path

from warn_sources.base import DATA_DIR
from warn_sources.ny import NewYorkDOL

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill" / "ny_2025_gap.py"
FIXTURE = Path(__file__).parent / "fixtures" / "ny_2025_gap_sample.csv"

_spec = importlib.util.spec_from_file_location("ny_2025_gap", SCRIPT)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Wiring: the source exposes the history file aggregate merges
# ---------------------------------------------------------------------------


def test_ny_declares_history_file():
    assert NewYorkDOL.history_file == DATA_DIR / "historical" / "ny_history.json"
    assert gap.OUTPUT_FILE.name == "ny_history.json"


def test_window_is_the_dashboard_era():
    # 2025-04-01 is NY's legacy->dashboard cutover; the live feed owns 2026+.
    assert gap.WINDOW_START == "2025-04-01"
    assert gap.WINDOW_END == "2025-12-31"


# ---------------------------------------------------------------------------
# capture() on a truncated real year-2025 export
# ---------------------------------------------------------------------------
#
# The fixture holds: a Jan 2025 row (legacy era), a Dec 2025 row, an
# identical Klēn Space pair (Tableau's flattened amendment history), two
# Rite Aid sites sharing a _record_key at different addresses, and a
# synthetic 2026 row (live-feed era).


def test_capture_keeps_only_gap_window_records():
    records, stats = gap.capture(FIXTURE, [])
    companies = [r["company"] for r in records]
    # Jan 2025 (legacy pages already cover it) and 2026 (live feed) are out.
    assert "57th Street Associates" not in companies
    assert "Future Feed Co." not in companies
    assert stats["outside_window"] == 2
    assert all(
        gap.WINDOW_START <= r["notice_date"] <= gap.WINDOW_END for r in records
    )


def test_capture_collapses_identical_rows_but_keeps_distinct_sites():
    records, stats = gap.capture(FIXTURE, [])
    # The Klēn pair differs only in Date Posted/Index (columns the parser
    # does not emit): one survives.
    assert [r["company"] for r in records].count("Klēn Space, Inc.") == 1
    assert stats["identical_dupes"] == 1
    # Same _record_key (company/county/date/employees) but different
    # addresses = genuinely distinct sites: both survive.
    rite_aid = [r for r in records if r["company"] == "Rite Aid"]
    assert len(rite_aid) == 2
    assert len({r["address"] for r in rite_aid}) == 2
    assert len(records) == 4  # Abbott House + Klēn + 2x Rite Aid


def test_capture_dedupes_against_live_store():
    store = [{"company": "Abbott House", "notice_date": "2025-12-26"}]
    records, stats = gap.capture(FIXTURE, store)
    assert "Abbott House" not in [r["company"] for r in records]
    assert stats["in_live_store"] == 1
    assert len(records) == 3


def test_captured_records_match_unified_schema():
    records, _ = gap.capture(FIXTURE, [])
    for r in records:
        assert r["state"] == "NY"
        assert r["company"]
        assert ISO_RE.match(r["notice_date"])
        assert r["effective_date"] is None or ISO_RE.match(r["effective_date"])
        assert isinstance(r["employees"], int)
        assert r["city"] == ""        # not published -> empty, not faked
        assert r["industry"] == ""
    # Deterministic output order: sorted by notice date first.
    dates = [r["notice_date"] for r in records]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# The committed history file honors the window and the live store
# ---------------------------------------------------------------------------


def test_history_file_stays_inside_gap_window_and_off_the_live_store():
    history = json.loads(gap.OUTPUT_FILE.read_text())["records"]
    assert history, "committed ny_history.json must not be empty"
    for r in history:
        assert gap.WINDOW_START <= r["notice_date"] <= gap.WINDOW_END
    # Every gap month is represented (the undercount the capture fixes).
    months = {r["notice_date"][:7] for r in history}
    assert months == {f"2025-{m:02d}" for m in range(4, 13)}
    # No textual twin of a live-store record — the merge cannot double count.
    store = json.loads(gap.STORE_FILE.read_text())["records"]
    store_keys = {gap._coarse_key(r) for r in store}
    assert not any(gap._coarse_key(r) in store_keys for r in history)
