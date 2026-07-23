"""Tests for the New Jersey WARN source (warn_sources/nj.py).

The fixture tests/fixtures/nj_sample.xlsx is a 2-sheet, 15-row excerpt of
real rows fetched from the state's WARN_Notice_Archive.xlsx on 2026-07-20,
chosen to exercise every feed quirk: datetime vs string dates, multi-date
lists with BLN corrections, "TBA"/"-" placeholders, asterisked and
multi-county employee counts, \xa0 padding, blank and footnote rows.
"""

import re
from pathlib import Path

import pandas as pd

import warn_sources
from warn_sources.nj import NewJerseyDOL, _effective_date, _employees

FIXTURE = Path(__file__).parent / "fixtures" / "nj_sample.xlsx"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_registry_contains_nj():
    assert "nj" in warn_sources.SOURCES
    assert warn_sources.SOURCES["nj"] is NewJerseyDOL
    src = warn_sources.get_source("nj")
    assert src.code == "nj"
    assert src.name == "New Jersey"
    assert src.enabled


def test_parse_fixture(tmp_path):
    src = warn_sources.get_source("nj", tmp_path)
    df = src.parse(FIXTURE)

    # Only the columns NJ really publishes (no notice date in this feed).
    assert list(df.columns) == ["company", "city", "effective_date", "employees"]

    # 13 data rows minus the blank row and the 2009 footnote row.
    assert len(df) == 11
    assert "Walmart" in set(df["company"])
    assert not any(c.startswith("*") for c in df["company"])
    assert not any(c.lower() == "company" for c in df["company"])

    # Dates are ISO strings or missing — never raw feed strings.
    for val in df["effective_date"]:
        assert pd.isna(val) or ISO_DATE.match(val), val

    # Employees are plain ints, 0 when the state published no count.
    assert df["employees"].map(lambda v: isinstance(v, int)).all()

    rows = {r["company"]: r for r in df.to_dict("records")}
    # Real datetime cell.
    assert rows["Walmart"]["effective_date"] == "2025-06-13"
    # BLN hand-audited multi-date correction.
    assert rows["Bristol Myers Squibb (BMS)"]["effective_date"] == "2024-04-25"
    # Prose date -> first date token.
    assert rows["Rite Aid"]["effective_date"] == "2025-06-04"
    # "TBA" effective date -> missing.
    assert pd.isna(rows["BROOKS BROTHERS CUST SVC"]["effective_date"])
    # \xa0-padded string cells are cleaned ("3/4/07 ", "102 ").
    assert rows["JOHNSON CONTROLS"]["effective_date"] == "2007-03-04"
    assert rows["JOHNSON CONTROLS"]["employees"] == 102
    # Multi-county breakdown summed per BLN crosswalk.
    assert rows["Amazon"]["employees"] == 871
    # Asterisked count and placeholder counts.
    assert rows["ASHBROOK NURSING HOME - ST BARNABAS"]["employees"] == 149
    assert rows["METAL TEXTILES CORPORATION"]["employees"] == 0
    assert rows["UNIVERSAL FOLDING BOX"]["employees"] == 0


def test_unify_stamps_state_and_missing_fields(tmp_path):
    src = warn_sources.get_source("nj", tmp_path)
    df = src.unify(src.parse(FIXTURE))
    assert (df["state"] == "NJ").all()
    # NJ publishes no notice date — stays None, never synthesized.
    assert df["notice_date"].isna().all()
    for col in warn_sources.UNIFIED_FIELDS:
        assert col in df.columns


def test_effective_date_edge_cases():
    assert _effective_date(None) is None
    assert _effective_date("TBA") is None
    assert _effective_date("-") is None
    assert _effective_date("Temp layoff") is None
    assert _effective_date("6/13/25 - 8/22/25") == "2025-06-13"
    assert _effective_date("2/13/24,3/15/24") == "2024-02-13"
    assert _effective_date("9/6/204") == "2024-09-06"  # BLN typo correction
    assert _effective_date("02/06/2011") == "2011-02-06"


def test_employees_edge_cases():
    assert _employees(None) == 0
    assert _employees("TBA") == 0
    assert _employees(23695) == 0  # BLN: known feed error
    assert _employees(16000) == 16000  # BLN: legitimate United Airlines figure
    assert _employees("*1689") == 1689
    assert _employees(55) == 55
