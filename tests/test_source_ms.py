"""Tests for the Mississippi (MS) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ms as ms_module

FIXTURE = Path(__file__).parent / "fixtures" / "ms_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_mississippi():
    assert "ms" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ms"]
    assert cls.code == "ms"
    assert cls.name == "Mississippi"
    assert cls.source_url.startswith("https://mdes.ms.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ms", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ms"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture (rows lifted verbatim from the
# consolidated CSV of the 2020-2026 quarterly MDES PDFs)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("ms", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "city",
        "industry",
    ]


def test_parse_drops_junk_rows_and_requires_company(parsed):
    # Fixture has 17 data lines: 15 real notices, one repeated-header
    # line, one blank row — only the real notices survive.
    assert len(parsed) == 15
    assert (parsed["company"].str.strip() != "").all()
    assert "company name" not in set(parsed["company"].str.lower())


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    # BLN jobs_corrections: "TBA" -> no published count -> 0.
    milwaukee = parsed[parsed["company"].str.startswith("Milwaukee Electric")]
    assert milwaukee["employees"].tolist() == [0]


def test_parse_field_crosswalk_follows_bln(parsed):
    row = parsed[parsed["company"].str.startswith("Hollywood Casino")]
    assert row["notice_date"].tolist() == ["2020-07-15"]     # date_notice
    assert row["effective_date"].tolist() == ["2020-09-15"]  # date_effective
    assert row["employees"].tolist() == [15]                 # affected
    assert row["layoff_type"].tolist() == ["Layoff"]         # action_type
    assert row["industry"].tolist() == ["721120 – Casino Hotels"]  # naics


def test_parse_old_quarters_keep_merged_company_column(parsed):
    # Pre-2026 quarters publish one merged "Company Name City (County)"
    # column; BLN maps the whole cell to company — city/county stay
    # empty, never guessed out of the text.
    row = parsed[parsed["company"].str.startswith("Stein Mart")]
    assert row["company"].tolist() == ["Stein Mart Madison (Madison)"]
    assert row["city"].tolist() == [""]
    assert row["county"].tolist() == [""]


def test_parse_new_quarters_have_real_city_and_county(parsed):
    row = parsed[parsed["company"] == "GXO Logistics"]
    assert row["city"].tolist() == ["Southaven"]
    assert row["county"].tolist() == ["DeSoto"]
    assert row["notice_date"].tolist() == ["2026-01-06"]
    assert row["effective_date"].tolist() == ["2026-01-31"]


def test_parse_splits_count_merged_into_action_type(parsed):
    # PY2025 oct-dec quarter merges "# Affected" into the Type of Action
    # cell ("Closure 79"); the published count must not be dropped.
    burgess = parsed[parsed["company"].str.startswith("Burgess-Norton")]
    assert burgess["layoff_type"].tolist() == ["Closure"]
    assert burgess["employees"].tolist() == [79]
    westlake = parsed[parsed["company"].str.startswith("Westlake")]
    assert westlake["layoff_type"].tolist() == ["Closure"]
    assert westlake["employees"].tolist() == [99]
    milwaukee = parsed[parsed["company"].str.startswith("Milwaukee Electric")]
    assert milwaukee["layoff_type"].tolist() == ["Layoff"]


def test_parse_applies_bln_date_corrections(parsed):
    # "10/05/202" -> 2020-10-05 (truncated year, BLN date_corrections)
    mdoc = parsed[parsed["company"].str.startswith("The MS. Department")]
    assert mdoc["effective_date"].tolist() == ["2020-10-05"]
    # "2/2026" -> 2026-02-01
    westlake = parsed[parsed["company"].str.startswith("Westlake")]
    assert westlake["effective_date"].tolist() == ["2026-02-01"]
    # "4/3.2026" -> 2026-04-03
    nike = parsed[parsed["company"].str.startswith("NIKE")]
    assert nike["effective_date"].tolist() == ["2026-04-03"]


def test_parse_unpublished_dates_stay_none(parsed):
    # "Pending"/"TBA" -> None; the notice_date is never copied over.
    stanley = parsed[parsed["company"] == "Stanley Black & Decker"]
    assert stanley["effective_date"].tolist() == [None]
    assert stanley["notice_date"].tolist() == ["2026-02-20"]
    milwaukee = parsed[parsed["company"].str.startswith("Milwaukee Electric")]
    assert milwaukee["effective_date"].tolist() == [None]


def test_parse_two_digit_years_and_verbatim_state_typos(parsed):
    # "11/14/20" parses via BLN's %m/%d/%y.
    services = parsed[parsed["company"].str.startswith("The Services System")]
    assert services["effective_date"].tolist() == ["2020-11-14"]
    # The state published "2/13/2013" verbatim (typo for 2023) in the
    # Jan-Mar 2023 PDF; like BLN, we keep it as published.
    mtool = parsed[parsed["company"].str.startswith("Milwaukee Tool")]
    assert mtool["notice_date"].tolist() == ["2013-02-13"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ms", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "MS").all()
    assert (df["address"] == "").all()  # MS publishes no street address


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------


def test_clean_date_corrections_formats_and_garbage():
    assert ms_module._clean_date("07/15/2020") == "2020-07-15"
    assert ms_module._clean_date("11/14/20") == "2020-11-14"
    assert ms_module._clean_date("10/05/202") == "2020-10-05"
    assert ms_module._clean_date("2/2026") == "2026-02-01"
    assert ms_module._clean_date("Pending") is None
    assert ms_module._clean_date("TBA") is None
    assert ms_module._clean_date("Management Canceled") is None
    assert ms_module._clean_date("") is None
    # Reason text bleeding into the date cell: first token wins.
    assert ms_module._clean_date("6/11/2025 WARN- Due to") == "2025-06-11"


def test_pdf_url_extraction_skips_map_and_dedupes():
    html = """
    <div id="page_content">
      <a href="/media/warn-py2025-qtr-4-apr-jun-2026.pdf">Q4</a>
      <a href="/media/warn-py2025-qtr-3-jan-mar-2026.pdf">Q3</a>
      <a href="/media/warn-py2025-qtr-3-jan-mar-2026.pdf">Q3 again</a>
      <a href="/media/wia-county-map.pdf">County map</a>
      <a href="/other.html">Not a PDF</a>
    </div>
    """
    src = warn_sources.SOURCES["ms"].__new__(warn_sources.SOURCES["ms"])
    urls = src._pdf_urls(html)
    assert urls == [
        "https://mdes.ms.gov/media/warn-py2025-qtr-4-apr-jun-2026.pdf",
        "https://mdes.ms.gov/media/warn-py2025-qtr-3-jan-mar-2026.pdf",
    ]
