"""Tests for the Connecticut WARN source (warn_sources/ct.py)."""

import re
from pathlib import Path

import numpy as np

import warn_sources
from warn_sources.ct import COLUMNS, ConnecticutDOL, _transform_date, _transform_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "ct_api_sample.json"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_ct():
    assert "ct" in warn_sources.SOURCES
    src = warn_sources.get_source("ct")
    assert isinstance(src, ConnecticutDOL)
    assert src.code == "ct"
    assert src.name == "Connecticut"
    assert src.paths.root.name == "ct"


# ---------------------------------------------------------------------------
# Offline parse against a real API sample (tests/fixtures/ct_api_sample.json)
# ---------------------------------------------------------------------------


def test_parse_fixture_schema_and_types(tmp_path):
    df = ConnecticutDOL(tmp_path).parse(FIXTURE)

    assert list(df.columns) == COLUMNS
    # 9 blobItems in the fixture, one junk row (blank company) dropped
    assert len(df) == 8
    assert df["company"].str.len().gt(0).all()

    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO_DATE.match(val), f"{col}: {val!r}"

    assert all(isinstance(v, (int, np.integer)) for v in df["employees"])


def test_parse_fixture_field_mapping(tmp_path):
    df = ConnecticutDOL(tmp_path).parse(FIXTURE)
    rows = {r["company"]: r for r in df.to_dict("records")}

    # Clean row: dates map from warn_document_date / layoff_dates
    conduent = rows["Conduent Commercial Solutions, LLC"]
    assert conduent["notice_date"] == "2026-06-26"
    assert conduent["effective_date"] == "2026-08-28"
    assert conduent["employees"] == 2
    assert conduent["city"] == "Remote"

    # Date range resolves to its start date (BLN first-token rule)
    guida = rows["Guida-Seibert Dairy Company"]
    assert guida["effective_date"] == "2026-07-20"
    assert guida["employees"] == 205
    assert guida["city"] == "New Britain"

    # Spelled-out date handled via the vendored corrections table
    simply = rows["SimplyIOA, LLC"]
    assert simply["effective_date"] == "2025-12-02"
    # "66; #CT workers not indicated" -> no CT count published -> 0
    assert simply["employees"] == 0

    # "%m-%d-%Y" range variant; "Not provided" head-count -> 0
    posigen = rows["PosiGen Developer LLC"]
    assert posigen["employees"] in (0, 78)  # two PosiGen filings in fixture
    assert df[df["company"] == "PosiGen Developer LLC"].shape[0] == 2


def test_transform_date_quirks():
    assert _transform_date("8/28/2026") == "2026-08-28"
    assert _transform_date("2026-06-26") == "2026-06-26"
    assert _transform_date("11-23-2025 - 12-6-2025") == "2025-11-23"
    assert _transform_date("August 24, 2024") == "2024-08-24"  # correction
    assert _transform_date("N/A") is None
    assert _transform_date("") is None
    assert _transform_date(None) is None
    assert _transform_date("Not Indicated") is None
    # Unknown garbage must degrade to None, never a raw string
    assert _transform_date("sometime next year, probably") is None
    # Below the minimum plausible year -> corrections/None, never trusted
    assert _transform_date("0025-05-16") == "2025-05-16"


def test_transform_jobs_quirks():
    assert _transform_jobs("205") == 205
    assert _transform_jobs("113,") == 113          # correction
    assert _transform_jobs("Not provided") == 0
    assert _transform_jobs("Not indicated") == 0
    assert _transform_jobs("66; #CT workers not indicated") == 0
    assert _transform_jobs(None) == 0
    assert _transform_jobs("") == 0
    assert _transform_jobs("999999") == 0          # over BLN sanity cap
