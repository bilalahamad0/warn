"""Tests for the Alaska (AK) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ak

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snippet of https://jobs.alaska.gov/RR/WARN_notices.htm
# (captured 2026-07-21): the real header row, the <hr> separator row, the
# page's commented-out template row, 17 real notice rows spanning 2006-2026
# covering every date/count quirk class, and the real blank trailing row.
FIXTURE = FIXTURES / "ak_notices_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_alaska():
    assert "ak" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ak"]
    assert cls.code == "ak"
    assert cls.name == "Alaska"
    assert cls.source_url == "https://jobs.alaska.gov/RR/WARN_notices.htm"
    assert cls.enabled is True
    assert "ak" in [type(s).code for s in warn_sources.all_sources()]


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ak", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ak"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (header-keyed port of BLN warn-scraper ak.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_html():
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_rows_drops_junk_rows(fixture_html):
    rows = ak._extract_rows(fixture_html)
    # Header keys the dicts; <hr> separator, commented-out template row and
    # the blank trailing row are all dropped.
    assert len(rows) == 17
    assert all("Company" in r for r in rows)
    assert not any(r.get("Company") == "Name" for r in rows)  # template row


def test_extract_rows_first_row_keys(fixture_html):
    first = ak._extract_rows(fixture_html)[0]
    assert first["Company"] == "RNDC Shared Services LLC"
    assert first["Location"] == "Juneau"
    assert first["Notice Date"] == "07/06/26"
    assert first["Layoff Date"] == "09/04/26"
    assert first["Employees Affected"] == "160"
    assert first["Notes"] == ""


def test_extract_rows_collapses_br_inside_cells(fixture_html):
    sams = [
        r for r in ak._extract_rows(fixture_html) if "Club" in r["Company"]
    ][0]
    # "Starting <br > 3/16/18" in the source markup collapses cleanly.
    assert sams["Layoff Date"] == "Starting 3/16/18"


def test_extract_rows_no_table_means_no_rows():
    assert ak._extract_rows("<html><body>moved</body></html>") == []


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_two_and_four_digit_years():
    assert ak._clean_date("07/06/26") == "2026-07-06"
    assert ak._clean_date("2/8/25") == "2025-02-08"
    assert ak._clean_date("12/10/2024") == "2024-12-10"
    assert ak._clean_date("9/5/2023") == "2023-09-05"


def test_clean_date_vendored_corrections():
    assert ak._clean_date("9/30/20*") == "2020-09-30"
    assert ak._clean_date("August-November 2021") == "2021-08-01"
    assert ak._clean_date("4/1/20 5/31/20") == "2020-04-01"
    assert ak._clean_date("March to May 2016") == "2016-03-01"
    assert ak._clean_date("June-August 2023") == "2023-06-01"
    assert ak._clean_date(
        "Begins 7/7/25 and will be staggered until official closure "
        "on 11/30/25"
    ) == "2025-07-07"
    assert ak._clean_date("Varied") is None
    assert ak._clean_date("various") is None


def test_clean_date_range_start_and_starting_prefix():
    # BLN's transform_date cleanup: " to " range keeps the start date,
    # a leading "Starting " is stripped.
    assert ak._clean_date("4/7/20 to 5/31/20") == "2020-04-07"
    assert ak._clean_date("4/22/20 to June 2020") == "2020-04-22"
    assert ak._clean_date("Starting 3/16/18") == "2018-03-16"


def test_clean_date_empty_and_junk_are_none():
    assert ak._clean_date("") is None
    assert ak._clean_date(None) is None
    assert ak._clean_date("garbage") is None
    assert ak._clean_date("01/01/0001") is None  # out-of-window year


def test_clean_employees():
    assert ak._clean_employees("160") == 160
    assert ak._clean_employees("1,234") == 1234
    assert ak._clean_employees("Up to 300") == 300
    assert ak._clean_employees("1 Alaska Worker") == 1
    assert ak._clean_employees("TBA") == 0
    assert ak._clean_employees("") == 0


# ---------------------------------------------------------------------------
# Offline parse against the fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("ak", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    # Only the columns Alaska really publishes — no county, address or
    # industry are ever fabricated.
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]


def test_parse_row_count_and_required_company(parsed):
    assert len(parsed) == 17
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "RNDC Shared Services LLC"]
    assert row["notice_date"].tolist() == ["2026-07-06"]   # Notice Date
    assert row["effective_date"].tolist() == ["2026-09-04"]  # Layoff Date
    assert row["employees"].tolist() == [160]              # Employees Affected
    assert row["city"].tolist() == ["Juneau"]              # Location
    assert row["layoff_type"].tolist() == [""]             # Notes (blank)


def test_parse_notes_become_layoff_type(parsed):
    vigor = parsed[parsed["company"] == "Vigor Alaska, LLC"]
    assert vigor["layoff_type"].tolist() == ["Closure"]
    # The staggered-closure sentence resolves via the vendored correction.
    assert vigor["effective_date"].tolist() == ["2025-07-07"]


def test_parse_jobs_corrections(parsed):
    nfw = parsed[parsed["company"] == "Natural Fiber Welding, Inc."]
    assert nfw["employees"].tolist() == [1]     # "1 Alaska Worker"
    ykhc = parsed[parsed["company"] == "Yukon-Kuskokwim Health Corporation"]
    assert ykhc["employees"].tolist() == [300]  # "Up to 300"
    tba = parsed[parsed["company"] == "American Nursery Services, Inc."]
    assert tba["employees"].tolist() == [0]     # "TBA": no published count
    ravn = parsed[parsed["company"].str.startswith("RavnAir Group")]
    assert ravn["employees"].tolist() == [1234]  # "1,234"


def test_parse_unusable_effective_dates_stay_none(parsed):
    # "Varied", "various" and an empty cell are no usable date — and the
    # notice date is never copied into the effective date.
    for company in (
        "Norcon",
        "United Parcel Services",
        "Agrium U.S. Inc. Kenai Nitrogen Operations",
    ):
        row = parsed[parsed["company"] == company]
        assert row["effective_date"].tolist() == [None]
        assert row["notice_date"].tolist() != [None]


def test_parse_multi_city_notices_keep_joined_text(parsed):
    ravn = parsed[parsed["company"].str.startswith("RavnAir Group")]
    assert ravn["city"].tolist() == [
        "Anchorage and 20 smaller Alaska communities, plus Boston, Mass."
    ]


def test_fetch_rejects_a_collapsed_table(tmp_path, monkeypatch):
    # The fixture's 17 rows sit below MIN_EXPECTED_ROWS (the live page has
    # ~65): fetch must refuse to hand a collapsed page to the diff engine.
    src = warn_sources.get_source("ak", tmp_path)

    def fake_download(force=False, url=None, meta_file=None, local_path=None):
        local_path.write_bytes(FIXTURE.read_bytes())
        return True, str(local_path)

    monkeypatch.setattr(ak.warn_monitor, "download_xlsx", fake_download)
    with pytest.raises(RuntimeError, match="layout may have changed"):
        src.fetch()


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ak", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "AK").all()
    # AK publishes no county, street address, or industry.
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
