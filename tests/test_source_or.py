"""Tests for the Oregon (OR) WARN source."""

import importlib
import re
from pathlib import Path

import pytest
from openpyxl import Workbook

import warn_sources

# "or" is a Python keyword, so the module is reached via importlib.
or_module = importlib.import_module("warn_sources.or")

FIXTURE = Path(__file__).parent / "fixtures" / "or_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_oregon():
    assert "or" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["or"]
    assert cls.code == "or"
    assert cls.name == "Oregon"
    assert cls.source_url.startswith("https://ccwd.hecc.oregon.gov")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("or", tmp_path)
    assert src.paths.root == tmp_path / "states" / "or"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("or", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]


def test_parse_drops_companyless_rows_and_requires_company(parsed):
    # Fixture has 18 data rows, one of which has a blank company.
    assert len(parsed) == 17
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    boise = parsed[parsed["company"].str.startswith("BOISE CASCADE")]
    assert boise["employees"].tolist() == [0]  # state published no count


def test_parse_nulls_the_1899_excel_epoch_sentinel(parsed):
    sugar = parsed[parsed["company"].str.startswith("THE AMALGAMATED")]
    assert sugar["effective_date"].tolist() == [None]
    assert sugar["notice_date"].tolist() == ["2005-01-19"]  # kept, not copied


def test_parse_field_crosswalk_follows_bln(parsed):
    row = parsed[parsed["company"] == "Conduent - Oregon Remote Employees"]
    assert row["notice_date"].tolist() == ["2026-06-26"]      # Received Date
    assert row["effective_date"].tolist() == ["2026-08-28"]   # Layoff Date
    assert row["employees"].tolist() == [17]                  # Laid Off
    assert row["layoff_type"].tolist() == [
        "Large Layoff - 10 or more workers"
    ]


def test_parse_cleans_whitespace_and_trailing_commas(parsed):
    assert "TRUS JOIST - A WEYERHAEUSER BUSINESS" in set(parsed["company"])
    adventist = parsed[parsed["company"] == "Adventist Health Portland"]
    assert adventist["city"].tolist() == ["Portland"]
    # Out-of-state HQ filings keep their "City, ST" form.
    assert "Mesa, AZ" in set(parsed["city"])


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("or", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "OR").all()
    assert (df["county"] == "").all()  # OR publishes no county


# ---------------------------------------------------------------------------
# Workbook extraction (layout vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def _build_ccwd_workbook(path, data_rows):
    wb = Workbook()
    ws = wb.active
    ws.append([None, "Worker Adjustment and Retraining Notification (WARN)"])
    ws.append([None, "WARNs Received: 7/20/2016 to 7/20/2026"])
    ws.append(
        [
            "WARN#",
            "Company Name",
            "Location",
            "Layoff Date",
            "Laid Off",
            "Layoff Type",
            "Received Date",
        ]
    )
    for row in data_rows:
        ws.append(row)
    wb.save(path)


def test_extract_rows_skips_title_and_blank_rows(tmp_path):
    xlsx = tmp_path / "sample.xlsx"
    _build_ccwd_workbook(
        xlsx,
        [
            ["9001", "Acme Mill", "Salem", None, 25, "Reduction", None],
            ["", "", "", "", "", "", ""],  # trailing filler
        ],
    )
    rows = or_module._extract_rows(xlsx)
    assert len(rows) == 1
    assert rows[0]["Company Name"] == "Acme Mill"
    assert rows[0]["Laid Off"] == "25"


def test_extract_rows_rejects_unexpected_headers(tmp_path):
    wb = Workbook()
    wb.active.append(["totally", "different", "sheet"])
    xlsx = tmp_path / "bad.xlsx"
    wb.save(xlsx)
    with pytest.raises(ValueError):
        or_module._extract_rows(xlsx)
