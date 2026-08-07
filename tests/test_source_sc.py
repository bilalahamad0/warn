"""Tests for the South Carolina (SC) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import sc as sc_module

FIXTURE = Path(__file__).parent / "fixtures" / "sc_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_south_carolina():
    assert "sc" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["sc"]
    assert cls.code == "sc"
    assert cls.name == "South Carolina"
    assert cls.source_url.startswith("https://scworks.org/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("sc", tmp_path)
    assert src.paths.root == tmp_path / "states" / "sc"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture (consolidated raw CSV built
# from rows the live crawl extracted out of the state's annual PDFs)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("sc", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "city",
        "address",
        "industry",
    ]


def test_parse_drops_junk_and_dedupes_cross_year_repeat(parsed):
    # 15 fixture rows -> 13 records: the blank-company row drops, and the
    # Bank of America notice listed in both the 2014 and 2015 annual
    # reports collapses to one record.
    assert len(parsed) == 13
    assert (parsed["company"].str.strip() != "").all()
    assert len(parsed[parsed["company"] == "Bank of America"]) == 1


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    # AREVA's legacy row has no parseable jobs cell -> 0, never None.
    areva = parsed[parsed["company"] == "AREVA Federal ServicesLLC"]
    assert areva["employees"].tolist() == [0]


def test_parse_legacy_crosswalk_follows_bln(parsed):
    # BLN warn-transformer sc.py: date -> notice_date, location -> city;
    # legacy reports publish no effective date and none is fabricated.
    row = parsed[parsed["company"] == "Fast Lane of Rock Hill"]
    assert row["notice_date"].tolist() == ["2013-02-25"]
    assert row["effective_date"].tolist() == [None]
    assert row["employees"].tolist() == [31]
    assert row["layoff_type"].tolist() == ["Closure"]
    assert row["city"].tolist() == ["Rock Hill"]
    assert row["county"].tolist() == [""]
    assert row["industry"].tolist() == ["722511"]  # NAICS


def test_parse_modern_columns_map_by_state_labels(parsed):
    row = parsed[parsed["company"] == "Block, Inc."]
    assert row["notice_date"].tolist() == ["2024-01-30"]
    assert row["effective_date"].tolist() == ["2024-03-30"]
    assert row["employees"].tolist() == [3]
    assert row["layoff_type"].tolist() == ["Permanent Layoff"]
    assert row["county"].tolist() == ["Statewide - Multiple Counties"]
    assert row["city"].tolist() == [""]
    assert row["address"].tolist() == [
        "1955 Broadway St., Ste 600, Oakland, CA 94612"
    ]


def test_parse_vendors_bln_literal_date_typo_corrections(parsed):
    # BLN date_corrections: "12/31//2015" and "4/8/20/20".
    mohawk = parsed[parsed["company"] == "Mohawk Industries"]
    assert mohawk["notice_date"].tolist() == ["2015-12-31"]
    peak = parsed[parsed["company"] == "Peak Workforce Solutions"]
    assert peak["notice_date"].tolist() == ["2020-04-08"]


def test_parse_collapses_date_ranges_to_range_start(parsed):
    # "5/10/2024 - 12/31/2024" -> 2024-05-10 (BLN convention), including
    # the 2-digit-year variant "8/19/24 - 9/20/24".
    stanley = parsed[parsed["company"] == "Stanley Black & Decker"]
    assert stanley["effective_date"].tolist() == ["2024-05-10"]
    interfor = parsed[parsed["company"] == "Interfor"]
    assert interfor["effective_date"].tolist() == ["2024-08-19"]


def test_parse_repairs_county_overflow_interleaved_into_date(parsed):
    # pdfplumber interleaves wrapped County text with the Notice Date
    # cell ("ltiple 1C/8o/u2n0t2ie6s"); the digit/slash subsequence is
    # the date and the letters restore the county.
    smbc = parsed[parsed["company"].str.startswith("SMBC")]
    assert smbc["county"].tolist() == ["Statewide - Multiple Counties"]
    assert smbc["notice_date"].tolist() == ["2026-01-08"]
    # Same repair when the Company column overflowed into County: the
    # mangled cell subsequence-matches exactly one real county.
    caraustar = parsed[parsed["company"] == "Caraustar Industrial &"]
    assert caraustar["county"].tolist() == ["York"]
    assert caraustar["notice_date"].tolist() == ["2023-07-05"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("sc", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "SC").all()


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_clean_date_rejects_partial_dates_and_garbage():
    assert sc_module._clean_date("2/16/2023") == "2023-02-16"
    assert sc_module._clean_date("8/19/24") == "2024-08-19"
    assert sc_module._clean_date("2/2014") is None      # month-only
    assert sc_module._clean_date("June 2018") is None   # prose
    assert sc_module._clean_date("2025") is None        # bare year
    assert sc_module._clean_date("") is None
    assert sc_module._clean_date(None) is None


def test_year_pdf_links_takes_first_link_per_year():
    html = """
    <html><body>
      <a href="/media/2026.pdf">2026 WARN Report</a>
      <a href="/media/2026-old.pdf">2026 WARN Report (old)</a>
      <a href="/media/2025.pdf">2025 WARN Report</a>
      <a href="/media/brochure.pdf">Employer brochure</a>
      <a href="/contact">Contact us</a>
    </body></html>
    """
    links = sc_module._year_pdf_links(html)
    assert links == {2026: "/media/2026.pdf", 2025: "/media/2025.pdf"}
