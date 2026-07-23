"""Tests for the Pennsylvania (PA) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import pa as pa_module

FIXTURE = Path(__file__).parent / "fixtures" / "pa_notices_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_pennsylvania():
    assert "pa" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["pa"]
    assert cls.code == "pa"
    assert cls.name == "Pennsylvania"
    assert cls.source_url.startswith("https://www.pa.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("pa", tmp_path)
    assert src.paths.root == tmp_path / "states" / "pa"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a truncated real-page fixture (7 notices)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("pa", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "address",
    ]
    # PA publishes no notice date, city, or industry — never fabricated.
    assert "notice_date" not in parsed.columns


def test_parse_counts_leaves_not_containers(parsed):
    # The fixture's month accordion (a container item) is not a notice.
    assert len(parsed) == 7
    assert "July" not in set(parsed["company"])
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["effective_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_clean_row(parsed):
    row = parsed[parsed["company"] == "Transform Warehouse Operations, LLC"]
    assert row["effective_date"].tolist() == ["2026-09-16"]
    assert row["employees"].tolist() == [147]
    assert row["county"].tolist() == ["Bucks"]
    # The page litters the tag with zero-width spaces; they must be gone.
    assert row["layoff_type"].tolist() == ["Closing"]
    assert row["address"].tolist() == ["1 Kresge Road, Fairless Hills, PA 19030"]


def test_parse_strips_thousands_separator_and_folds_addresses(parsed):
    row = parsed[parsed["company"] == "JBS Souderton"]
    assert row["employees"].tolist() == [1485]  # "1,485" on the page
    assert row["address"].tolist() == [
        "249 Allentown Road, Souderton, PA  18964, "
        "741 Souder Road, Souderton, PA  18964"
    ]


def test_parse_applies_bln_jobs_corrections(parsed):
    # "5 (within PA)" -> 5, per BLN warn-transformer jobs_corrections.
    row = parsed[parsed["company"] == "Vestis Services, LLC"]
    assert row["employees"].tolist() == [5]


def test_parse_unknown_count_becomes_zero(parsed):
    row = parsed[parsed["company"] == "Starbucks"]
    assert row["employees"].tolist() == [0]
    assert row["effective_date"].tolist() == ["2025-12-05"]


def test_parse_range_dates_resolve_to_first_date(parsed):
    # "beginning 6/4/2026; ending 6/30/26" -> the beginning date.
    row = parsed[parsed["company"] == "American Expediting Logistics, LLC"]
    assert row["effective_date"].tolist() == ["2026-06-04"]


def test_parse_effective_dates_and_counties_variant_keys(parsed):
    row = parsed[parsed["company"] == "Carson Valley Children's Aid"]
    assert row["effective_date"].tolist() == ["2025-09-01"]
    assert row["county"].tolist() == ["Montgomery and Philadelphia"]
    assert row["employees"].tolist() == [96]


def test_parse_notice_without_detail_keys_survives(parsed):
    # Everett Foodliner publishes free text with no KEY: lines (a quirk BLN
    # documents); the row keeps its company and gets safe empty fields.
    row = parsed[parsed["company"].str.startswith("Everett Foodliner")]
    assert len(row) == 1
    assert row["employees"].tolist() == [0]
    assert row["effective_date"].tolist() == [None]
    assert row["county"].tolist() == [""]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("pa", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "PA").all()
    assert df["notice_date"].isna().all()  # PA publishes no notice date
    assert (df["city"] == "").all()


# ---------------------------------------------------------------------------
# Date / jobs cleaning (rules vendored from BLN warn-transformer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9/16/26", "2026-09-16"),
        ("12/5/2025", "2025-12-05"),
        ("beginning 3/1/2026; ending 6/30/2026", "2026-03-01"),
        ("2/17/2025 through 3/3/2025", "2025-02-17"),
        ("November 3, 2023", "2023-11-03"),
        ("Phase 1: 4/14", "2023-04-14"),  # vendored BLN correction
        ("7/3/20223 - 10/16/2023", "2023-07-03"),  # source-page year typo
        ("Unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_date(raw, expected):
    assert pa_module._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("147", 147),
        ("1,485", 1485),
        ("60 total", 60),
        ("Unknown", None),
        ("TBD", None),
        ("72 (54 PA residents impacted)", 54),  # PA residents beat totals
        ("9,236 Nationwide; PA total pending verification", None),
        ("", None),
    ],
)
def test_clean_jobs(raw, expected):
    assert pa_module._clean_jobs(raw) == expected
