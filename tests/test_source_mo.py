"""Tests for the Missouri (MO) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import mo

FIXTURES = Path(__file__).parent / "fixtures"

# Real capture of the full https://jobs.mo.gov/warn/2026 table
# (2026-07-21): 22 data rows + the page's own totals row, current
# 10-column layout (Industry + Notes).
FIX_2026 = FIXTURES / "mo_2026_sample.html"

# Truncated real snippet of the legacy 8-column /warn/2019 layout plus
# the real Welk Resorts row (/warn/2020) carrying the source's
# "03/20/0202" year typo, and the page totals row.
FIX_2019 = FIXTURES / "mo_2019_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_missouri():
    assert "mo" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["mo"]
    assert cls.code == "mo"
    assert cls.name == "Missouri"
    assert cls.source_url.startswith("https://jobs.mo.gov/warn")


def test_missouri_is_disabled_behind_the_bot_wall():
    # jobs.mo.gov serves an Incapsula JS challenge to non-browser clients;
    # the source ships disabled until a run environment passes the wall.
    assert warn_sources.SOURCES["mo"].enabled is False
    codes = [type(s).code for s in warn_sources.all_sources()]
    assert "mo" not in codes


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("mo", tmp_path)
    assert src.paths.root == tmp_path / "states" / "mo"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (header-keyed port of BLN warn-scraper mo.py)
# ---------------------------------------------------------------------------


def test_extract_rows_current_layout():
    rows = mo._extract_rows(FIX_2026.read_text(encoding="utf-8"))
    assert len(rows) == 23  # 22 notices + the page totals row
    first = rows[0]
    # Sort-widget caption stripped from the header key.
    assert "Received" in first and "Received Sort descending" not in first
    assert first["Received"] == "01/19/2026"
    assert first["Title"] == "TelaForce, LLC"
    assert first["Industry"] == "Business Services"
    assert first["Type"] == "Loss of Contract"
    # Totals row: no Title, only the year's affected-employees sum.
    assert rows[-1]["Title"] == ""
    assert rows[-1]["# affected"] == "2511"


def test_extract_rows_legacy_8_column_layout():
    rows = mo._extract_rows(FIX_2019.read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert "Industry" not in rows[0]  # 2019 pages predate the column
    assert rows[0]["Title"] == "Beauty Brands"
    # Amended notice: original + revision date + "rev" marker.
    assert rows[3]["Received"] == "10/02/2019 10/30/2020 rev"


def test_extract_rows_no_table_means_empty_year():
    assert mo._extract_rows("<html><body>WARN Notices</body></html>") == []


def test_challenge_detection():
    stub = (
        '<html><head><script src="/_Incapsula_Resource?SWJIYLWA=x">'
        "</script></head><body></body></html>"
    )
    assert mo._is_challenge(stub)
    # Genuine pages reference the Incapsula script too, but always carry
    # the "WARN Notices" page title — even years with no notices.
    real = FIX_2026.read_text(encoding="utf-8")
    assert not mo._is_challenge("<title>WARN Notices | JobsMoGov</title>" + real)


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_first_date_wins():
    # Amended Received cells and phased Layoff cells keep the first date.
    assert mo._clean_date("10/02/2019 10/30/2020 rev") == "2019-10-02"
    assert mo._clean_date("05/06/2026 05/31/2026") == "2026-05-06"


def test_clean_date_vendored_corrections():
    assert mo._clean_date("March 2020") == "2020-03-01"
    assert mo._clean_date("11/08/2109") == "2019-11-08"
    assert mo._clean_date("04/-9/2020") == "2020-04-09"
    assert mo._clean_date("03/20/0202") == "2020-03-20"  # live-feed typo
    assert mo._clean_date("") is None
    assert mo._clean_date(None) is None


def test_clean_date_never_emits_junk_years():
    assert mo._clean_date("01/01/0001") is None
    assert mo._clean_date("garbage") is None


def test_clean_employees():
    assert mo._clean_employees("104") == 104
    assert mo._clean_employees("1,204") == 1204
    assert mo._clean_employees("") == 0
    assert mo._clean_employees("Unknown") == 0
    assert mo._clean_employees("330 remote workers (18 located in Missouri)") == 18


# ---------------------------------------------------------------------------
# Offline parse against the consolidated JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_json(tmp_path):
    rows = []
    for year, fixture in ((2026, FIX_2026), (2019, FIX_2019)):
        for row in mo._extract_rows(fixture.read_text(encoding="utf-8")):
            rows.append({"Year": year, **row})
    path = tmp_path / "raw_download"
    path.write_text(json.dumps({"source": mo.BASE_URL, "rows": rows}))
    return path


@pytest.fixture
def parsed(tmp_path, raw_json):
    src = warn_sources.get_source("mo", tmp_path)
    return src.parse(raw_json)


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


def test_parse_drops_totals_rows(parsed):
    assert len(parsed) == 26  # 22 real 2026 rows + 4 from the 2019 snippet
    assert (parsed["company"].str.strip() != "").all()
    assert "2511" not in set(parsed["employees"].astype(str))


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "TelaForce, LLC"]
    assert row["notice_date"].tolist() == ["2026-01-19"]    # Received
    assert row["effective_date"].tolist() == ["2026-03-20"]  # Layoff date(s)
    assert row["employees"].tolist() == [104]                # # affected
    assert row["layoff_type"].tolist() == ["Loss of Contract"]
    assert row["county"].tolist() == ["Jackson"]
    assert row["city"].tolist() == ["Kansas City"]
    assert row["industry"].tolist() == ["Business Services"]


def test_parse_never_copies_one_date_into_the_other(parsed):
    # Every MO notice publishes distinct received vs. layoff dates.
    assert (parsed["notice_date"] != parsed["effective_date"]).all()


def test_parse_amended_notice_keeps_original_received_date(parsed):
    cerner = parsed[parsed["company"] == "Cerner Corporation"]
    assert cerner["notice_date"].tolist() == ["2019-10-02"]  # not the rev
    assert cerner["effective_date"].tolist() == ["2019-12-13"]
    assert cerner["city"].tolist() == [""]  # multi-location: no city given


def test_parse_phased_layoff_keeps_first_date(parsed):
    sheraton = parsed[parsed["company"].str.startswith("Sheraton Clayton")]
    assert sheraton["effective_date"].tolist() == ["2019-10-01"]
    saks = parsed[parsed["company"] == "Saks & Company LLC"]
    assert saks["effective_date"].tolist() == ["2026-05-06"]


def test_parse_corrects_the_live_year_typo(parsed):
    welk = parsed[parsed["company"] == "Welk Resorts"]
    assert welk["notice_date"].tolist() == ["2020-03-31"]
    assert welk["effective_date"].tolist() == ["2020-03-20"]  # 03/20/0202


def test_parse_legacy_rows_have_empty_industry(parsed):
    beauty = parsed[parsed["company"] == "Beauty Brands"]
    assert beauty["industry"].tolist() == [""]
    assert beauty["layoff_type"].tolist() == ["Closing"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("mo", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "MO").all()
    assert (df["address"] == "").all()  # MO publishes no street address
