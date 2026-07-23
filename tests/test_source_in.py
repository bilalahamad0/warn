"""Tests for the Indiana (IN) WARN source."""

import importlib
import re
from pathlib import Path

import pytest

import warn_sources

# ``in`` is a Python keyword, so the module comes in through importlib —
# exactly the route the registry itself uses.
in_module = importlib.import_module("warn_sources.in")

FIXTURE = Path(__file__).parent / "fixtures" / "in_notices_sample.json"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_indiana():
    assert "in" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["in"]
    assert cls.code == "in"
    assert cls.name == "Indiana"
    assert cls.source_url.startswith("https://www.in.gov/dwd/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("in", tmp_path)
    assert src.paths.root == tmp_path / "states" / "in"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a fixture of real fetched rows (19 raw rows)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("in", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
        "industry",
    ]
    # IN publishes no county and no street address — never fabricated.
    assert "county" not in parsed.columns
    assert "address" not in parsed.columns


def test_parse_drops_header_and_revision_rows(parsed):
    # 19 raw rows - header - one all-N/A revision marker = 17 notices.
    assert len(parsed) == 17
    assert "Company" not in set(parsed["company"])
    assert not parsed["company"].str.contains("DFA Dairy Brands").any()
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_clean_row(parsed):
    row = parsed[parsed["company"] == "Thyssenkrupp Presta North America"]
    assert row["notice_date"].tolist() == ["2026-06-30"]
    assert row["effective_date"].tolist() == ["2027-03-31"]
    assert row["employees"].tolist() == [207]
    assert row["layoff_type"].tolist() == ["Closure"]
    assert row["city"].tolist() == ["Terre Haute"]
    assert row["industry"].tolist() == ["Manufacturing"]


def test_parse_applies_bln_jobs_corrections(parsed):
    # Per BLN warn-transformer jobs_corrections, vendored.
    by_company = parsed.set_index("company")["employees"]
    assert by_company["Subaru of Indiana Automotive, Inc."] == 0  # Entire Plant
    assert by_company["Polaris Boats, LLC"] == 100  # "100+"
    assert by_company["Xanodyne Pharmaceuticals, Inc."] == 4  # "4 Hoosiers"
    unknown = "Bay Valley Foods LLC, Tree House Foods Occupations Affected"
    assert by_company[unknown] == 0  # "Unknown"


def test_parse_strips_markup_leak_from_cells(parsed):
    # The live page leaks "/td>" into some cells (BLN scraper quirk).
    row = parsed[parsed["company"] == "Polaris Boats, LLC"]
    assert row["industry"].tolist() == ["Ship Building and Repairing"]


def test_parse_messy_effective_dates(parsed):
    by_company = parsed.set_index("company")["effective_date"]
    assert by_company["Textron Specialized Vehicles"] == "2019-01-01"  # "2019"
    # "December 2020" (month-name format)
    assert by_company["Tenneco (formerly Federal Mogul)"] == "2020-12-01"
    # BLN date_corrections one-offs
    assert by_company["Avis Budget Car Rental, LLC"] == "2020-04-01"
    georgia = "Georgia Gulf Corporation (Royal Outdoor Products facility)"
    assert by_company[georgia] == "2012-02-14"  # "Mid February 2012"
    assert by_company["ATI Alleheny Ludlum"] == "2008-08-01"
    us_steel = "United States Steel Corporation Gary Works Plant"
    assert by_company[us_steel] == "2015-05-27"


def test_parse_range_dates_resolve_to_first_date(parsed):
    by_company = parsed.set_index("company")["effective_date"]
    # "9/22/2014-12/7/2014" and "11/21/2010 - 3/15/2011"
    assert by_company["Talon Logistics"] == "2014-09-22"
    assert by_company["Xanodyne Pharmaceuticals, Inc."] == "2010-11-21"
    # "10/30/2017 & 6/30/2018"
    bay = "Bay Valley Foods LLC, Tree House Foods Occupations Affected"
    assert by_company[bay] == "2017-10-30"


def test_parse_decodes_notice_type_legend(parsed):
    by_company = parsed.set_index("company")["layoff_type"]
    margaret = "Margaret Mary Community Hospital (dba Margaret Mary Health)"
    assert by_company[margaret] == "Transfer"  # TR
    assert by_company["Navient Solutions LLC"] == "Layoff"  # L/O
    pf = (
        "PF Chang's China Bistro "
        "(Indianapolis 1, Indianapolis 2, Fort Wayne)"
    )
    assert by_company[pf] == "Reduction in Hours"  # RH


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("in", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "IN").all()
    assert (df["county"] == "").all()  # IN publishes no county
    assert (df["address"] == "").all()


# ---------------------------------------------------------------------------
# Date / jobs cleaning (rules vendored from BLN warn-transformer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("6/30/2026", "2026-06-30"),
        ("6/30/26", "2026-06-30"),
        ("March 2024", "2024-03-01"),
        ("Mar 2024", "2024-03-01"),
        ("2024", "2024-01-01"),
        ("3/2024", "2024-03-01"),
        ("5/29/2009 to 12/1/2009", "2009-05-29"),
        ("01/30/1202", "2012-01-30"),  # vendored BLN correction
        ("4th Qtr 2012", "2012-09-01"),  # vendored BLN correction
        ("TBD", None),
        ("Unknown", None),
        ("N/A", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_date(raw, expected):
    assert in_module._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("207", 207),
        ("100+", 100),  # vendored BLN correction
        ("40-50", 40),
        ("Entire Plant", 0),
        ("All", 0),
        ("Unknown", 0),
        ("NA", 0),
        ("", 0),
    ],
)
def test_clean_employees(raw, expected):
    assert in_module._clean_employees(raw) == expected
