"""Tests for the Tennessee (TN) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import tn as tn_module

FIXTURE = Path(__file__).parent / "fixtures" / "tn_reports_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_tennessee():
    assert "tn" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["tn"]
    assert cls.code == "tn"
    assert cls.name == "Tennessee"
    assert cls.source_url.startswith("https://www.tn.gov/workforce")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("tn", tmp_path)
    assert src.paths.root == tmp_path / "states" / "tn"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a truncated real-HTML fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("tn", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "county",
    ]


def test_parse_drops_junk_rows_and_requires_company(parsed):
    # Fixture holds 14 real notices across both year tables, plus one
    # blank-company row and one repeated-header row — only notices survive.
    assert len(parsed) == 14
    assert (parsed["company"].str.strip() != "").all()
    assert "company" not in set(parsed["company"].str.lower())


def test_parse_reads_both_year_tables(parsed):
    companies = set(parsed["company"])
    assert "Carlex" in companies                  # 2026 WARN Notices table
    assert "Perdue Farms" in companies            # Archived Reports table


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    carlex = parsed[parsed["company"] == "Carlex"]
    assert carlex["employees"].tolist() == [325]


def test_parse_field_crosswalk_follows_bln(parsed):
    # BLN transformer: notice_date="Notice Date" (Date of Posting),
    # effective_date="Effective Date" (Closure/Layoff Date),
    # jobs="No. Of Employees" (Affected Workers); county kept as published.
    row = parsed[parsed["company"] == "US Endodontics, LLC"]
    assert row["notice_date"].tolist() == ["2025-12-30"]
    assert row["effective_date"].tolist() == ["2026-02-27"]
    assert row["employees"].tolist() == [70]
    assert row["county"].tolist() == ["Washington"]


def test_parse_multi_date_range_collapses_to_first_date(parsed):
    # "8-28-2026/ 10-30-2026/<br/>12/31-2026" -> 2026-08-28 (BLN
    # date_corrections quirk: a range keeps its first listed date).
    stanley = parsed[parsed["company"] == "Stanley Black & Decker"]
    assert stanley["effective_date"].tolist() == ["2026-08-28"]
    assert stanley["notice_date"].tolist() == ["2026-06-24"]  # never copied
    # "3-20-2026 /7-24-2026 to 7-31-2026" -> 2026-03-20
    linamar = parsed[parsed["company"] == "Linamar Shelbyville"]
    assert linamar["effective_date"].tolist() == ["2026-03-20"]
    # "4/1/2025 to 7/31/2025" -> 2025-04-01
    modine = parsed[parsed["company"] == "Modine Manufacturing"]
    assert modine["effective_date"].tolist() == ["2025-04-01"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("tn", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "TN").all()
    assert (df["city"] == "").all()   # TN's live page publishes no city
    assert (df["layoff_type"] == "").all()


# ---------------------------------------------------------------------------
# Date cleaner (vendored BLN transform_date behavior)
# ---------------------------------------------------------------------------


def test_clean_date_formats_and_ranges():
    assert tn_module._clean_date("7/13/2026") == "2026-07-13"
    assert tn_module._clean_date("9-7-2026") == "2026-09-07"
    assert tn_module._clean_date("March 13, 2020") == "2020-03-13"
    assert tn_module._clean_date("4-1-2026 through October 2026") == "2026-04-01"
    assert tn_module._clean_date("5-12-2026to 6-5-2026") == "2026-05-12"
    assert (
        tn_module._clean_date("8-28-2026/ 10-30-2026/\n12/31-2026")
        == "2026-08-28"
    )


def test_clean_date_rejects_garbage():
    assert tn_module._clean_date("") is None
    assert tn_module._clean_date(None) is None
    assert tn_module._clean_date("TBD") is None
    assert tn_module._clean_date("124") is None       # BLN date_corrections
    # No explicit year -> never parsed (pandas would inject current year).
    assert tn_module._clean_date("November 9") is None


def test_clean_jobs_applies_bln_corrections():
    assert tn_module._clean_jobs("147 (69 Tennessee residents)") == 69
    assert tn_module._clean_jobs("135 (7 in Tennessee)") == 7
    assert tn_module._clean_jobs("1,024") == 1024
    assert tn_module._clean_jobs("") == 0
    assert tn_module._clean_jobs("unknown") == 0
