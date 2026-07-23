"""Tests for the Virginia (VA) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import va as va_module

FIXTURE = Path(__file__).parent / "fixtures" / "va_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_virginia():
    assert "va" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["va"]
    assert cls.code == "va"
    assert cls.name == "Virginia"
    assert cls.source_url.startswith("https://virginiaworks.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("va", tmp_path)
    assert src.paths.root == tmp_path / "states" / "va"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a 13-row fixture of real feed rows
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("va", tmp_path)
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
    # VA publishes no county, street address, or industry; "Contact
    # Person" / "Collective Bargaining Unit" have no unified field.
    for absent in ("county", "address", "industry", "Contact Person"):
        assert absent not in parsed.columns


def test_parse_row_count(parsed):
    assert len(parsed) == 13
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    assert parsed["employees"].sum() == 1359  # fixture total, all published


def test_parse_clean_row(parsed):
    row = parsed[parsed["company"] == "Emerson"]
    assert row["notice_date"].tolist() == ["2026-07-09"]
    assert row["effective_date"].tolist() == ["2026-09-30"]
    assert row["employees"].tolist() == [139]
    assert row["layoff_type"].tolist() == ["Closure"]
    # Feed's double space ("Charlottesville  VA") collapses; " VA" strips.
    assert row["city"].tolist() == ["Charlottesville"]


def test_parse_unescapes_html_entities(parsed):
    # The CSV export carries "T&amp;H Services LLC".
    assert "T&H Services LLC" in set(parsed["company"])
    row = parsed[parsed["company"] == "Medical Faculty Associates, Inc. (MFA)"]
    if row.empty:  # feed writes it without the comma
        row = parsed[parsed["company"] == "Medical Faculty Associates Inc. (MFA)"]
    assert row["city"].tolist() == ["Alexandria, Arlington, Ashburn & Reston"]


def test_parse_applies_bln_1973_date_correction(parsed):
    # BLN warn-transformer date_corrections: "10/01/1973" -> None.
    row = parsed[parsed["company"] == "Yoga Works Inc."]
    assert row["notice_date"].tolist() == ["2020-07-14"]
    assert row["effective_date"].tolist() == [None]


def test_parse_location_variants(parsed):
    def city_of(company, **extra):
        rows = parsed[parsed["company"] == company]
        for col, val in extra.items():
            rows = rows[rows[col] == val]
        assert len(rows) == 1, company
        return rows["city"].tolist()[0]

    assert city_of("Conduent Commercial Solutions LLC") == "VA-Statewide"
    # Doubled state suffix "Sandston, VA VA" -> "Sandston".
    assert city_of("LL Flooring", layoff_type="Closure") == "Sandston"
    # Bare " VA" location -> empty, never a fabricated city.
    assert city_of("PAE Shared Services LLC") == ""
    # Out-of-state suffixes stay verbatim.
    assert city_of("Kmart") == "Hoffman Estates IL"
    assert city_of("American Eagle") == "Washington DC"


def test_parse_layoff_type_variants(parsed):
    types = dict(zip(parsed["company"], parsed["layoff_type"]))
    assert types["Management Science for Health (MSH)"] == ""  # blank in feed
    assert types["American Eagle"] == "Permanent Reduction"
    # The feed runs multi-type values together; kept verbatim.
    combined = parsed[parsed["layoff_type"] == "ClosureLayoff"]
    assert combined["company"].tolist() == ["LL Flooring"]
    assert combined["city"].tolist() == ["Richmond"]


def test_parse_strips_company_whitespace(parsed):
    # Feed publishes "General Dynamics Information Technology " (trailing sp).
    assert "General Dynamics Information Technology" in set(parsed["company"])


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("va", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "VA").all()
    for col in ("county", "address", "industry"):
        assert (df[col] == "").all()


# ---------------------------------------------------------------------------
# Cleaning helpers (rules vendored from BLN warn-transformer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("07/09/2026", "2026-07-09"),
        ("1/5/2020", "2020-01-05"),
        ("10/01/1973", None),  # vendored BLN date correction
        ("", None),
        (None, None),
        ("TBD", None),
    ],
)
def test_clean_date(raw, expected):
    assert va_module._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Charlottesville  VA", "Charlottesville"),
        ("Clintwood &amp; Nora VA", "Clintwood & Nora"),
        ("VA-Statewide VA", "VA-Statewide"),
        ("Sandston, VA VA", "Sandston"),
        (" VA", ""),
        ("Hoffman Estates IL", "Hoffman Estates IL"),
        ("Washington, DC DC", "Washington, DC DC"),
        ("Pittsylvania County VA", "Pittsylvania County"),
    ],
)
def test_clean_location(raw, expected):
    assert va_module._clean_location(raw) == expected
