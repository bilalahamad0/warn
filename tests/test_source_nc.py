"""Tests for the North Carolina (NC) WARN source."""

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import warn_sources
from warn_sources import nc as nc_module

# Truncated real snippet of the 2026 summary-list HTML table
# (real header + eight real filings, fetched 2026-07-22).
FIXTURE = Path(__file__).parent / "fixtures" / "nc_2026_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _grid_from_html(path):
    table = BeautifulSoup(path.read_text(), "html5lib").find("table")
    return [
        [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        for tr in table.find_all("tr")
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_north_carolina():
    assert "nc" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["nc"]
    assert cls.code == "nc"
    assert cls.name == "North Carolina"
    assert cls.source_url.startswith("https://www.commerce.nc.gov")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("nc", tmp_path)
    assert src.paths.root == tmp_path / "states" / "nc"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against the real-data HTML fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("nc", tmp_path)
    src.paths.ensure()
    rows, mapping = nc_module._rows_from_grid(_grid_from_html(FIXTURE))
    assert mapping is not None
    full = [
        {col: r.get(col, "2026" if col == "year" else "")
         for col in nc_module._RAW_COLUMNS}
        for r in rows
    ]
    nc_module._write_raw_csv(full, src.paths.raw)
    return src.parse(src.paths.raw)


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
    ]


def test_parse_keeps_all_data_rows_and_requires_company(parsed):
    assert len(parsed) == 8  # header dropped, eight filings kept
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_mapping(parsed):
    thermo = parsed[parsed["company"] == "Thermo Fisher Scientific"]
    assert thermo["notice_date"].tolist() == ["2026-01-08"]
    assert thermo["effective_date"].tolist() == ["2027-12-31"]
    assert thermo["employees"].tolist() == [423]
    assert thermo["layoff_type"].tolist() == ["Closure Permanent"]
    assert thermo["county"].tolist() == ["Buncombe"]  # " County" stripped
    assert thermo["city"].tolist() == ["Asheville"]
    assert thermo["address"].tolist() == ["275 Aiken Road"]


def test_parse_notice_date_is_date_received_by_nc(parsed):
    # Frito Lay: Date of Notice 7/6/2026, Date Received by NC 7/10/2026 —
    # the unified notice_date is the received-by-state date, and the
    # employer's notice-stamp date is never copied into it.
    frito = parsed[parsed["company"] == "Frito Lay, Inc."]
    assert frito["notice_date"].tolist() == ["2026-07-10"]
    assert frito["effective_date"].tolist() == ["2026-09-06"]


def test_parse_out_of_state_hq_quirks(parsed):
    # SMBC MANUBANK filed from its New York HQ: county is published as
    # "N/A" (-> "") and City carries the out-of-state "New York NY".
    smbc = parsed[parsed["company"] == "SMBC MANUBANK"]
    assert smbc["county"].tolist() == [""]
    assert smbc["city"].tolist() == ["New York NY"]
    assert smbc["layoff_type"].tolist() == ["Layoff Permanent"]


def test_parse_multi_location_notices_stay_per_location(parsed):
    # Avelo filed one notice covering two airports; NC lists one row per
    # location and each stays a separate record.
    avelo = parsed[parsed["company"] == "Avelo Airlines, Inc."]
    assert sorted(avelo["employees"].tolist()) == [78, 82]
    assert sorted(avelo["city"].tolist()) == ["Morrisville", "Wilmington"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("nc", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "NC").all()
    assert (df["industry"] == "").all()  # NC publishes no industry


# ---------------------------------------------------------------------------
# Grid-scraping rules shared by the HTML report and the archive PDFs
# ---------------------------------------------------------------------------

# Vendored from the real 2025 "Workforce WARN Listings" PDF as extracted
# by pdfplumber (title banner, wrapped headers, None padding, page-2
# continuation without a header, Total Sum footer).
_PDF_PAGE1 = [
    ["WARN Summary by County/Parish\nAs of 2026-07-17", None, None, None,
     None, None, None, None, None, None, None, None, None],
    ["", "County", "Warn\nNumber", "Date of\nNotice", "Date\nReceived by\nNC",
     "Effective\nDate", "WARN Notice: WARN Notice Name", "WARN\nnotice\ntype",
     "Type of\nlayoff or\nclosure", "Number affected at\nthis location",
     "Address 1", "City", ""],
    ["", "Durham County", "202500001", "1/6/2025", "1/6/2025", "12/15/2025",
     "Resilience US, Inc.", "Layoff", "Permanent", "120",
     "1733 TW Alexander Dr", "Durham", ""],
    [None, "Mecklenburg County", "202500002", "1/13/2025", "1/13/2025",
     "8/29/2025", "Eaton", "Closure", "Permanent", "76",
     "5910 Long Creek Park Drive", "Charlotte", None],
]

_PDF_PAGE2 = [
    ["", "Wilson County", "202500047", "9/30/2025", "9/30/2025", "12/1/2025",
     "Mativ Conwed, LLC", "Closure", "Permanent", "50",
     "2711 Commerce Road S", "Wilson", ""],
    ["", "Total Sum\nCount", "", "", "", "", "", "", "", "7365", "", "", ""],
    [None, None, "89", "", "", "", "", "", "", "", "", "", None],
]

# Shape of the aggregate "Break Outs" tables that share some archive PDFs.
_PDF_BREAKOUT = [
    ["", "Wake County", "12", "", "", "", "1523", "", "", "", "", "", ""],
]


def test_rows_from_grid_maps_headers_and_drops_junk():
    rows, carry = nc_module._rows_from_grid(_PDF_PAGE1)
    assert carry is not None
    assert [r["company"] for r in rows] == ["Resilience US, Inc.", "Eaton"]
    assert rows[0]["received_date"] == "1/6/2025"
    assert rows[0]["warn_number"] == "202500001"


def test_rows_from_grid_carries_header_across_pages():
    _, carry = nc_module._rows_from_grid(_PDF_PAGE1)
    rows, _ = nc_module._rows_from_grid(_PDF_PAGE2, carry)
    assert [r["company"] for r in rows] == ["Mativ Conwed, LLC"]
    # Total Sum footer and the trailing notice-count row are junk.


def test_rows_from_grid_rejects_breakout_tables_as_continuations():
    _, carry = nc_module._rows_from_grid(_PDF_PAGE1)
    rows, _ = nc_module._rows_from_grid(_PDF_BREAKOUT, carry)
    assert rows == []  # "12" in the Warn Number slot is no 20YY##### id
    assert nc_module._rows_from_grid(_PDF_BREAKOUT) == ([], None)


def test_2022_pdf_column_order_is_resolved_by_header_text():
    grid = [
        ["County", "Warn\nNumber", "Date of\nNotice", "Date Received\nby NC",
         "Effective Date", "WARN Notice: WARN Notice Name",
         "Type of layoff\nor closure", "WARN notice\ntype",
         "Number\naffected at this\nlocation", "Address 1", "City"],
        ["Guilford County", "202200001", "1/21/2022", "1/25/2022",
         "12/31/2022", "ADT Cybersecurity/SDI facility", "Permanent",
         "Closure", "67", "301 North Elm Street Suite 550", "Greensboro"],
    ]
    rows, _ = nc_module._rows_from_grid(grid)
    assert rows[0]["notice_type"] == "Closure"
    assert rows[0]["layoff_kind"] == "Permanent"
    assert rows[0]["received_date"] == "1/25/2022"


# ---------------------------------------------------------------------------
# Cell cleaners
# ---------------------------------------------------------------------------


def test_clean_date():
    assert nc_module._clean_date("1/7/2026") == "2026-01-07"
    assert nc_module._clean_date("12/31/2027") == "2027-12-31"
    assert nc_module._clean_date("N/A") is None
    assert nc_module._clean_date("") is None
    assert nc_module._clean_date("TBD") is None
    assert nc_module._clean_date("1/15/2002") is None  # typo window


def test_clean_employees():
    assert nc_module._clean_employees("423") == 423
    assert nc_module._clean_employees("1,200") == 1200
    assert nc_module._clean_employees("") is None
    assert nc_module._clean_employees("22000") is None  # sanity cap


def test_clean_county():
    assert nc_module._clean_county("New Hanover County") == "New Hanover"
    assert nc_module._clean_county("N/A") == ""
    assert nc_module._clean_county("Wake") == "Wake"
