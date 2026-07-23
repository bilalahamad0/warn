"""Tests for the Wisconsin WARN source (warn_sources/wi.py)."""

import re
from pathlib import Path

import numpy as np
import pandas as pd

import warn_sources
from warn_sources.wi import (
    COLUMNS,
    WisconsinDWD,
    _transform_company,
    _transform_date,
    _transform_jobs,
    _transform_notice_type,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wi_originals_sample.json"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_wi():
    assert "wi" in warn_sources.SOURCES
    src = warn_sources.get_source("wi")
    assert isinstance(src, WisconsinDWD)
    assert src.code == "wi"
    assert src.name == "Wisconsin"
    assert src.paths.root.name == "wi"


# ---------------------------------------------------------------------------
# Offline parse against a real feed sample
# (tests/fixtures/wi_originals_sample.json — 9 rows cut from the live
#  Google Sheets "Originals" feed, including a stray duplicated header row)
# ---------------------------------------------------------------------------


def test_parse_fixture_schema_and_types(tmp_path):
    df = WisconsinDWD(tmp_path).parse(FIXTURE)

    assert list(df.columns) == COLUMNS
    # 9 data rows in the fixture, the stray header row dropped
    assert len(df) == 8
    assert df["company"].str.len().gt(0).all()

    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert pd.isna(val) or ISO_DATE.match(val), f"{col}: {val!r}"

    assert all(isinstance(v, (int, np.integer)) for v in df["employees"])


def test_parse_fixture_field_mapping(tmp_path):
    df = WisconsinDWD(tmp_path).parse(FIXTURE)
    rows = {r["company"]: r for r in df.to_dict("records")}

    # Clean row: NoticeRcvd is %Y%m%d, LayoffBeginDate is %m/%d/%Y —
    # and they are never copied into each other.
    semco = rows["Semco Windows and Doors"]
    assert semco["notice_date"] == "2020-01-02"
    assert semco["effective_date"] == "2019-12-31"
    assert semco["employees"] == 141
    assert semco["layoff_type"] == "Facility Closure"
    assert semco["county"] == "Lincoln"
    assert semco["city"] == "Merrill"
    assert semco["industry"] == "Wood Window & Door Mfg."

    # Company cell carrying an embedded HTML footnote is cut at the tag;
    # "Unknown" worker count -> 0 (no count published).
    uscc = rows["United States Cellular Corporation"]
    assert uscc["notice_date"] == "2025-07-09"
    assert uscc["effective_date"] == "2025-06-02"
    assert uscc["employees"] == 0
    assert uscc["county"] == "Multiple"

    # "Unknown" layoff date -> None (BLN correction), never a raw string.
    sparhawk = rows[
        "Sparhawk Trucking Inc. / Sparhawk Truck and Trailer Inc."
    ]
    assert pd.isna(sparhawk["effective_date"])
    assert sparhawk["notice_date"] == "2026-05-29"
    assert sparhawk["layoff_type"] == ""  # NoticeType "Unknown"

    # "TBD" worker count -> 0.
    ahlstrom = rows["Ahlstrom Mosinee LLC"]
    assert ahlstrom["employees"] == 0
    assert ahlstrom["effective_date"] == "2026-06-30"

    # Two-digit-year layoff date ("06/02/23") -> %m/%d/%y.
    puris = rows["Puris Proteins LLC"]
    assert puris["effective_date"] == "2023-06-02"

    # Combined notice-type codes decode via the page legend.
    briggs = rows["Briggs & Stratton, LLC"]
    assert briggs["layoff_type"] == "Facility Closure, Workforce Reduction"
    assert briggs["county"] == "Washington, Milwaukee"

    # HTML entities in company/industry are unescaped.
    holiday = rows["Holiday Inn & Suites Wausau Rothschild"]
    assert holiday["industry"] == "Hotels & Motels exc Casino Hotels"


# ---------------------------------------------------------------------------
# Transform quirks (vendored BLN corrections + WI-specific cleanup)
# ---------------------------------------------------------------------------


def test_transform_date_quirks():
    assert _transform_date("20260720") == "2026-07-20"
    assert _transform_date("12/31/2019") == "2019-12-31"
    assert _transform_date("06/02/23") == "2023-06-02"
    assert _transform_date("Unknown") is None      # BLN correction
    assert _transform_date("11/03") is None        # BLN correction
    assert _transform_date("") is None
    assert _transform_date(None) is None
    # Trailing junk after a full date is stripped (BLN _clean_text rule)
    assert _transform_date("4/1/2022 *") == "2022-04-01"
    # Unknown garbage must degrade to None, never a raw string
    assert _transform_date("sometime in fall") is None
    # Implausible years are not trusted
    assert _transform_date("1/1/1901") is None


def test_transform_jobs_quirks():
    assert _transform_jobs("141") == 141
    assert _transform_jobs("Unknown") == 0         # BLN correction
    assert _transform_jobs("TBD") == 0             # BLN correction
    assert _transform_jobs(None) == 0
    assert _transform_jobs("") == 0
    assert _transform_jobs("999999") == 0          # over BLN sanity cap


def test_transform_company_quirks():
    # BLN wi.py: cut "- Revision" suffixes
    assert _transform_company("Acme Corp - Revision 1") == "Acme Corp"
    # Embedded HTML footnote cut at the first tag
    assert (
        _transform_company(
            'United States Cellular Corporation<br/></a><a><em style="x">'
            "* DWD received this layoff notice after the revision.</em>"
        )
        == "United States Cellular Corporation"
    )
    assert _transform_company("Bed Bath &amp; Beyond Inc.") == (
        "Bed Bath & Beyond Inc."
    )


def test_transform_notice_type_legend():
    assert _transform_notice_type("CL") == "Facility Closure"
    assert _transform_notice_type("WR") == "Workforce Reduction"
    assert _transform_notice_type("CL, WR") == (
        "Facility Closure, Workforce Reduction"
    )
    assert _transform_notice_type("Unknown") == ""
    assert _transform_notice_type("") == ""
