"""Tests for the Montana (MT) WARN source.

The fixture ``tests/fixtures/mt_sample.xlsx`` is a 14-row subset of the real
DLI feed (fetched 2026-07-20) preserving the blank spacer rows and every
quirk cell the BLN corrections tables cover.
"""

import re
from pathlib import Path

import warn_sources
from warn_sources.mt import MontanaDLI

FIXTURE = Path(__file__).parent / "fixtures" / "mt_sample.xlsx"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parsed(tmp_path):
    return MontanaDLI(tmp_path).parse(FIXTURE)


def test_mt_registered():
    assert "mt" in warn_sources.SOURCES
    assert warn_sources.SOURCES["mt"] is MontanaDLI
    assert MontanaDLI.enabled


def test_parse_columns_and_row_count(tmp_path):
    df = _parsed(tmp_path)
    assert list(df.columns) == [
        "company", "notice_date", "effective_date",
        "employees", "county", "industry",
    ]
    # 14 fixture rows minus header and two blank spacers.
    assert len(df) == 11


def test_parse_dates_are_iso_or_none(tmp_path):
    df = _parsed(tmp_path)
    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO_RE.match(val), (col, val)


def test_parse_employees_are_int(tmp_path):
    df = _parsed(tmp_path)
    assert all(isinstance(v, int) or hasattr(v, "item") for v in df["employees"])
    assert str(df["employees"].dtype).startswith("int")


def test_parse_clean_row(tmp_path):
    df = _parsed(tmp_path)
    row = df[df["company"] == "Wells Fargo & Company"].iloc[0]
    assert row["notice_date"] == "2026-03-30"
    assert row["effective_date"] == "2026-05-30"
    assert row["employees"] == 77
    assert row["county"] == "Yellowstone"
    assert row["industry"] == "Banking"


def test_parse_honors_bln_date_corrections(tmp_path):
    df = _parsed(tmp_path).set_index("company")
    # Multi-valued impact date -> first date.
    assert (
        df.loc["Gary & Leo's Fresh Foods", "effective_date"] == "2025-05-22"
    )
    # Typo'd years fixed per the corrections table.
    assert df.loc["Pacific Source Health", "effective_date"] == "2025-12-31"
    assert (
        df.loc["Exxon Mobile/Denbury Onshore LLC", "notice_date"]
        == "2025-11-17"
    )
    # Range collapses to its start; free text becomes None.
    assert df.loc["Golden Entertainment", "effective_date"] == "2020-03-16"
    assert df.loc["ION Nutritional Labs", "effective_date"] is None


def test_parse_honors_bln_jobs_corrections(tmp_path):
    df = _parsed(tmp_path).set_index("company")
    assert df.loc["Sidney Sugars", "employees"] == 1          # "up to 300"
    assert df.loc["American Nursery Services", "employees"] == 100  # "Over 100"
    assert df.loc["Sanjel", "employees"] == 0                 # "Not noted"
    assert df.loc["Fidelity Exploration & Prod.", "employees"] == 0
