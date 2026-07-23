"""Tests for the Minnesota (MN) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import mn

FIXTURES = Path(__file__).parent / "fixtures"

# 17 real rows lifted verbatim from the consolidated CSV that fetch()
# built out of the live DEED PDFs on 2026-07-22 (2021 annual through the
# January 2026 monthly). The set covers every era and quirk: YES rows
# with a real "WARN Received" date, "-"/"TBD" null markers, the Jan 2023
# vintage that publishes no Layoff Type column, the Jan 2025 vintage
# whose "-" received-marker bleeds into the Type column ("- Closing"),
# comma thousands ("1,100"), and Aversion/Retention rows with no start
# date or headcount.
FIX_CSV = FIXTURES / "mn_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_minnesota():
    assert "mn" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["mn"]
    assert cls.code == "mn"
    assert cls.name == "Minnesota"
    assert cls.enabled is True
    assert cls.cadence == "monthly"
    assert cls.source_url.startswith("https://mn.gov/deed/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("mn", tmp_path)
    assert src.paths.root == tmp_path / "states" / "mn"
    assert src.paths.latest.name == "warn_latest.json"
    assert src.paths.raw.name == "raw_download.csv"


# ---------------------------------------------------------------------------
# Catalog helpers (file discovery / period classification)
# ---------------------------------------------------------------------------


def test_file_period_monthly_variants():
    # month-after-year, hyphenless, and regular slugs all classify.
    assert mn._file_period(
        "plant-closing-mass-layoff-warn-2026-january_tcm1045-722872.pdf"
    ) == (2026, 1)
    assert mn._file_period("plant-closing-may2022_tcm1045-530080.pdf") == (
        2022,
        5,
    )
    assert mn._file_period(
        "plant-closing-mass-layoff-warn-october-2025_tcm1045-712065.pdf"
    ) == (2025, 10)


def test_file_period_annual_and_junk():
    assert mn._file_period(
        "plant-closing-mass-layoff-2021_tcm1045-515051.pdf"
    ) == (2021, None)
    assert mn._file_period("some-other-asset.pdf") is None


def test_group_period():
    assert mn._group_period("RR Start Date: June 2021 (8 records)") == (
        "2021-06"
    )
    assert mn._group_period("Emily's Bakery & Deli 2021 Hastings") == ""


# ---------------------------------------------------------------------------
# Cell cleaning
# ---------------------------------------------------------------------------


def test_clean_date():
    assert mn.MinnesotaDEED._clean_date("1/25/2021") == "2021-01-25"
    assert mn.MinnesotaDEED._clean_date("12/03/24") == "2024-12-03"
    # A stray word pushed in from the neighboring Industry column
    # (real Oct 2023 cell) still yields its date token.
    assert mn.MinnesotaDEED._clean_date("Assist 11/1/23") == "2023-11-01"
    for null in ("", "-", "TBD", "tbd", "N/A", None, "garbage"):
        assert mn.MinnesotaDEED._clean_date(null) is None, null
    # WARN-flag vintages (Dec 2023 / Jul 2025) put YES/NO here.
    for flag in ("YES", "NO", "TRUE", "FALSE"):
        assert mn.MinnesotaDEED._clean_date(flag) is None, flag
    # Year-less dates ("11/3") must stay None, never guess a year.
    assert mn.MinnesotaDEED._clean_date("11/3") is None


def test_clean_jobs():
    assert mn.MinnesotaDEED._clean_jobs("361") == 361
    assert mn.MinnesotaDEED._clean_jobs("1,100") == 1100
    for null in ("", "-", "TBD", None):
        assert mn.MinnesotaDEED._clean_jobs(null) == 0, null


def test_clean_type_strips_received_null_marker():
    assert mn.MinnesotaDEED._clean_type("- Closing") == "Closing"
    assert mn.MinnesotaDEED._clean_type("Closing") == "Closing"
    assert mn.MinnesotaDEED._clean_type("Workforce Reduction") == (
        "Workforce Reduction"
    )
    assert mn.MinnesotaDEED._clean_type("") == ""


# ---------------------------------------------------------------------------
# Offline parse against real consolidated-CSV rows
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("mn", tmp_path)
    return src.parse(FIX_CSV)


def test_parse_columns(parsed):
    # Only fields Minnesota really publishes: no county, no address.
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
        "industry",
    ]


def test_parse_keeps_every_company_row(parsed):
    assert len(parsed) == 17
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    # WARN Received -> notice_date, Layoff Start -> effective_date; the
    # two differ and must never be copied into each other.
    row = parsed[parsed["company"] == "Christopher & Banks 2021"].iloc[0]
    assert row["notice_date"] == "2021-01-25"
    assert row["effective_date"] == "2021-01-01"
    assert row["employees"] == 361
    assert row["layoff_type"] == "Closing"
    assert row["city"] == "Plymouth"
    assert row["industry"] == "Retail"


def test_parse_null_markers_stay_null(parsed):
    # "-" WARN Received on a non-WARN row -> no notice_date fabricated.
    godiva = parsed[
        parsed["company"] == "Godiva Chocolatier Inc-Minnetonka"
    ].iloc[0]
    assert godiva["notice_date"] is None
    assert godiva["effective_date"] == "2021-02-15"
    # "-" start and "TBD" workers -> None / 0.
    artic = parsed[
        parsed["company"] == "Artic Cat/Textron-St Cloud 2025"
    ].iloc[0]
    assert artic["notice_date"] is None
    assert artic["effective_date"] is None
    assert artic["employees"] == 0
    # Aversion/Retention row: DEED publishes neither start nor count.
    mayo = parsed[parsed["company"] == "Mayo"].iloc[0]
    assert mayo["effective_date"] is None
    assert mayo["employees"] == 0
    assert mayo["city"] == "Owatonna"


def test_parse_type_column_era_quirks(parsed):
    # Jan 2025 vintage: the received-cell "-" bleeds into Type.
    plains = parsed[parsed["company"] == "Green Plains- Fairmont 2024"]
    assert plains["layoff_type"].tolist() == ["Closing"]
    assert plains["notice_date"].tolist() == [None]
    # Jan 2023 vintage publishes no Layoff Type column at all.
    amazon = parsed[parsed["company"] == "Amazon Warehouse-Shakopee 2023"]
    assert amazon["layoff_type"].tolist() == [""]
    assert amazon["employees"].tolist() == [680]


def test_parse_comma_thousands(parsed):
    assert parsed[parsed["company"] == "3M- HQ 2023"][
        "employees"
    ].tolist() == [1100]
    assert parsed[parsed["company"] == "HyLife Foods Windom 2023"][
        "employees"
    ].tolist() == [1007]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("mn", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "MN").all()
    # Minnesota publishes no county or street address.
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
