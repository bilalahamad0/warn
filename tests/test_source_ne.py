"""Tests for the Nebraska (NE) WARN source."""

import json
import re
from pathlib import Path

import warn_sources
from warn_sources import ne as ne_module

# Truncated real snippets of the three NE DOL table layouts
# (fetched 2026-07-22): the live "active" page (4 columns, no City),
# the WARN Report archive (5 columns) and the Layoff and Closures
# Report archive (6 columns, incl. Type).
FIXTURES = Path(__file__).parent / "fixtures"
ACTIVE = FIXTURES / "ne_active_sample.html"
WARN_2019 = FIXTURES / "ne_warn_2019_sample.html"
LAYOFF_2019 = FIXTURES / "ne_layoff_2019_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_nebraska():
    assert "ne" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ne"]
    assert cls.code == "ne"
    assert cls.name == "Nebraska"
    assert cls.source_url.startswith("https://dol.nebraska.gov")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ne", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ne"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against the real-data fixtures
# ---------------------------------------------------------------------------


def _consolidated_raw(tmp_path):
    """Mirror fetch(): three pages -> one consolidated raw JSON."""
    rows = []
    for report, fixture in (
        ("active", ACTIVE),
        ("warn", WARN_2019),
        ("layoff", LAYOFF_2019),
    ):
        page_rows = ne_module._extract_rows(fixture.read_text())
        assert page_rows is not None
        for row in page_rows:
            rows.append({"report": report, **row})
    raw = tmp_path / "raw_download"
    raw.write_text(json.dumps({"source": "fixture", "rows": rows}))
    return raw


def _parsed(tmp_path):
    src = warn_sources.get_source("ne", tmp_path)
    return src, src.parse(_consolidated_raw(tmp_path))


def test_parse_columns(tmp_path):
    _, df = _parsed(tmp_path)
    assert list(df.columns) == [
        "company",
        "notice_date",
        "employees",
        "layoff_type",
        "city",
    ]


def test_parse_keeps_all_data_rows_and_requires_company(tmp_path):
    _, df = _parsed(tmp_path)
    assert len(df) == 14  # 5 active + 4 warn + 5 layoff, no junk rows
    assert (df["company"].str.strip() != "").all()


def test_parse_dates_are_iso(tmp_path):
    _, df = _parsed(tmp_path)
    for val in df["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(tmp_path):
    _, df = _parsed(tmp_path)
    assert all(isinstance(v, int) for v in df["employees"])


def test_parse_field_crosswalk_follows_bln(tmp_path):
    _, df = _parsed(tmp_path)
    # Active page: Date -> notice_date, Jobs Affected -> employees, and
    # its Location column carries the city text (no City column there).
    conduent = df[df["company"] == "Conduent"]
    assert conduent["notice_date"].tolist() == ["2026-06-26"]
    assert conduent["employees"].tolist() == [2]
    assert conduent["city"].tolist() == ["Remote"]
    assert conduent["layoff_type"].tolist() == [""]
    # Archive tables: City column feeds city (BLN location="City");
    # their facility-descriptor Location column is dropped.
    fargo = df[df["company"] == "Fargo Assembly of PA, Inc."]
    assert fargo["notice_date"].tolist() == ["2019-07-15"]
    assert fargo["employees"].tolist() == [186]
    assert fargo["city"].tolist() == ["David City"]
    # Layoff report only: Type -> layoff_type, as published.
    jack = df[df["company"] == "Jack Link's Jerky"]
    assert jack["layoff_type"].tolist() == ["Closure"]
    assert jack["employees"].tolist() == [62]


def test_parse_missing_count_becomes_zero(tmp_path):
    # Lazzari's Pizza's Jobs Affected cell is genuinely blank on the
    # 2019 layoff report — the state published no count.
    _, df = _parsed(tmp_path)
    lazzari = df[df["company"] == "Lazzari's Pizza"]
    assert lazzari["employees"].tolist() == [0]


def test_unify_never_fabricates_missing_fields(tmp_path):
    # NE publishes no effective date, county, address or industry.
    src, df = _parsed(tmp_path)
    unified = src.unify(df)
    assert list(unified.columns) == warn_sources.UNIFIED_FIELDS
    assert (unified["state"] == "NE").all()
    assert unified["effective_date"].isna().all()
    assert (unified["county"] == "").all()
    assert (unified["address"] == "").all()
    assert (unified["industry"] == "").all()


# ---------------------------------------------------------------------------
# Scraping + cleaning rules (vendored from BLN warn-scraper/-transformer)
# ---------------------------------------------------------------------------


def test_extract_rows_signals_missing_table():
    assert ne_module._extract_rows("<html><body>maintenance</body>") is None


def test_extract_rows_skips_decoration_and_headerless_rows():
    # Real archive pages open with title/print decoration th-rows before
    # the true column-header row; rows before any header are dropped.
    rows = ne_module._extract_rows(WARN_2019.read_text())
    assert len(rows) == 4
    assert set(rows[0]) == {"Date", "Company", "Jobs Affected", "City", "Location"}


def test_date_corrections_vendored_from_bln():
    # The amended two-date cell resolves to the second listed date.
    assert (
        ne_module._clean_date("12/19/2022\xa0\xa0\n\xa0 11/2/2022") == "2022-11-02"
    )
    assert ne_module._clean_date("04/25/25") == "2025-04-25"
    assert ne_module._clean_date("8/2/2019") == "2019-08-02"
    # Generic multi-date fallback keeps the last m/d/Y token.
    assert ne_module._clean_date("1/2/2024 3/4/2024") == "2024-03-04"
    assert ne_module._clean_date("6/1/1901") is None  # typo window
    assert ne_module._clean_date("") is None
    assert ne_module._clean_date(None) is None


def test_jobs_corrections_vendored_from_bln():
    assert ne_module._clean_employees("100+") == 100
    assert ne_module._clean_employees("5-9") == 5
    assert ne_module._clean_employees("3-5") == 3
    assert ne_module._clean_employees("a few") == 1
    assert ne_module._clean_employees("218") == 218
    assert ne_module._clean_employees("") == 0
    assert ne_module._clean_employees("unknown") == 0
