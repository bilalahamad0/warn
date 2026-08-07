"""Tests for the Ohio 2023-2025 archive backfill.

The capture script (scripts/backfill/oh_2023_2025_gap.py) recovers the
three years that fell between the BLN historical snapshot (ends 2022)
and Ohio's current-year live feed, from the per-year archive CSVs on
JFS's rebuilt site, writing the history file warn_sources.aggregate
merges into the national dataset. These tests drive its pure ``capture``
step with a fixture of real archive rows (2023-header shape: padded junk
preamble, duplicated Company column) plus an engineered duplicate and an
out-of-window pair, and pin the committed history file to the window so
a re-run can never double-count the live store's records.
"""

import importlib.util
import json
import re
from pathlib import Path

from warn_sources.base import DATA_DIR
from warn_sources.oh import OhioJFS

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill" / "oh_2023_2025_gap.py"
FIXTURE = Path(__file__).parent / "fixtures" / "oh_archive_gap_sample.csv"

_spec = importlib.util.spec_from_file_location("oh_2023_2025_gap", SCRIPT)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Wiring: the source exposes the history file aggregate merges
# ---------------------------------------------------------------------------


def test_oh_declares_history_file():
    assert OhioJFS.history_file == DATA_DIR / "historical" / "oh_history.json"
    assert gap.OUTPUT_FILE.name == "oh_history.json"


def test_window_is_the_archive_era():
    # 2022 and earlier belongs to the BLN snapshot; the live feed owns 2026+.
    assert gap.WINDOW_START == "2023-01-01"
    assert gap.WINDOW_END == "2025-12-31"


# ---------------------------------------------------------------------------
# capture() on real archive rows
# ---------------------------------------------------------------------------
#
# The fixture holds (2023-archive shape, junk preamble padded with trailing
# commas, duplicated Company column): three real 2023 rows, an identical
# Zulily pair (engineered, re-export safety), the real UPDATE Daniel Drake
# row the state re-dated to its 2026 update receipt, and a synthetic 2022
# row (BLN-snapshot era).


def test_capture_keeps_only_window_records():
    records, stats = gap.capture([FIXTURE], [])
    companies = [r["company"] for r in records]
    # 2022 (BLN snapshot covers it) and the 2026-re-dated UPDATE are out.
    assert "Past Feed Co." not in companies
    assert "Daniel Drake Center for Post-Acute Care" not in companies
    assert stats["outside_window"] == 2
    assert all(
        gap.WINDOW_START <= r["notice_date"] <= gap.WINDOW_END for r in records
    )


def test_capture_collapses_identical_rows():
    records, stats = gap.capture([FIXTURE], [])
    # The Zulily pair is identical on _record_key (Ohio publishes no
    # address, so that key is the full row identity): one survives.
    assert [r["company"] for r in records].count("Zulily") == 1
    assert stats["identical_dupes"] == 1
    assert len(records) == 3  # syncreon + Zulily + FCA US LLC


def test_capture_dedupes_against_live_store():
    store = [{"company": "syncreon America, Inc", "notice_date": "2023-12-14"}]
    records, stats = gap.capture([FIXTURE], store)
    assert "syncreon America, Inc" not in [r["company"] for r in records]
    assert stats["in_live_store"] == 1
    assert len(records) == 2


def test_captured_records_match_unified_schema():
    records, _ = gap.capture([FIXTURE], [])
    for r in records:
        assert r["state"] == "OH"
        assert r["company"]
        assert ISO_RE.match(r["notice_date"])
        assert r["effective_date"] is None or ISO_RE.match(r["effective_date"])
        assert isinstance(r["employees"], int)
        assert r["address"] == ""     # not published -> empty, not faked
        assert r["industry"] == ""
    # Two-digit years and the City/County split survive the live parser.
    syncreon = next(
        r for r in records if r["company"] == "syncreon America, Inc"
    )
    assert syncreon["notice_date"] == "2023-12-14"
    assert syncreon["effective_date"] == "2024-02-05"
    assert (syncreon["city"], syncreon["county"]) == ("Toledo", "Lucas")
    # Deterministic output order: sorted by notice date first.
    dates = [r["notice_date"] for r in records]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# The committed history file honors the window and the live store
# ---------------------------------------------------------------------------


def test_history_file_stays_inside_window_and_off_the_live_store():
    history = json.loads(gap.OUTPUT_FILE.read_text())["records"]
    assert history, "committed oh_history.json must not be empty"
    for r in history:
        assert gap.WINDOW_START <= r["notice_date"] <= gap.WINDOW_END
    # Every window month is represented (the gap the capture fixes).
    months = {r["notice_date"][:7] for r in history}
    assert months == {
        f"{y}-{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13)
    }
    # No textual twin of a live-store record — the merge cannot double count.
    store = json.loads(gap.STORE_FILE.read_text())["records"]
    store_keys = {gap._coarse_key(r) for r in store}
    assert not any(gap._coarse_key(r) in store_keys for r in history)
