"""Tests for the Alabama (AL) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import al as al_module

FIXTURE = Path(__file__).parent / "fixtures" / "al_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_alabama():
    assert "al" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["al"]
    assert cls.code == "al"
    assert cls.name == "Alabama"
    assert cls.source_url.startswith("https://workforce.alabama.gov")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("al", tmp_path)
    assert src.paths.root == tmp_path / "states" / "al"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("al", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]


def test_parse_drops_junk_rows_and_requires_company(parsed):
    # Fixture has 12 data lines: 10 real notices, one repeated header
    # line, one blank-company row — only the real notices survive.
    assert len(parsed) == 10
    assert (parsed["company"].str.strip() != "").all()
    assert "company" not in set(parsed["company"].str.lower())


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    visteon = parsed[parsed["company"] == "VISTEON CORPORATION"]
    assert visteon["employees"].tolist() == [0]  # blank in the feed


def test_parse_field_crosswalk_follows_bln(parsed):
    row = parsed[parsed["company"] == "Alabama Cooperage"]
    assert row["notice_date"].tolist() == ["2026-07-16"]     # date_notice
    assert row["effective_date"].tolist() == ["2026-09-14"]  # date_action
    assert row["employees"].tolist() == [71]                 # affected
    assert row["layoff_type"].tolist() == ["Closure"]        # action_type
    assert row["city"].tolist() == ["Trinity"]               # location


def test_parse_corrects_the_0001_placeholder_date(parsed):
    # BLN date_corrections: literal "01/01/0001" -> 2020-01-01; the
    # notice_date is kept as published, never copied over.
    gibson = parsed[parsed["company"] == "CR GIBSON"]
    assert gibson["effective_date"].tolist() == ["2020-01-01"]
    assert gibson["notice_date"].tolist() == ["2020-01-10"]
    secret = parsed[parsed["company"] == "VICTORIA'S SECRET"]
    assert secret["effective_date"].tolist() == ["2020-01-01"]


def test_parse_keeps_historical_action_types_and_statewide(parsed):
    types = set(parsed["layoff_type"])
    # The state's own asterisk footnote marker is preserved verbatim.
    assert {"Closure", "Layoff", "Closing *", "Layoff *"} <= types
    valley = parsed[parsed["company"] == "VALLEY SERVICES INC"]
    assert valley["city"].tolist() == ["Statewide"]


def test_parse_handles_quoted_commas_and_blank_city(parsed):
    hooters = parsed[parsed["company"] == "HOOTERS OF AMERICA, LLC"]
    assert hooters["city"].tolist() == [""]
    assert hooters["employees"].tolist() == [90]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("al", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "AL").all()
    assert (df["county"] == "").all()  # AL publishes no county


# ---------------------------------------------------------------------------
# Feed-CSV reader (headerless format vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_read_feed_csv_is_headerless_and_skips_blank_lines():
    text = (
        "AL202600003,Closure,07/16/2026,09/14/2026,"
        "Alabama Cooperage,Trinity,71,1519\n"
        "\n"
        ", , , , , , , \n"
        "b-135-837,Closing *,02/28/2001,04/28/2001,"
        "PLIANT CORPORATION,Birmingham,94,881\n"
    )
    rows = al_module._read_feed_csv(text)
    assert len(rows) == 2
    assert rows[0]["company"] == "Alabama Cooperage"
    assert rows[0]["affected"] == "71"
    assert rows[1]["action_type"] == "Closing *"


def test_clean_date_rejects_garbage_and_corrects_placeholder():
    assert al_module._clean_date("07/16/2026") == "2026-07-16"
    assert al_module._clean_date("01/01/0001") == "2020-01-01"
    assert al_module._clean_date("") is None
    assert al_module._clean_date("TBD") is None
