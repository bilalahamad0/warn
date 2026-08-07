"""Tests for the Colorado (CO) WARN source."""

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import warn_sources
from warn_sources import co as co_module

# Truncated real snippet of the 2026 Google Sheet HTML export
# (header row + first five filings, fetched 2026-07-21).
FIXTURE = Path(__file__).parent / "fixtures" / "co_2026_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_colorado():
    assert "co" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["co"]
    assert cls.code == "co"
    assert cls.name == "Colorado"
    assert cls.source_url.startswith("https://cdle.colorado.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("co", tmp_path)
    assert src.paths.root == tmp_path / "states" / "co"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline fetch-shape + parse against the real-data fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("co", tmp_path)
    src.paths.ensure()
    table = BeautifulSoup(FIXTURE.read_text(), "html5lib").find("table")
    rows = co_module._standardize(co_module._table_rows(table))
    co_module._write_raw_csv(rows, src.paths.raw)
    return src.parse(src.paths.raw)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "industry",
    ]


def test_parse_keeps_all_data_rows_and_requires_company(parsed):
    assert len(parsed) == 5  # header row dropped, five filings kept
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk_follows_bln(parsed):
    tiaa = parsed[parsed["company"] == "TIAA"]
    assert tiaa["notice_date"].tolist() == ["2026-01-02"]     # WARN Date
    assert tiaa["effective_date"].tolist() == ["2026-03-03"]  # Begin Date
    assert tiaa["employees"].tolist() == [101]                # # Permanent
    assert tiaa["county"].tolist() == ["Denver, RC"]          # Workforce Area
    assert tiaa["layoff_type"].tolist() == ["Not Specified"]
    assert tiaa["industry"].tolist() == ["52: Finance and Insurance"]


def test_parse_never_copies_notice_date_into_effective_date(parsed):
    ritchey = parsed[parsed["company"] == "Alan Ritchey"]
    assert ritchey["notice_date"].tolist() == ["2026-01-06"]
    assert ritchey["effective_date"].tolist() == ["2026-02-28"]


def test_parse_uses_co_specific_permanent_count(parsed):
    # Tessera notified 90 nationwide but 1 in Colorado; the transformer
    # takes the permanent-losses column, not Total Notified.
    tessera = parsed[parsed["company"] == "Tessera Therapeutics"]
    assert tessera["employees"].tolist() == [1]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("co", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "CO").all()
    assert (df["city"] == "").all()  # CO publishes no per-notice city


# ---------------------------------------------------------------------------
# Sheet-scraping rules (vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_table_rows_requires_header_or_fallback():
    # 2019-style sheet: empty first row, data rows with no header.
    html = (
        "<table><tr><td></td></tr>"
        "<tr><td>Acme Aviation, Inc.</td><td>79</td><td>Denver</td>"
        "<td>9/22/2019</td><td>Loss of Contract</td><td>Various</td>"
        "<td>9/22/19</td></tr></table>"
    )
    table = BeautifulSoup(html, "html5lib").find("table")
    with pytest.raises(ValueError):
        co_module._table_rows(table)
    rows = co_module._table_rows(
        table, co_module._FALLBACK_HEADERS["2019"]
    )
    assert len(rows) == 1
    assert rows[0]["Company Name"] == "Acme Aviation, Inc."
    assert rows[0]["Layoff Date(s)"] == "9/22/19"


def test_standardize_drops_junk_and_applies_avis_fix():
    rows = [
        {"Company Name": "", "WARN Letter": "Avis Budget Group",
         "Layoff Total": "3"},          # 2020 quirk: name in letter col
        {"Company Name": "x", "Layoff Total": "5"},        # junk (< 3)
        {"Company Name": "Real Co", "Layoff Total": "12",
         "Layoff Date(s)": "Layoff Date(s)"},   # stray header fragment
    ]
    std = co_module._standardize(rows)
    assert [r["company"] for r in std] == ["Avis Budget Group"]
    assert std[0]["jobs"] == "3"


def test_corrections_vendored_from_bln_transformer():
    assert co_module._clean_date("11/20/20-11/30/290") == "2020-11-20"
    assert co_module._clean_date("Multi Phase (See WARN)") is None
    assert co_module._clean_date("12012024") == "2024-12-01"
    assert co_module._clean_date("1/15/2002") is None  # typo window
    assert co_module._clean_date("7/9/26") == "2026-07-09"
    assert co_module._clean_jobs("61 total, 4 in CO") == 4
    assert co_module._clean_jobs("Not specified") == 0
    assert co_module._clean_jobs("22000") is None  # jobs sanity cap
    assert co_module._clean_jobs("729") == 729
    assert co_module._clean_jobs("") is None


def test_sheet_export_url_adapts_both_link_schemas():
    assert co_module._sheet_export_url(
        "https://docs.google.com/spreadsheets/d/abc123/edit?gid=1#gid=1"
    ) == "https://docs.google.com/spreadsheets/d/abc123/gviz/tq?tqx=out:html"
    assert co_module._sheet_export_url(
        "https://drive.google.com/open?id=xyz789"
    ) == "https://docs.google.com/spreadsheets/d/xyz789/gviz/tq?tqx=out:html"
    with pytest.raises(ValueError):
        co_module._sheet_export_url("https://example.com/warn.pdf")
