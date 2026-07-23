"""Tests for the Maryland (MD) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import md as md_module

FIXTURE = Path(__file__).parent / "fixtures" / "md_pages_sample.json"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_maryland():
    assert "md" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["md"]
    assert cls.code == "md"
    assert cls.name == "Maryland"
    assert cls.source_url.startswith("https://www.dllr.state.md.us/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("md", tmp_path)
    assert src.paths.root == tmp_path / "states" / "md"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a truncated real-pages fixture (12 notices across
# the modern "Local Area"/"Type" era and the pre-2021 "WIA Code"/"Type
# Code" era)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("md", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "address",
        "industry",
    ]
    # MD publishes one mixed Location column (kept as address) — city is
    # never fabricated from it.
    assert "city" not in parsed.columns


def test_parse_drops_header_rows(parsed):
    assert len(parsed) == 12
    assert "Company" not in set(parsed["company"])
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_modern_era_row(parsed):
    row = parsed[parsed["company"] == "Taft Broadcasting, LLC"]
    assert row["notice_date"].tolist() == ["2026-07-20"]
    assert row["effective_date"].tolist() == ["2026-09-30"]
    assert row["employees"].tolist() == [2]
    assert row["layoff_type"].tolist() == ["Mass Layoff - No Recall"]
    assert row["county"].tolist() == ["Anne Arundel"]
    assert row["address"].tolist() == ["6700 Taylor Avenue"]
    assert row["industry"].tolist() == ["541990"]


def test_parse_effective_date_range_resolves_to_first_date(parsed):
    # "07/08/2026 - 08/17/2026" on the live page.
    row = parsed[parsed["company"] == "Dejana Truck and Utility Equipment"]
    assert row["effective_date"].tolist() == ["2026-07-08"]
    assert row["layoff_type"].tolist() == ["Temporary Furlough"]


def test_parse_wia_and_type_codes_decode_via_printed_legend(parsed):
    # 2020-era rows publish "WIA Code" 5 and "Type Code" 1 — the page's
    # own printed legends decode them.
    row = parsed[
        (parsed["company"] == "Macy's") & (parsed["notice_date"] == "2020-01-10")
    ]
    assert row["county"].tolist() == ["Lower Shore"]
    assert row["layoff_type"].tolist() == ["Plant Closure"]


def test_parse_strips_thousands_separator(parsed):
    row = parsed[parsed["company"].str.startswith("IAS Logistics")]
    assert row["employees"].tolist() == [1609]  # "1,609" on the page


def test_parse_applies_bln_corrections_on_real_row(parsed):
    # Real 2020 row: effective date "3/16/2020 (REVISED) 10/22/2020
    # 11/26/2020" and jobs "103 (REVISED) 10/22/2020 108" — both resolve
    # per BLN warn-transformer corrections.
    row = parsed[parsed["company"] == "Collegiate Hotel Group"]
    assert row["effective_date"].tolist() == ["2020-03-16"]
    assert row["employees"].tolist() == [103]


def test_parse_unknown_count_becomes_zero(parsed):
    rows = parsed[parsed["company"] == "Capital One Financial"]
    assert len(rows) == 4  # one filing, four sites — all kept
    assert rows["employees"].tolist() == [0, 0, 0, 0]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("md", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "MD").all()
    assert (df["city"] == "").all()  # never fabricated from Location


# ---------------------------------------------------------------------------
# Date / jobs cleaning (rules vendored from BLN warn-transformer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1/06/2025", "2025-01-06"),
        ("9/20/18 - 9/30/18", "2018-09-20"),
        ("4/1/2020 (REVISED) 10/22/2020", "2020-04-01"),
        ("4/13/2018to 5/11/2018", "2018-04-13"),
        ("Start 12/1/10 End 9/2011", "2010-12-01"),
        ("5/62011", "2011-05-06"),  # vendored BLN correction
        ("8/2017-12/2018", "2017-08-01"),  # month-only span
        ("2/29/2014", "2014-02-28"),  # invalid leap date, never non-ISO
        ("7/24/1969", "2024-07-24"),  # source-page year typo
        ("Unknown at this time", None),
        ("N/A", None),
        ("517112", None),  # NAICS code misfiled in a date column
        ("", None),
        (None, None),
    ],
)
def test_clean_date(raw, expected):
    assert md_module._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("213", 213),
        ("1,609", 1609),
        ("103 (REVISED) 10/22/2020 108", 103),
        ("50 - 60", 50),
        ("1100-1200 (MDDCVA)", 1100),
        ("approx. 150", 150),
        ("9 50", 59),  # vendored BLN correction, verbatim
        ("TBD", None),
        ("Unknown", None),
        ("N/A", None),
        ("", None),
    ],
)
def test_clean_jobs(raw, expected):
    assert md_module._clean_jobs(raw) == expected
