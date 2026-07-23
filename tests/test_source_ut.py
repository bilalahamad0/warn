"""Tests for the Utah (UT) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ut

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snippet of
# https://jobs.utah.gov/employer/business/warnnotices.html (captured
# 2026-07-22): the 2026 table's real header row + 6 real notice rows,
# plus a second table of real rows from the page's per-year tables
# covering every date/count quirk in BLN's UT transformer.
FIXTURE = FIXTURES / "ut_notices_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_utah():
    assert "ut" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ut"]
    assert cls.code == "ut"
    assert cls.name == "Utah"
    assert cls.source_url == (
        "https://jobs.utah.gov/employer/business/warnnotices.html"
    )
    assert cls.enabled is True
    assert "ut" in [type(s).code for s in warn_sources.all_sources()]


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ut", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ut"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (header-keyed port of BLN warn-scraper ut.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_html():
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_rows_all_tables_header_keyed(fixture_html):
    rows = ut._extract_rows(fixture_html)
    # Both per-year tables are scraped; each header row keys its own
    # table's dicts and is not emitted as data.
    assert len(rows) == 15
    assert all("Company Name" in r for r in rows)
    assert not any(r["Company Name"] == "Company Name" for r in rows)


def test_extract_rows_first_row_cells(fixture_html):
    first = ut._extract_rows(fixture_html)[0]
    assert first["Date of Notice"] == "10/30/26"
    assert first["Company Name"] == "Point Designs"
    assert first["Location"] == "Bountiful"
    assert first["Affected Workers"] == "8"


def test_extract_rows_no_table_means_no_rows():
    assert ut._extract_rows("<html><body>moved</body></html>") == []


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_two_and_four_digit_years():
    # BLN date_format ("%m/%d/%Y", "%m/%d/%y") under last-match-wins:
    # 2-digit years must land in this century, not year 25.
    assert ut._clean_date("10/30/26") == "2026-10-30"
    assert ut._clean_date("12/05/25") == "2025-12-05"
    assert ut._clean_date("12/23/2022") == "2022-12-23"
    assert ut._clean_date("2/4/2020") == "2020-02-04"


def test_clean_date_vendored_corrections():
    assert ut._clean_date("03/09/2020&") == "2020-03-09"
    assert ut._clean_date("01/05/18/") == "2018-01-05"
    assert ut._clean_date("03/05/14 Updated") == "2014-03-05"
    assert ut._clean_date("09/31/10") == "2010-09-30"  # impossible Sep 31
    assert ut._clean_date("05/2009") == "2009-05-01"
    assert ut._clean_date("01/07//09") == "2009-01-07"
    assert ut._clean_date("08/31//2022") == "2022-08-31"


def test_clean_date_empty_and_junk_are_none():
    assert ut._clean_date("") is None
    assert ut._clean_date(None) is None
    assert ut._clean_date("garbage") is None
    assert ut._clean_date("01/01/0001") is None  # out-of-window year


def test_clean_employees():
    assert ut._clean_employees("8") == 8
    assert ut._clean_employees("1,234") == 1234
    assert ut._clean_employees("645 Revised") == 645  # vendored correction
    assert ut._clean_employees("") == 0
    assert ut._clean_employees("TBD") == 0


# ---------------------------------------------------------------------------
# Offline parse against the fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("ut", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    # Only the columns Utah really publishes — no effective date, county,
    # address, industry, or layoff type are ever fabricated.
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "city",
    ]


def test_parse_row_count_and_required_company(parsed):
    assert len(parsed) == 15
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Provo Canyon School"]
    assert row["notice_date"].tolist() == ["2026-07-09"]  # Date of Notice
    assert row["employees"].tolist() == [303]             # Affected Workers
    assert row["city"].tolist() == ["Provo and Springville"]  # Location


def test_parse_quirk_rows_resolve_via_corrections(parsed):
    gd = parsed[parsed["company"] == "General Dynamics IT"]
    assert gd["notice_date"].tolist() == ["2014-03-05"]  # "03/05/14 Updated"
    assert gd["employees"].tolist() == [645]             # "645 Revised"
    dn = parsed[parsed["company"] == "Deseret News Publishing Co."]
    assert dn["notice_date"].tolist() == ["2010-09-30"]  # impossible Sep 31
    sa = parsed[parsed["company"] == "Spring Air"]
    assert sa["notice_date"].tolist() == ["2009-05-01"]  # month-only cell
    for company in ("DDSC, INC", "Medly Health Inc"):
        row = parsed[parsed["company"] == company]
        assert row["notice_date"].tolist() == ["2022-08-31"]  # "08/31//2022"


def test_fetch_rejects_a_collapsed_table(tmp_path, monkeypatch):
    # The fixture's 15 rows sit far below MIN_EXPECTED_ROWS (the live page
    # has ~280): fetch must refuse to hand a collapsed page to the diff
    # engine.
    src = warn_sources.get_source("ut", tmp_path)

    def fake_download(force=False, url=None, meta_file=None, local_path=None):
        local_path.write_bytes(FIXTURE.read_bytes())
        return True, str(local_path)

    monkeypatch.setattr(ut.warn_monitor, "download_xlsx", fake_download)
    with pytest.raises(RuntimeError, match="layout may have changed"):
        src.fetch()


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ut", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "UT").all()
    # UT publishes notice_date only — effective_date is never fabricated.
    assert df["effective_date"].isna().all()
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
    assert (df["layoff_type"] == "").all()
