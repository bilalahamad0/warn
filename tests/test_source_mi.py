"""Tests for the Michigan WARN source (warn_sources/mi.py)."""

import re
from pathlib import Path

import numpy as np

import warn_sources
from warn_sources.mi import (
    COLUMNS,
    MichiganLEO,
    _transform_date,
    _transform_jobs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mi_search_sample.json"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_mi():
    assert "mi" in warn_sources.SOURCES
    src = warn_sources.get_source("mi")
    assert isinstance(src, MichiganLEO)
    assert src.code == "mi"
    assert src.name == "Michigan"
    assert src.paths.root.name == "mi"


# ---------------------------------------------------------------------------
# Offline parse against a real API sample (tests/fixtures/mi_search_sample.json,
# 10 entries captured live from the LEO search JSON on 2026-07-21; covers both
# fragment formats: new h3 + <li> bullets and older anchor + <p>/<br>)
# ---------------------------------------------------------------------------


def test_parse_fixture_schema_and_types(tmp_path):
    df = MichiganLEO(tmp_path).parse(FIXTURE)

    assert list(df.columns) == COLUMNS
    assert len(df) == 10
    assert df["company"].str.len().gt(0).all()

    # Michigan publishes no notice date and no industry — the columns must
    # not exist here (the shared unify() adds them as None/empty).
    assert "notice_date" not in df.columns
    assert "industry" not in df.columns

    for val in df["effective_date"]:
        assert val is None or ISO_DATE.match(val), repr(val)

    assert all(isinstance(v, (int, np.integer)) for v in df["employees"])


def test_parse_fixture_field_mapping(tmp_path):
    df = MichiganLEO(tmp_path).parse(FIXTURE)
    rows = {r["company"]: r for r in df.to_dict("records")}

    # New format: h3 company, nested multi-site address <ul>, range date.
    rec = rows["Rec Boat Holdings, LLC"]
    assert rec["effective_date"] == "2026-08-15"  # "8/15/2026 -- 12/31/2026"
    assert rec["employees"] == 239
    assert rec["layoff_type"] == "Facility closure"
    assert rec["county"] == "Wexford"
    assert rec["address"].count(";") == 6  # 7 sites joined with "; "

    # Older format: company in the title-link anchor, <p>/<br> fields,
    # two-digit year, ", Michigan" suffix stripped from the city.
    samaritas = rows["Samaritas"]
    assert samaritas["effective_date"] == "2026-03-31"  # "3/31/26"
    assert samaritas["employees"] == 58
    assert samaritas["city"] == "Grand Rapids"
    assert samaritas["county"] == "Kent"

    # Tight range with no spaces: "5/9/26-6/19/26" -> start date.
    siena = rows["Siena Heights University"]
    assert siena["effective_date"] == "2026-05-09"

    # Closure-date-only filing: date_close is NOT the effective date and
    # must never be copied into it (BLN maps effective_date <- date_start).
    c3 = rows["C3 Industries, Inc."]
    assert c3["effective_date"] is None
    assert c3["employees"] == 62

    # Thousands separator in the head-count.
    factory_zero = rows["Factory ZERO Detroit-Hamtramck Assembly Center"]
    assert factory_zero["employees"] == 1140

    # Free-text head-count: "1 remote Michigan worker" -> 1.
    simply = rows["SimplyIOA, LLC"]
    assert simply["employees"] == 1

    # Vendored BLN date corrections: "Commencing June 2025",
    # "Beginning April 21, 2025".
    lacroix = rows["LACROIX Electronics"]
    assert lacroix["effective_date"] == "2025-06-01"
    qmc = rows["Quality Metalcraft, Inc."]
    assert qmc["effective_date"] == "2025-04-21"


# ---------------------------------------------------------------------------
# Transform quirks
# ---------------------------------------------------------------------------


def test_transform_date_quirks():
    assert _transform_date("7/24/2026") == "2026-07-24"
    assert _transform_date("3/31/26") == "2026-03-31"
    assert _transform_date("December 2, 2025") == "2025-12-02"
    assert _transform_date("2/23/2026 -- 5/31/2026") == "2026-02-23"
    assert _transform_date("5/9/26-6/19/26") == "2026-05-09"
    assert _transform_date("7/1/2026 -- 11/2026") == "2026-07-01"
    # Multi-date list resolves to the first date.
    v = "12/5/25, 1/16/26, 2/26/26, and between 3/20/26 and 4/3/26"
    assert _transform_date(v) == "2025-12-05"
    # Qualifier words and month-only values.
    assert _transform_date("Beginning July 7, 2025") == "2025-07-07"
    assert _transform_date("Commencing June 2025") == "2025-06-01"
    assert _transform_date("June 30, 2024 (approximate)") == "2024-06-30"
    # Vendored correction: an impossible calendar date -> None.
    assert _transform_date("April 31, 2019") is None
    assert _transform_date("") is None
    assert _transform_date(None) is None
    # Unknown garbage must degrade to None, never a raw string.
    assert _transform_date("sometime next year, probably") is None
    assert _transform_date("12/31/1899") is None  # below minimum year


def test_transform_jobs_quirks():
    assert _transform_jobs("311") == 311
    assert _transform_jobs("1,140") == 1140
    assert _transform_jobs("1 remote Michigan worker") == 1
    assert _transform_jobs("12 remote workers") == 12
    # Vendored corrections: leading total wins over the breakdown; a
    # bare multi-site list is summed.
    assert _transform_jobs("138 (133 Zeeland, 5 Traverse City)") == 138
    assert _transform_jobs("163 204 130 191") == 688
    assert _transform_jobs("") == 0
    assert _transform_jobs(None) == 0
    assert _transform_jobs("Not reported") == 0
    assert _transform_jobs("999999") == 0  # over BLN sanity cap
