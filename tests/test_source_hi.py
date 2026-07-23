"""Tests for the Hawaii (HI) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import hi as hi_module

# Truncated real snippets of the per-year WDC news pages (fetched
# 2026-07-21): 2021 = one-paragraph-per-notice format, incl. the He-Man
# date typo, asterisked amendment annotations, and the .docx annotation;
# 2019 = the grouped single-paragraph <br>-separated format.
FIXTURE_2021 = Path(__file__).parent / "fixtures" / "hi_2021_sample.html"
FIXTURE_2019 = Path(__file__).parent / "fixtures" / "hi_2019_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_hawaii():
    assert "hi" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["hi"]
    assert cls.code == "hi"
    assert cls.name == "Hawaii"
    assert cls.source_url.startswith("https://labor.hawaii.gov")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("hi", tmp_path)
    assert src.paths.root == tmp_path / "states" / "hi"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline fetch-shape + parse against the real-data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("hi", tmp_path)
    src.paths.ensure()
    rows = hi_module._extract_rows(FIXTURE_2021.read_text(), "2021")
    hi_module._write_raw_csv(rows, src.paths.raw)
    return src.parse(src.paths.raw)


def test_parse_columns(parsed):
    assert list(parsed.columns) == ["company", "notice_date", "employees"]


def test_parse_drops_annotation_rows_and_requires_company(parsed):
    # The fixture's seven extracted rows include three asterisked
    # amendment annotations whose text sits wholly inside the <a>
    # (Company empty, exactly as BLN emits them) — dropped.
    assert len(parsed) == 4
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int_zero(parsed):
    # HI publishes no employee counts on the listing.
    assert all(isinstance(v, int) for v in parsed["employees"])
    assert (parsed["employees"] == 0).all()


def test_parse_field_crosswalk_follows_bln(parsed):
    flying = parsed[parsed["company"] == "Flying Food Group, LLC"]
    # Two real filings kept; the "*... Amended 10/12/2021" annotation is
    # a separate (dropped) row, never merged into these.
    assert flying["notice_date"].tolist() == ["2021-08-05", "2021-10-05"]


def test_parse_heman_date_typo_correction_vendored(parsed):
    # "September 10. 2021" (period for comma) fails strptime; BLN's
    # date_corrections entry is vendored verbatim.
    heman = parsed[parsed["company"] == "He-Man Landscaping, LLC"]
    assert heman["notice_date"].tolist() == ["2021-09-21"]


def test_extract_rows_handles_grouped_br_format():
    rows = hi_module._extract_rows(FIXTURE_2019.read_text(), "2019")
    assert [r["Company"] for r in rows] == [
        "Maui Seaside Hotel",
        "Kai Management Services",
        "Payless Shoesource",
        "Tony Hawaii Automotive Group",
    ]
    assert [r["Date"] for r in rows] == [
        "2019-01-16", "2019-01-23", "2019-02-25", "2019-02-28",
    ]


def test_extract_rows_skips_non_pdf_annotation():
    # The 2021 page links one annotation to a .docx; BLN's a[href*=pdf]
    # selector skips it and so does the port.
    rows = hi_module._extract_rows(FIXTURE_2021.read_text(), "2021")
    assert not any("Anheuser-Busch" in r["Company"] and "email" in r["Company"]
                   for r in rows)


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("hi", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "HI").all()
    # HI publishes no effective date, city, or county — never fabricated.
    assert df["effective_date"].isna().all()
    assert (df["city"] == "").all()
    assert (df["county"] == "").all()


# ---------------------------------------------------------------------------
# Scrape/transform rules (vendored from BLN warn-scraper/-transformer)
# ---------------------------------------------------------------------------


def test_clean_date_corrections_vendored_from_bln():
    assert hi_module._clean_date("*Hawaiian Airlines Amendment") is None
    assert hi_module._clean_date(
        "* Hawaiian Airlines Amended September 16, 2020"
    ) == "2020-09-16"
    assert hi_module._clean_date(
        "*Flying Food Group, LLC Amended 10/12/2021"
    ) == "2021-10-12"
    assert hi_module._clean_date(
        "February 7, 2024 –   Ginshari, Inc. – KuruKuru Sushi"
    ) == "2025-02-07"
    assert hi_module._clean_date("September 10. 2021") == "2021-09-21"


def test_clean_date_iso_passthrough_and_junk():
    assert hi_module._clean_date("2026-06-30") == "2026-06-30"
    assert hi_module._clean_date("June 30, 2026") == "2026-06-30"
    assert hi_module._clean_date("") is None
    assert hi_module._clean_date(None) is None
    assert hi_module._clean_date("no date here") is None
    assert hi_module._clean_date("January 1, 1900") is None  # typo window
    assert hi_module._clean_date("3021-01-01") is None       # typo window


def test_year_page_urls_filters_to_year_notice_pages():
    html = (
        '<div id="container_main"><p>'
        '<a href="https://labor.hawaii.gov/wdc/2026-warn-notices/">2026</a> '
        '<a href="https://labor.hawaii.gov/wdc/2025-warn-notices/">2025</a> '
        '<a href="https://labor.hawaii.gov/wdc/contact/">Contact us</a>'
        "</p></div>"
        '<div id="nav"><a href="https://labor.hawaii.gov/wdc/2019-warn-'
        'notices/">outside container</a></div>'
    )
    assert hi_module._year_page_urls(html) == [
        "https://labor.hawaii.gov/wdc/2026-warn-notices/",
        "https://labor.hawaii.gov/wdc/2025-warn-notices/",
    ]
    assert hi_module._year_page_urls("<p>no container</p>") == []
