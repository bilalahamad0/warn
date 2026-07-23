"""Tests for the Maine (ME) WARN source."""

import csv
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import me

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snapshots of the Maine JobLink app (fetched 2026-07-21):
# a 2026 date-range search-results page (4 data rows) and the detail page
# for record 649.
SEARCH_FIXTURE = FIXTURES / "me_search_2026_sample.html"
DETAIL_FIXTURE = FIXTURES / "me_detail_649.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_maine():
    assert "me" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["me"]
    assert cls.code == "me"
    assert cls.name == "Maine"
    assert cls.source_url == "https://joblink.maine.gov/search/warn_lookups"


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("me", tmp_path)
    assert src.paths.root == tmp_path / "states" / "me"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (JobLink layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_search_page_extracts_data_rows():
    rows = me._parse_search_page(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 4  # <th> header sort links never leak through
    first = rows[0]
    assert first["employer"] == "SMBC-Sumitomo Mitsui Banking Corporation"
    assert first["city"] == "New York"
    assert first["notice_date"] == "Jan 13, 2026"
    assert first["warn_type"] == "WARN"
    assert first["record_number"] == "649"
    assert first["detail_page_url"] == (
        "https://joblink.maine.gov/search/warn_lookups/649"
    )


def test_parse_search_page_rejects_unknown_layout():
    with pytest.raises(ValueError):
        me._parse_search_page("<html><body>maintenance</body></html>")


def test_parse_search_page_accepts_no_matches_message():
    html = "<p>There were no matches for your search results.</p>"
    assert me._parse_search_page(html) == []


def test_parse_detail_page_extracts_count_and_address():
    detail = me._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    assert detail["number_of_employees_affected"] == "1"
    # Newlines collapsed to "; " exactly as BLN's job_center helper does.
    assert detail["address"] == "277 Park Avenue; New York, New York 10172"


# ---------------------------------------------------------------------------
# Offline parse against the consolidated CSV built from the real fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_csv(tmp_path):
    """The consolidated CSV exactly as fetch() writes it."""
    rows = me._parse_search_page(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    detail = me._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    rows[0].update(detail)
    # Edge cases the full feed contains but this page happens not to:
    # a row with no count / no date, and a stray repeated header row.
    rows.append(
        {
            "employer": "Edge Case LLC",
            "notice_date": "",
            "number_of_employees_affected": "",
            "warn_type": "WARN",
            "city": "Bangor",
            "address": "",
        }
    )
    rows.append({col: col for col in me._CSV_COLUMNS})  # junk header row
    path = tmp_path / "raw_download"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=me._CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in me._CSV_COLUMNS})
    return path


@pytest.fixture
def parsed(tmp_path, raw_csv):
    src = warn_sources.get_source("me", tmp_path)
    return src.parse(raw_csv)


def test_parse_columns(parsed):
    # ME publishes no effective date, county, or industry — never emitted.
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "layoff_type",
        "city",
        "address",
    ]


def test_parse_drops_junk_header_rows(parsed):
    assert len(parsed) == 5  # 4 real rows + edge case; header row dropped
    assert "employer" not in set(parsed["company"])
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    edge = parsed[parsed["company"] == "Edge Case LLC"]
    assert edge["employees"].tolist() == [0]  # no count published
    assert edge["notice_date"].tolist() == [None]
    assert edge["layoff_type"].tolist() == ["WARN"]


def test_parse_field_crosswalk(parsed):
    """BLN warn-transformer me.py mapping, incl. %b %d, %Y dates."""
    row = parsed[parsed["company"] == "SMBC-Sumitomo Mitsui Banking Corporation"]
    assert row["notice_date"].tolist() == ["2026-01-13"]
    assert row["employees"].tolist() == [1]  # from the detail page
    assert row["layoff_type"].tolist() == ["WARN"]
    assert row["city"].tolist() == ["New York"]
    assert row["address"].tolist() == [
        "277 Park Avenue; New York, New York 10172"
    ]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("me", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "ME").all()
    assert (df["county"] == "").all()  # ME publishes no county
    # JobLink feeds have no effective date; it must never be synthesized.
    assert df["effective_date"].isna().all()
