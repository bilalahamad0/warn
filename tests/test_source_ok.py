"""Tests for the Oklahoma (OK) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ok as ok_module

FIXTURE = Path(__file__).parent / "fixtures" / "ok_notices_sample.json"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_oklahoma():
    assert "ok" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ok"]
    assert cls.code == "ok"
    assert cls.name == "Oklahoma"
    assert cls.source_url.startswith("https://www.employoklahoma.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ok", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ok"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a fixture of real fetched aura records (11 raw rows)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("ok", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "layoff_type",
        "city",
    ]
    # OK publishes no effective date, county, address, or industry —
    # never fabricated.
    for absent in ("effective_date", "county", "address", "industry"):
        assert absent not in parsed.columns


def test_parse_drops_rows_without_company(parsed):
    # 11 raw rows - 1 with an empty employer name = 10 notices.
    assert len(parsed) == 10
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_always_int_zero(parsed):
    # Oklahoma publishes no employee counts (BLN jobs column always null).
    assert all(isinstance(v, int) for v in parsed["employees"])
    assert (parsed["employees"] == 0).all()


def test_parse_clean_row(parsed):
    row = parsed[parsed["company"] == "Hopkins Mfg- Miami"]
    assert row["notice_date"].tolist() == ["2026-02-23"]
    assert row["employees"].tolist() == [0]
    assert row["layoff_type"].tolist() == ["Plant Closing"]
    assert row["city"].tolist() == ["Miami"]


def test_parse_city_falls_back_to_workforce_board(parsed):
    # BLN transformer quirk, vendored: location = city or workforce_board.
    by_company = parsed.set_index("company")["city"]
    assert by_company["Surgical Specialties"] == "SouthernRegion"
    assert by_company["TTEC"] == "Central"
    assert by_company["HILTI"] == "TULSA"  # real city wins when present
    assert by_company["Lockheed Martin"] == ""  # neither published


def test_parse_layoff_types_carried_verbatim(parsed):
    by_company = parsed.set_index("company")["layoff_type"]
    assert by_company["Macy's Distribution"] == "Plant Closing"
    assert by_company["Sunoco"] == "Mass Layoff"
    assert by_company["Oral Roberts University"] == "Other"


def test_parse_unescapes_entities_and_rejects_absurd_dates(parsed):
    row = parsed[parsed["company"] == "A&B Distributors"]
    assert len(row) == 1
    assert row["notice_date"].tolist() == [None]  # 1899 fails the year guard


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ok", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "OK").all()
    assert df["effective_date"].isna().all()  # never synthesized
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()


# ---------------------------------------------------------------------------
# Helpers (rules vendored from BLN warn-transformer ok.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-02-23", "2026-02-23"),  # feed's native ISO format
        ("2003-07-21", "2003-07-21"),  # genuine pre-2019 history rows
        ("1899-05-05", None),  # below the sanity year floor
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_date(raw, expected):
    assert ok_module._clean_date(raw) == expected


def test_extract_aura_context_from_portal_html():
    html = (
        '<script>var ctx = {"mode":"PROD","fwuid":"ABC123",'
        '"loaded":{"APPLICATION@markup://siteforce:communityApp":"1684_KM"}'
        "};</script>"
    )
    ctx = ok_module._extract_aura_context(html)
    assert ctx["fwuid"] == "ABC123"
    assert ctx["loaded"] == {
        "APPLICATION@markup://siteforce:communityApp": "1684_KM"
    }
    assert ctx["app"] == "siteforce:communityApp"


def test_extract_aura_context_raises_without_fwuid():
    with pytest.raises(RuntimeError):
        ok_module._extract_aura_context("<html>no tokens here</html>")


def test_unwrap_records_raises_on_failure_state():
    with pytest.raises(RuntimeError):
        ok_module._unwrap_records({"actions": [{"state": "ERROR"}]})
    with pytest.raises(RuntimeError):
        ok_module._unwrap_records({"actions": []})
