"""Tests for the Rhode Island WARN source (warn_sources/ri.py).

The fixture workbook (tests/fixtures/ri_warn_report.xlsx) is a 9-data-row
excerpt of the real DLT "WARN Report" XLSX fetched 2026-07-20, preserving
the feed's quirks: title + repeated header rows, the two company-header
variants, prose employee counts, date ranges, the "2108" typo year,
"Staggered" and "5/4/204" garbage dates, and Covid "*" company markers.
"""

import re
from pathlib import Path

import pytest

import warn_sources

FIXTURE = Path(__file__).parent / "fixtures" / "ri_warn_report.xlsx"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_ri_registered():
    assert "ri" in warn_sources.SOURCES


def test_ri_metadata(tmp_path):
    src = warn_sources.get_source("ri", tmp_path)
    assert src.code == "ri"
    assert src.name == "Rhode Island"
    assert "dlt.ri.gov" in src.source_url
    assert src.paths.root == tmp_path / "states" / "ri"
    assert src.paths.raw.suffix == ".xlsx"  # openpyxl needs the extension


# ---------------------------------------------------------------------------
# Offline parse against the committed fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def parsed(tmp_path):
    src = warn_sources.get_source("ri", tmp_path)
    return src.parse(FIXTURE)


def test_parse_schema(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]
    assert len(parsed) == 9  # junk/title/header rows dropped


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO.match(val), f"{col}: {val!r}"


def test_parse_employees_are_ints(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"].tolist())


def test_parse_junk_rows_dropped(parsed):
    companies = parsed["company"].tolist()
    assert "Rhode Island WARN Report" not in companies
    assert not any("Company Name" in c for c in companies)


def test_parse_quirks(parsed):
    by_company = {r["company"]: r for r in parsed.to_dict("records")}

    # Prose employee counts resolve via the vendored BLN corrections.
    assert by_company["Conduent"]["employees"] == 1  # "1 (Remote worker)"
    ideal = by_company["Ideal US Talent Systems Worker OpCo LLC"]
    assert ideal["employees"] == 2  # "9,891 Remote Workers (2 from RI)"
    assert by_company["CVS Health"]["employees"] == 309  # "309 *updated…"

    # Date ranges keep the first date; typo years are corrected or nulled.
    assert by_company["Allied Group, LLC"]["effective_date"] == "2026-05-25"
    assert by_company["CVS Health"]["effective_date"] == "2023-10-21"
    assert by_company["Matlet/Group"]["notice_date"] == "2018-11-01"  # 2108
    assert by_company["ASM GLOBAL"]["notice_date"] is None  # "5/4/204"
    assert by_company["ASM GLOBAL"]["effective_date"] == "2024-06-30"
    flex = by_company["Flextronics Americas LLC"]
    assert flex["effective_date"] is None  # "Staggered"

    # Covid "*" markers are stripped; whitespace collapsed.
    assert "Santander" in by_company
    assert by_company["CVS"]["city"] == "Woonsocket"

    # Closing Yes/No -> layoff_type; blank when the state left it blank.
    assert by_company["Allied Group, LLC"]["layoff_type"] == "Closure"
    assert by_company["ASM GLOBAL"]["layoff_type"] == "Layoff"
    assert by_company["Conduent"]["layoff_type"] == ""


def test_unify_stamps_state(parsed, tmp_path):
    src = warn_sources.get_source("ri", tmp_path)
    df = src.unify(parsed)
    assert (df["state"] == "RI").all()
    assert (df["county"] == "").all()  # RI publishes no county
