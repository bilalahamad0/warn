"""Tests for the Vermont (VT) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import vt

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snippet of the live 2026 date-range search results page
# (captured 2026-07-21): the six-column results table only.
FIX_SEARCH = FIXTURES / "vt_search_2026.html"

# Truncated real snippet of detail page /search/warn_lookups/357
# (Calmont Beverage, captured 2026-07-21): the definition list only.
FIX_DETAIL = FIXTURES / "vt_detail_357.html"

# Consolidated CSV exactly as fetch() writes it. Rows 1-10 are real
# fetched 2019-2026 notices (including the quoted-comma company names,
# the "; "-joined addresses, Big Lots' out-of-state HQ city, and the
# 2019/2020 rows that publish no city/address at all). Rows 11-15 are
# edge cases in the portal's own formats: the 9999999 unknown-count
# sentinel (BLN jobs_corrections), a blank count, a >10000 overflow
# count, a header-echo row, and a row with no employer.
FIX_CSV = FIXTURES / "vt_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_vermont():
    assert "vt" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["vt"]
    assert cls.code == "vt"
    assert cls.name == "Vermont"
    assert cls.enabled is True
    assert cls.source_url == (
        "https://www.vermontjoblink.com/search/warn_lookups"
    )


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("vt", tmp_path)
    assert src.paths.root == tmp_path / "states" / "vt"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (vendored port of BLN job_center site.py)
# ---------------------------------------------------------------------------


def test_parse_search_rows_real_snippet():
    rows = vt._parse_search_rows(FIX_SEARCH.read_text(encoding="utf-8"))
    assert len(rows) == 6  # the six real 2026 notices
    first = rows[0]
    assert first["employer"] == "Calmont Beverage"
    assert first["city"] == "Barre"
    assert first["zip"] == "05641"
    assert first["notice_date"] == "Jan 22, 2026"
    assert first["warn_type"] == "WARN"
    assert first["record_number"] == "357"
    assert first["detail_path"].endswith("/warn_lookups/357")


def test_parse_search_rows_empty_year():
    html = "<html><body>no matches for your search results</body></html>"
    assert vt._parse_search_rows(html) == []


def test_parse_search_rows_layout_drift_raises():
    with pytest.raises(ValueError):
        vt._parse_search_rows("<html><body><p>hello</p></body></html>")


def test_parse_detail_real_snippet():
    detail = vt._parse_detail(FIX_DETAIL.read_text(encoding="utf-8"))
    assert detail["number_of_employees_affected"] == "67"
    # Multi-line street address collapsed to one "; "-joined line.
    assert detail["address"] == "308 Industrial Lane; Barre, Vermont 05641"


def test_next_page_link():
    assert vt._next_page_link("<html><body></body></html>") is None
    html = '<a class="next_page" href="/search/warn_lookups?page=2">2</a>'
    assert vt._next_page_link(html) == (
        "https://www.vermontjoblink.com/search/warn_lookups?page=2"
    )


def test_search_params_date_range():
    params = vt._search_params("2019-01-01", "2019-12-31")
    assert params["q[notice_on_gteq]"] == "2019-01-01"
    assert params["q[notice_on_lteq]"] == "2019-12-31"
    assert params["commit"] == "Search"


# ---------------------------------------------------------------------------
# Cleaning helpers (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_portal_format():
    # BLN warn-transformer vt.py: date_format = "%b %d, %Y".
    assert vt.VermontJobLink._clean_date("Jan 22, 2026") == "2026-01-22"
    assert vt.VermontJobLink._clean_date("Nov 05, 2019") == "2019-11-05"
    assert vt.VermontJobLink._clean_date("") is None
    assert vt.VermontJobLink._clean_date(None) is None
    assert vt.VermontJobLink._clean_date("garbage") is None


def test_clean_field_unescapes_and_collapses():
    assert vt._clean_field("A &amp; B\n  Cafe ") == "A & B Cafe"
    assert vt._clean_field(None) == ""


# ---------------------------------------------------------------------------
# Offline parse against the consolidated CSV exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("vt", tmp_path)
    return src.parse(FIX_CSV)


def test_parse_columns(parsed):
    # VT publishes no effective date (JobLink), county, industry, or
    # closure/layoff taxonomy — only what the state really publishes.
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "city",
        "address",
    ]


def test_parse_drops_junk_rows(parsed):
    # 13 usable rows: header echo + missing-employer rows are dropped.
    assert len(parsed) == 13
    assert (parsed["company"].str.strip() != "").all()
    assert "Employer" not in set(parsed["company"])


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Calmont Beverage"]
    assert row["notice_date"].tolist() == ["2026-01-22"]
    assert row["employees"].tolist() == [67]
    assert row["city"].tolist() == ["Barre"]
    assert row["address"].tolist() == [
        "308 Industrial Lane; Barre, Vermont 05641"
    ]


def test_parse_quoted_comma_company(parsed):
    assert "St. Albans Creamery, LLC" in set(parsed["company"])


def test_parse_rows_without_city_or_address(parsed):
    # 2019/2020 notices publish neither city nor address.
    spire = parsed[parsed["company"] == "Spire Hospitality/Top Notch Resort"]
    assert spire["city"].tolist() == [""]
    assert spire["address"].tolist() == [""]
    assert spire["employees"].tolist() == [126]
    assert spire["notice_date"].tolist() == ["2020-03-20"]


def test_parse_unknown_count_sentinel_is_zero(parsed):
    # BLN warn-transformer vt.py jobs_corrections: {9999999: None}.
    row = parsed[parsed["company"] == "Edge Case Sentinel Co"]
    assert row["employees"].tolist() == [0]


def test_parse_blank_count_is_zero(parsed):
    row = parsed[parsed["company"] == "Edge Case Blank Count Co"]
    assert row["employees"].tolist() == [0]


def test_parse_overflow_count_is_zero(parsed):
    # BLN BaseTransformer maximum_jobs=10000 sanity cap.
    row = parsed[parsed["company"] == "Edge Case Overflow Co"]
    assert row["employees"].tolist() == [0]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("vt", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "VT").all()
    # Never synthesized: JobLink publishes no effective date.
    assert df["effective_date"].isna().all()
    assert (df["county"] == "").all()
    assert (df["industry"] == "").all()
