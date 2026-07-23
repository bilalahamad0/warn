"""Tests for the Washington (WA) WARN source."""

import csv
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import wa

# Real page-1 snapshot of the ESD search app (fetched 2026-07-21, 15 data
# rows) with the opaque VIEWSTATE/EVENTVALIDATION blobs stubbed out.
FIXTURE = Path(__file__).parent / "fixtures" / "wa_search_page1.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_washington():
    assert "wa" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["wa"]
    assert cls.code == "wa"
    assert cls.name == "Washington"
    assert cls.source_url.startswith("https://fortress.wa.gov/esd/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("wa", tmp_path)
    assert src.paths.root == tmp_path / "states" / "wa"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (GridView layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_page_extracts_data_rows_and_skips_pager():
    rows = wa._parse_page(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 15  # the pager chrome rows never leak through
    assert rows[0]["Company"] == "Verizon"
    assert rows[0]["Layoff Start Date"] == "9/18/2026"
    assert rows[0]["Received Date"] == "7/16/2026"
    assert rows[-1]["Company"] == "SMBC Manubank"


def test_parse_page_rejects_unexpected_headers():
    with pytest.raises(ValueError):
        wa._parse_page("<table><tr><th>Nope</th></tr></table>")


def test_has_next_page_reads_the_pager():
    html = FIXTURE.read_text(encoding="utf-8")
    assert wa._has_next_page(html, 1)  # links to Page$2..Page$11 exist
    assert not wa._has_next_page(html, 11)  # nothing beyond the pager range


# ---------------------------------------------------------------------------
# Offline parse against the consolidated CSV built from the real fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_csv(tmp_path):
    """The consolidated CSV exactly as fetch() writes it."""
    rows = wa._parse_page(FIXTURE.read_text(encoding="utf-8"))
    # Edge cases the full feed contains but page 1 happens not to: a row
    # with no worker count / no dates, and a stray repeated header row.
    rows.append(
        {
            "Company": "Edge Case LLC",
            "Location": "Spokane",
            "Layoff Start Date": "",
            "# of Workers": "",
            "Closure Layoff": "Closure",
            "Type of Layoff": "",
            "Received Date": "",
            "Notice": "",
        }
    )
    rows.append(dict(zip(wa._COLUMNS, wa._COLUMNS)))  # junk header row
    path = tmp_path / "raw_download"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=wa._COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def parsed(tmp_path, raw_csv):
    src = warn_sources.get_source("wa", tmp_path)
    return src.parse(raw_csv)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]


def test_parse_drops_junk_header_rows(parsed):
    assert len(parsed) == 16  # 15 real rows + edge case, header row dropped
    assert "Company" not in set(parsed["company"])
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    edge = parsed[parsed["company"] == "Edge Case LLC"]
    assert edge["employees"].tolist() == [0]  # no count published
    assert edge["notice_date"].tolist() == [None]
    assert edge["effective_date"].tolist() == [None]
    assert edge["layoff_type"].tolist() == ["Closure"]


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Verizon"]
    assert row["notice_date"].tolist() == ["2026-07-16"]     # Received Date
    assert row["effective_date"].tolist() == ["2026-09-18"]  # Layoff Start
    assert row["employees"].tolist() == [54]                 # # of Workers
    assert row["layoff_type"].tolist() == ["Layoff Permanent"]
    assert row["city"].tolist() == ["Various locations in Washington"]


def test_parse_never_copies_one_date_into_the_other(parsed):
    # Every real fixture row has distinct received vs. layoff-start dates.
    real = parsed[parsed["company"] != "Edge Case LLC"]
    assert (real["notice_date"] != real["effective_date"]).all()


def test_parse_joins_closure_and_type_ca_style(parsed):
    alpha = parsed[parsed["company"] == "Alpha Technologies Services, Inc."]
    assert alpha["layoff_type"].tolist() == ["Closure Permanent"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("wa", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "WA").all()
    assert (df["county"] == "").all()  # WA publishes no county
