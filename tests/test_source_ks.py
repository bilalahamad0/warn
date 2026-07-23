"""Tests for the Kansas (KS) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ks

# Real truncated snapshots of the KansasWorks JobLink app (fetched
# 2026-07-21): the 2026 search-results table (7 rows) and one notice's
# detail page, page chrome removed.
SEARCH_FIXTURE = Path(__file__).parent / "fixtures" / "ks_search_2026.html"
DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "ks_detail_2300.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_kansas():
    assert "ks" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ks"]
    assert cls.code == "ks"
    assert cls.name == "Kansas"
    assert cls.source_url == "https://www.kansasworks.com/search/warn_lookups"


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ks", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ks"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (JobLink layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_search_results_extracts_rows_and_skips_header():
    rows = ks._parse_search_results(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 7  # <th> header row never leaks through
    first = rows[0]
    assert first["employer"] == "First Brands Group, LLC (Hopkins)"
    assert first["city"] == "Emporia"
    assert first["notice_date"] == "Feb 23, 2026"
    assert first["warn_type"] == "WARN"
    assert first["record_number"] == "2300"
    assert first["detail_page_url"] == (
        "https://www.kansasworks.com/search/warn_lookups/2300"
    )


def test_parse_search_results_handles_no_matches_page():
    html = "<html><body>no matches for your search results</body></html>"
    assert ks._parse_search_results(html) == []


def test_parse_search_results_rejects_unrecognized_layout():
    with pytest.raises(RuntimeError):
        ks._parse_search_results("<html><body><p>Maintenance</p></body></html>")


def test_next_page_link_absent_on_single_page():
    html = SEARCH_FIXTURE.read_text(encoding="utf-8")
    assert ks._next_page_link(html) is None


def test_next_page_link_follows_pagination():
    html = '<a class="next_page" href="/search/warn_lookups?page=2">Next</a>'
    assert ks._next_page_link(html) == (
        "https://www.kansasworks.com/search/warn_lookups?page=2"
    )


def test_parse_detail_page_extracts_count_and_collapses_address():
    detail = ks._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    assert detail["number_of_employees_affected"] == "130"
    # Multi-line address collapsed to "; " as BLN's job_center helper does.
    assert detail["address"] == "428 Peyton St.; Emporia, Kansas 66801"


# ---------------------------------------------------------------------------
# Offline parse against the consolidated JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_json(tmp_path):
    rows = ks._parse_search_results(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    detail = ks._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    for row in rows:
        if row["record_number"] == "2300":
            row["number_of_employees_affected"] = detail[
                "number_of_employees_affected"
            ]
            row["address"] = detail["address"]
        else:
            row["number_of_employees_affected"] = "25"
            row["address"] = "1 Main St; Topeka, Kansas 66603"
    # Edge cases the full feed contains but this page happens not to:
    rows.append(  # no count, no date published
        {
            "employer": "Edge Case LLC",
            "city": "Salina",
            "zip": "",
            "lwib_area": "",
            "notice_date": "",
            "warn_type": "WARN",
            "record_number": "9001",
            "detail_page_url": "",
            "number_of_employees_affected": "",
            "address": "",
        }
    )
    rows.append(  # BLN jobs_corrections: 22000 is a verified feed error
        {
            "employer": "Feed Error Inc",
            "city": "Wichita",
            "zip": "",
            "lwib_area": "",
            "notice_date": "Mar 2, 2020",
            "warn_type": "WARN",
            "record_number": "9002",
            "detail_page_url": "",
            "number_of_employees_affected": "22,000",
            "address": "",
        }
    )
    rows.append(  # junk header echo
        {
            "employer": "Employer",
            "city": "City",
            "zip": "ZIP",
            "lwib_area": "LWIB Area",
            "notice_date": "Notice Date",
            "warn_type": "WARN Type",
            "record_number": "",
            "detail_page_url": "",
            "number_of_employees_affected": "",
            "address": "",
        }
    )
    path = tmp_path / "raw_download"
    path.write_text(json.dumps(rows))
    return path


@pytest.fixture
def parsed(tmp_path, raw_json):
    src = warn_sources.get_source("ks", tmp_path)
    return src.parse(raw_json)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "layoff_type",
        "city",
        "address",
    ]


def test_parse_drops_junk_header_rows(parsed):
    assert len(parsed) == 9  # 7 real rows + 2 edge cases, header echo dropped
    assert "Employer" not in set(parsed["company"])
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    edge = parsed[parsed["company"] == "Edge Case LLC"]
    assert edge["employees"].tolist() == [0]  # no count published
    assert edge["notice_date"].tolist() == [None]


def test_parse_honors_bln_jobs_corrections(parsed):
    row = parsed[parsed["company"] == "Feed Error Inc"]
    assert row["employees"].tolist() == [0]  # 22000 -> None per BLN crosswalk
    assert row["notice_date"].tolist() == ["2020-03-02"]


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "First Brands Group, LLC (Hopkins)"]
    row = row[row["city"] == "Emporia"]
    assert row["notice_date"].tolist() == ["2026-02-23"]  # "Feb 23, 2026"
    assert row["employees"].tolist() == [130]  # detail-page count
    assert row["layoff_type"].tolist() == ["WARN"]
    assert row["address"].tolist() == ["428 Peyton St.; Emporia, Kansas 66801"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ks", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "KS").all()
    # Kansas publishes no effective date, county, or industry — never
    # synthesized from other fields.
    assert df["effective_date"].isna().all()
    assert (df["county"] == "").all()
    assert (df["industry"] == "").all()
