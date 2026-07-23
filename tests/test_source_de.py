"""Tests for the Delaware (DE) WARN source."""

import re
from pathlib import Path

import pandas as pd

import warn_sources
from warn_sources import de

FIXTURES = Path(__file__).parent / "fixtures"

# Real snapshots from joblink.delaware.gov (fetched 2026-07-21):
# - de_search_2026.html: 2026 search results (2 data rows, header sort links)
# - de_detail_110.html: detail page for record 110 (Atlas Hospitality)
# - de_sample.csv: 9 rows of the consolidated raw CSV the fetcher writes
SEARCH_HTML = (FIXTURES / "de_search_2026.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "de_detail_110.html").read_text(encoding="utf-8")
SAMPLE_CSV = FIXTURES / "de_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_delaware():
    assert "de" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["de"]
    assert cls.code == "de"
    assert cls.name == "Delaware"
    assert cls.source_url == "https://joblink.delaware.gov/search/warn_lookups"


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("de", tmp_path)
    assert src.paths.root == tmp_path / "states" / "de"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (JobLink layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_search_page_extracts_rows_and_skips_header():
    rows = de._parse_search_page(SEARCH_HTML)
    assert len(rows) == 2  # <th> sort links never leak through
    first = rows[0]
    assert first["employer"] == "Atlas Hospitality Group, LLC"
    assert first["city"] == "Baltimore"
    assert first["zip"] == "21231"
    assert first["notice_date"] == "Apr 30, 2026"
    assert first["warn_type"] == "WARN"
    assert first["record_number"] == "110"
    assert first["detail_page_url"] == (
        "https://joblink.delaware.gov/search/warn_lookups/110"
    )
    assert rows[1]["employer"] == "Conduent"
    assert de._next_page_url(SEARCH_HTML) is None


def test_parse_detail_page_extracts_address_and_count():
    detail = de._parse_detail_page(DETAIL_HTML)
    assert detail["number_of_employees_affected"] == "67"
    # Newlines collapse to "; " exactly as BLN's job_center transform does.
    assert detail["address"] == "1429 Aliceanna Street; Baltimore, Maryland 21231"


# ---------------------------------------------------------------------------
# parse(): consolidated CSV -> unified schema
# ---------------------------------------------------------------------------


def test_parse_sample_csv_matches_unified_schema():
    src = warn_sources.get_source("de")
    df = src.parse(SAMPLE_CSV)

    assert list(df.columns) == [
        "company",
        "notice_date",
        "employees",
        "layoff_type",
        "city",
        "address",
    ]
    # DE/JobLink publishes no effective date, county, or industry — parse
    # must not emit (let alone synthesize) those columns.
    for absent in ("effective_date", "county", "industry"):
        assert absent not in df.columns

    assert len(df) == 9
    assert df["company"].iloc[0] == "Atlas Hospitality Group, LLC"
    assert all(isinstance(e, int) for e in df["employees"])
    assert all(d is None or ISO_DATE.match(d) for d in df["notice_date"])

    # "Apr 30, 2026" (BLN date_format %b %d, %Y) -> ISO
    assert df["notice_date"].iloc[0] == "2026-04-30"
    assert df["employees"].iloc[0] == 67

    # Real Non-WARN row keeps the state's own taxonomy and its empty city.
    jakes = df[df["company"] == "Jakes Seafood II, Inc."].iloc[0]
    assert jakes["layoff_type"] == "Non-WARN"
    assert jakes["notice_date"] == "2020-03-16"
    assert jakes["city"] == ""


def test_parse_drops_junk_rows_and_defaults_missing_count(tmp_path):
    csv_path = tmp_path / "raw.csv"
    csv_path.write_text(
        "employer,notice_date,number_of_employees_affected,warn_type,"
        "city,zip,lwib_area,address,record_number,detail_page_url\n"
        'Acme Co,"Jan 05, 2026",,WARN,Dover,19901,1 - Statewide,,1,u\n'
        "Employer,,,,,,,,,\n"  # stray header row -> dropped
        ",,,,,,,,,\n"  # blank row -> dropped
    )
    src = warn_sources.get_source("de", tmp_path)
    df = src.parse(csv_path)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["company"] == "Acme Co"
    assert row["employees"] == 0  # no published count -> int 0, never None
    assert isinstance(row["employees"], int)
    assert row["notice_date"] == "2026-01-05"


def test_unify_stamps_state_and_nullable_fields():
    src = warn_sources.get_source("de")
    unified = src.unify(src.parse(SAMPLE_CSV))
    assert set(warn_sources.UNIFIED_FIELDS) <= set(unified.columns)
    assert (unified["state"] == "DE").all()
    assert unified["effective_date"].isna().all()  # never copied from notice
    assert (unified["county"] == "").all()


def test_clean_date_handles_blank_and_junk():
    assert de.DelawareJobLink._clean_date("Apr 30, 2026") == "2026-04-30"
    assert de.DelawareJobLink._clean_date("") is None
    assert de.DelawareJobLink._clean_date(None) is None
    assert de.DelawareJobLink._clean_date("N/A") is None


def test_parse_search_page_layout_change_raises():
    try:
        de._parse_search_page("<html><body><p>totally new layout</p></body></html>")
    except ValueError:
        pass
    else:
        raise AssertionError("layout change should raise ValueError")


def test_parse_search_page_no_matches_returns_empty():
    html = (
        "<html><body><p>There are no matches for your search results."
        "</p></body></html>"
    )
    assert de._parse_search_page(html) == []


def test_parse_returns_object_dtype_none_not_nan():
    df = warn_sources.get_source("de").parse(SAMPLE_CSV)
    # None (not NaN) so JSON serialization yields null, matching the engine.
    for d in df["notice_date"]:
        assert d is None or isinstance(d, str)
    assert not any(pd.isna(e) for e in df["employees"])
