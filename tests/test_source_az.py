"""Tests for the Arizona (AZ) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import az

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snapshots of the JobLink app (fetched 2026-07-21): 6 of
# 25 search-result rows with the pager preserved, plus one detail page.
SEARCH_FIXTURE = FIXTURES / "az_search_sample.html"
DETAIL_FIXTURE = FIXTURES / "az_detail_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_arizona():
    assert "az" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["az"]
    assert cls.code == "az"
    assert cls.name == "Arizona"
    assert cls.source_url == "https://www.azjobconnection.gov/search/warn_lookups"


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("az", tmp_path)
    assert src.paths.root == tmp_path / "states" / "az"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (JobLink layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_search_results_extracts_rows_and_skips_header():
    rows = az._parse_search_results(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 6  # the <th> header row never leaks through
    assert rows[0]["employer"] == "Block, Inc."
    assert rows[0]["city"] == "Oakland"
    assert rows[0]["notice_date"] == "Feb 26, 2026"
    assert rows[0]["warn_type"] == "WARN"
    assert rows[0]["record_number"] == "954"
    assert rows[0]["detail_page_url"] == (
        "https://www.azjobconnection.gov/search/warn_lookups/954"
    )


def test_parse_search_results_raises_on_layout_drift():
    with pytest.raises(RuntimeError):
        az._parse_search_results("<html><body>something else</body></html>")


def test_next_page_link_follows_pager():
    html = SEARCH_FIXTURE.read_text(encoding="utf-8")
    link = az._next_page_link(html)
    assert link.startswith("https://www.azjobconnection.gov/search/warn_lookups?")
    assert "page=2" in link
    assert az._next_page_link("<html><body>no pager</body></html>") is None


def test_parse_detail_page_extracts_count_and_flattens_address():
    detail = az._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    assert detail["number_of_employees_affected"] == "83"
    # BLN job_center utils: newlines collapsed to "; "
    assert detail["address"] == (
        "1955 Broadway, Suite 600; Oakland, California 94612"
    )


# ---------------------------------------------------------------------------
# Offline parse against the raw JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_json(tmp_path):
    """Consolidated raw file built from the real fixtures."""
    rows = az._parse_search_results(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    detail = az._parse_detail_page(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    for row in rows:
        if row["record_number"] == "954":
            row["number_of_employees_affected"] = detail[
                "number_of_employees_affected"
            ]
            row["address"] = detail["address"]
        else:
            row["number_of_employees_affected"] = "10"
            row["address"] = "1 Main St; Phoenix, Arizona 85001"
    # Edge cases the full feed contains but the snippet happens not to: a
    # row with no count/date/address, a junk header echo, an absurd count.
    rows.append(
        {
            "employer": "Edge Case LLC",
            "city": "Yuma",
            "notice_date": "",
            "warn_type": "WARN",
            "record_number": "1",
            "number_of_employees_affected": "",
            "address": "",
        }
    )
    rows.append({"employer": "Employer", "record_number": "2"})
    rows.append(
        {
            "employer": "Absurd Count Co",
            "city": "Mesa",
            "notice_date": "Jan 02, 2025",
            "warn_type": "WARN",
            "record_number": "3",
            "number_of_employees_affected": "999999",
            "address": "",
        }
    )
    path = tmp_path / "raw_download"
    path.write_text(json.dumps(rows))
    return path


@pytest.fixture
def parsed(tmp_path, raw_json):
    src = warn_sources.get_source("az", tmp_path)
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
    assert len(parsed) == 8  # 6 real + 2 edge cases, header echo dropped
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
    # BLN BaseTransformer maximum_jobs sanity cap
    absurd = parsed[parsed["company"] == "Absurd Count Co"]
    assert absurd["employees"].tolist() == [0]


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Block, Inc."]
    assert row["notice_date"].tolist() == ["2026-02-26"]  # "%b %d, %Y"
    assert row["employees"].tolist() == [83]
    assert row["layoff_type"].tolist() == ["WARN"]
    assert row["city"].tolist() == ["Oakland"]
    assert row["address"].tolist() == [
        "1955 Broadway, Suite 600; Oakland, California 94612"
    ]


def test_parse_never_emits_effective_date(parsed):
    # JobLink publishes no effective date; it must never be synthesized.
    assert "effective_date" not in parsed.columns


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("az", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "AZ").all()
    assert df["effective_date"].isna().all()  # never copied from notice_date
    assert (df["county"] == "").all()  # AZ publishes no county
