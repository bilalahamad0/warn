"""Tests for the Louisiana WARN source (warn_sources/la.py)."""

import re
from pathlib import Path

import warn_sources
from warn_sources.la import (
    LouisianaLWC,
    _clean_date,
    _clean_employees,
    _rows_from_tables,
    _split_company_address,
)

FIXTURE = Path(__file__).parent / "fixtures" / "la_notices_sample.json"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

COLUMNS = [
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "address",
    "industry",
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_la():
    assert "la" in warn_sources.SOURCES
    src = warn_sources.get_source("la")
    assert isinstance(src, LouisianaLWC)
    assert src.code == "la"
    assert src.name == "Louisiana"
    assert src.paths.root.name == "la"


# ---------------------------------------------------------------------------
# Offline parse against a real feed sample
# (tests/fixtures/la_notices_sample.json — 9 raw table rows cut from a live
#  2026-07-22 crawl: six from the five-column 2025 PDF, three from the
#  six-column 2026 PDF)
# ---------------------------------------------------------------------------


def test_parse_fixture_schema_and_types(tmp_path):
    df = LouisianaLWC(tmp_path).parse(FIXTURE)

    assert list(df.columns) == COLUMNS
    assert len(df) == 9
    assert df["company"].str.len().gt(0).all()

    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO_DATE.match(val), f"{col}: {val!r}"

    assert all(type(v) is int for v in df["employees"])
    assert (df["employees"] >= 0).all()


def test_parse_fixture_field_mapping(tmp_path):
    df = LouisianaLWC(tmp_path).parse(FIXTURE)
    rows = {r["company"]: r for r in df.to_dict("records")}

    # Five-column layout: address split out of the Company Name cell at
    # the first line starting with a digit; both date formats honored
    # (Notice Date %m/%d/%y here, Layoff Date %m/%d/%y) and never
    # copied into each other.
    reddy = rows["Dr. Reddy’s Laboratories"]
    assert reddy["notice_date"] == "2025-01-07"
    assert reddy["effective_date"] == "2025-03-14"
    assert reddy["employees"] == 107
    assert reddy["address"] == "8800 Line Ave, Shreveport, LA 71106"
    assert reddy["industry"] == "Manufacturing"

    # Line-wrapped company names joined with a space, not a comma.
    gdit = rows["General Dynamics Information Technology"]
    assert gdit["address"] == "2251 Lakeshore Drive, New Orleans"

    # No line starts with a digit -> first line is the company, the rest
    # is the address (BLN fallback rule).
    idea = rows["IDEA Southern Louisiana"]
    assert idea["employees"] == 212
    assert idea["address"].startswith("Idea Innovation, (7800 Innovation")
    assert ",," not in idea["address"]

    # Phased layoff window "7/31/25 to 12/31/25" keeps its start date.
    cornerstone = rows["Cornerstone Chemical Company"]
    assert cornerstone["effective_date"] == "2025-07-31"

    # A later-rescinded notice stays exactly as the state publishes it.
    ups = rows["(*)UPS"]
    assert ups["employees"] == 177
    assert ups["notice_date"] == "2025-07-10"

    # Facility designation lands in address — BLN quirk kept as-is.
    premier = rows["Premier Health Consultants, LLC"]
    assert premier["address"] == "(Premier Health Urgent Care)"

    # Six-column layout: dedicated Address column; line-wrapped industry
    # cells squished to one line.
    westlake = rows["Westlake Corporation"]
    assert westlake["address"] == "330 Belden Street, Lake Charles, LA 70601"
    assert westlake["industry"] == "Petrochemical Manufacturing"
    assert westlake["notice_date"] == "2025-12-15"
    assert westlake["effective_date"] == "2026-02-14"
    assert westlake["employees"] == 121

    # The rows behind BLN's camelot artifacts ("601"->date, "3/16/26"->
    # 101 jobs) parse cleanly here — real values, corrections unneeded.
    mcglinchey = rows["McGlinchey Stafford PLLC"]
    assert mcglinchey["effective_date"] == "2026-03-16"
    assert mcglinchey["employees"] == 101
    assert mcglinchey["address"] == (
        "601 Poydras Street, Suite 1200, New Orleans, LA 70130"
    )

    denka = rows["Denka Performance Elastomer LLC"]
    assert denka["effective_date"] == "2026-04-01"
    assert denka["employees"] == 45


# ---------------------------------------------------------------------------
# Transform quirks (vendored BLN corrections + cleanup chain)
# ---------------------------------------------------------------------------


def test_clean_date_quirks():
    assert _clean_date("1/7/25") == "2025-01-07"          # %m/%d/%y
    assert _clean_date("3/14/2025") == "2025-03-14"       # %m/%d/%Y
    assert _clean_date("12/31/25") == "2025-12-31"        # BLN correction
    assert _clean_date("6/31/09") == "2009-06-30"         # BLN correction
    assert _clean_date("5/1820") == "2020-05-18"          # BLN correction
    assert _clean_date("N/A") is None
    assert _clean_date("Various") is None
    # Cleanup chain: range separators keep the start date
    assert _clean_date("7/31/25 to 12/31/25") == "2025-07-31"
    assert _clean_date("7/31/25\nto\n12/31/25") == "2025-07-31"
    assert _clean_date("1/5/26 - 3/1/26") == "2026-01-05"
    # "Starting" stripped case-insensitively before the retry
    assert _clean_date("Starting 8/21/23") == "2023-08-21"
    # First-token split -> "Not" correction -> None
    assert _clean_date("Not specified") is None
    assert _clean_date("") is None
    assert _clean_date(None) is None
    # Implausible years degrade to None, never junk
    assert _clean_date("1/1/1901") is None


def test_clean_employees_quirks():
    assert _clean_employees("107") == 107
    assert _clean_employees("1,250") == 1250
    assert _clean_employees("50-297") == 50               # BLN correction
    assert _clean_employees("1*") == 1                    # BLN correction
    assert _clean_employees("TBD") == 0                   # no count -> 0
    assert _clean_employees("NA") == 0
    assert _clean_employees("") == 0
    assert _clean_employees("unknown") == 0


def test_split_company_address():
    company, address = _split_company_address(
        "Dr. Reddy’s Laboratories\n8800 Line Ave\nShreveport, LA 71106"
    )
    assert company == "Dr. Reddy’s Laboratories"
    assert address == "8800 Line Ave, Shreveport, LA 71106"

    # Wrapped company name: joined with a space, address starts at the
    # first digit-leading line.
    company, address = _split_company_address(
        "General Dynamics Information\nTechnology\n2251 Lakeshore Drive"
    )
    assert company == "General Dynamics Information Technology"
    assert address == "2251 Lakeshore Drive"

    # No digit-leading line: first line company, rest address.
    company, address = _split_company_address(
        "Premier Health Consultants, LLC\n(Premier Health Urgent Care)"
    )
    assert company == "Premier Health Consultants, LLC"
    assert address == "(Premier Health Urgent Care)"

    assert _split_company_address("") == ("", "")


def test_rows_from_tables_headers_and_footnotes():
    tables = [
        [
            ["Company Name", "Notice Date", "Layoff Date",
             "Employees Affected", "Industry"],
            ["Acme Co\n1 Main St", "1/7/25", "3/14/25", "10", "Retail"],
            # Footnote fragment (<= 2 populated cells) is dropped
            ["(*)UPS notice was Rescinded on 9/5/2025", "", "", "", ""],
            # Repeated header on a later page is swallowed
            ["Company Name", "Notice Date", "Layoff Date",
             "Employees Affected", "Industry"],
            ["Beta LLC\n2 Oak Ave", "2/1/25", "4/1/25", "20", "Energy"],
        ]
    ]
    rows = _rows_from_tables(tables, context="unit-test")
    assert len(rows) == 2
    assert rows[0]["company_original"] == "Acme Co\n1 Main St"
    assert rows[0]["date_notice"] == "1/7/25"
    assert rows[0]["date_action"] == "3/14/25"
    assert rows[0]["affected"] == "10"
    assert rows[1]["industry"] == "Energy"


def test_rows_from_tables_requires_known_header():
    import pytest

    with pytest.raises(RuntimeError):
        _rows_from_tables(
            [[["Mystery Column", "Other"], ["data", "row"]]],
            context="unit-test",
        )
