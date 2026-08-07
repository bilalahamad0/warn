"""Tests for the Kentucky WARN source (warn_sources/ky.py).

Offline: the fixtures under tests/fixtures/ are tiny XLSX files built from
rows really fetched from kyworks.ky.gov on 2026-07-20, reproducing the
feed's documented quirks (blank County header, Excel date serials,
datetime-mangled NAICS, footer rows, overlap between the current report and
the prior-year archive). Dates map by plain meaning — notice_date from
"Date Received" — deliberately diverging from BLN's swapped field map; see
the ky.py docstring.
"""

import json
import re
from pathlib import Path

import warn_sources
from warn_sources import ky

FIXTURES = Path(__file__).parent / "fixtures"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_fixtures(tmp_path):
    """Consolidate both fixture workbooks and parse, like fetch() would."""
    groups = [
        ky._extract_workbook_rows(FIXTURES / "ky_current_sample.xlsx"),
        ky._extract_workbook_rows(FIXTURES / "ky_prior_sample.xlsx"),
    ]
    raw = tmp_path / "raw_download"
    raw.write_text(json.dumps(ky._consolidate(groups), default=str))
    return warn_sources.get_source("ky", tmp_path).parse(raw)


def test_registry_contains_kentucky():
    assert "ky" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ky"]
    assert cls.enabled
    assert cls.name == "Kentucky"


def test_parse_columns_and_types(tmp_path):
    df = _parse_fixtures(tmp_path)
    assert list(df.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "industry",
    ]
    assert str(df["employees"].dtype) == "int64"
    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO.match(val), f"bad {col}: {val!r}"


def test_parse_drops_headers_footers_and_dedupes_overlap(tmp_path):
    df = _parse_fixtures(tmp_path)
    companies = df["company"].tolist()
    # 3 from the current report + 4 archive rows + serial row + NAICS row,
    # minus the Battelle notice present in both files.
    assert len(df) == 8
    assert "Company: Company Name" not in companies      # header echo
    assert "Total Notice Count" not in companies          # footer
    assert "Bogus Corp After Footer" not in companies     # after footer
    assert companies.count("Battelle Memorial Institute E3") == 1


def test_parse_maps_dates_by_plain_meaning_not_blns_swap(tmp_path):
    """notice_date <- "Date Received", effective_date <- "Projected Date".

    BLN's transformer swaps these two; replicating that put future "notice
    dates" at the top of the national dashboard's newest-first table. The
    module deliberately diverges — see the ky.py docstring.
    """
    df = _parse_fixtures(tmp_path)
    carrier = df[df.company.str.startswith("Carrier")].iloc[0]
    assert carrier["notice_date"] == "2026-06-12"      # Date Received
    assert carrier["effective_date"] == "2026-08-30"   # Projected Date
    assert carrier["layoff_type"] == "Closure"
    assert carrier["county"] == "Simpson"
    assert carrier["industry"] == "333415"


def test_parse_converts_excel_date_serials(tmp_path):
    df = _parse_fixtures(tmp_path)
    led = df[df.company == "LED VANCE LLC"].iloc[0]
    assert led["notice_date"] == "2019-01-25"      # serial 43490 (received)
    assert led["effective_date"] == "2019-09-27"   # serial 43735 (projected)


def test_parse_employee_counts(tmp_path):
    df = _parse_fixtures(tmp_path)
    by_company = df.set_index("company")["employees"]
    assert by_company["Blue Oval SK Group/Battery Plant"] == 1514
    # Feed published no count -> 0, still an int
    key = "Kellanova formerly Kellogg Company - Pikeville Plant"
    assert by_company[key] == 0


def test_parse_recovers_unnamed_county_column(tmp_path):
    df = _parse_fixtures(tmp_path)
    assert df[df.company == "Moveret"].iloc[0]["county"] == "Hardin"


def test_parse_drops_mangled_naics(tmp_path):
    df = _parse_fixtures(tmp_path)
    mine = df[df.company == "THOROUGHFARE MINING LLC"].iloc[0]
    assert mine["industry"] == ""                  # datetime garbage dropped
    assert mine["notice_date"] == "2017-12-18"     # Date Received
    assert mine["employees"] == 99
