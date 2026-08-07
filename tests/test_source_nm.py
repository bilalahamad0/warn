"""Tests for the New Mexico (NM) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import nm

FIXTURES = Path(__file__).parent / "fixtures"

# Consolidated JSON exactly as fetch() writes it: 12 real rows pulled
# from the live 2016-2026 WARN PDFs (2026-07-21) via _extract_pdf_rows,
# spanning every BLN quirk — "1/0/00", "July-September 2025",
# "July - August 2026", "Not Disclosed"/"?"/"N/A" counts, and all four
# date formats — plus one all-blank padding row.
FIX_JSON = FIXTURES / "nm_sample.json"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_new_mexico():
    assert "nm" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["nm"]
    assert cls.code == "nm"
    assert cls.name == "New Mexico"
    assert cls.enabled is True
    assert cls.source_url.startswith("https://www.dws.state.nm.us/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("nm", tmp_path)
    assert src.paths.root == tmp_path / "states" / "nm"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_all_four_live_formats():
    assert nm._clean_date("9-Jan-2020") == "2020-01-09"    # %d-%b-%Y
    assert nm._clean_date("05-Jan-17") == "2017-01-05"     # %d-%b-%y
    assert nm._clean_date("1/19/2016") == "2016-01-19"     # %m/%d/%Y
    assert nm._clean_date("4/27/26") == "2026-04-27"       # %m/%d/%y


def test_clean_date_two_digit_year_falls_through_wide_formats():
    # %d-%b-%Y parses "17" as year 17; the port must fall through to
    # %d-%b-%y instead of emitting None (or year 0017).
    assert nm._clean_date("29-Jul-16") == "2016-07-29"


def test_clean_date_vendored_corrections():
    assert nm._clean_date("1/0/00") is None                # BLN correction
    assert nm._clean_date("July-September 2025") == "2025-07-15"


def test_clean_date_month_range_rule():
    # Live 2026 phased layoff; same convention as BLN's 2025 correction.
    assert nm._clean_date("July - August 2026") == "2026-07-15"
    assert nm._clean_date("July-September\n2025") == "2025-07-15"


def test_clean_date_never_emits_junk():
    assert nm._clean_date("") is None
    assert nm._clean_date(None) is None
    assert nm._clean_date("garbage") is None
    assert nm._clean_date("Nope - Whatever 2026") is None


def test_clean_employees():
    assert nm._clean_employees("412") == 412
    assert nm._clean_employees("1,204") == 1204
    assert nm._clean_employees("") == 0
    # Vendored BLN jobs_corrections — all three live in the PDFs.
    assert nm._clean_employees("Not Disclosed") == 0
    assert nm._clean_employees("?") == 0
    assert nm._clean_employees("N/A") == 0


# ---------------------------------------------------------------------------
# Offline parse against the consolidated JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("nm", tmp_path)
    return src.parse(FIX_JSON)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "county",
        "city",
    ]


def test_parse_drops_padding_rows(parsed):
    assert len(parsed) == 12  # 13 fixture rows minus the blank one
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Sitel"]
    assert row["notice_date"].tolist() == ["2017-01-05"]     # NOTICE DATE
    assert row["effective_date"].tolist() == ["2017-03-26"]  # LAYOFF DATE
    assert row["employees"].tolist() == [412]  # TOTAL LAYOFF NUMBER
    assert row["county"].tolist() == ["Doña Ana"]
    assert row["city"].tolist() == ["Las Cruces"]


def test_parse_received_date_never_copied_into_notice_date(parsed):
    # Smith's: NOTICE DATE 1/19/2016 but RECEIVED DATE 12/2/2015 — the
    # BLN crosswalk maps NOTICE DATE only.
    smiths = parsed[parsed["company"] == "Smith's Food & Drug Store"]
    assert smiths["notice_date"].tolist() == ["2016-01-19"]
    assert "2015-12-02" not in parsed["notice_date"].tolist()


def test_parse_corrected_notice_date_is_none_not_junk(parsed):
    # David's Bridal notice date is the source's literal "1/0/00".
    davids = parsed[parsed["company"] == "David's Bridal"]
    assert davids["notice_date"].tolist() == [None]
    assert davids["effective_date"].tolist() == ["2023-04-14"]
    assert davids["employees"].tolist() == [0]  # "N/A"


def test_parse_month_range_layoff_dates(parsed):
    intel = parsed[parsed["company"] == "Intel"]
    assert intel["effective_date"].tolist() == ["2025-07-15"]
    conduent = parsed[parsed["company"] == "Conduent"]
    assert conduent["effective_date"].tolist() == ["2026-07-15"]


def test_parse_undisclosed_counts_are_zero(parsed):
    williams = parsed[parsed["company"] == "Williams"]
    assert williams["employees"].tolist() == [0]  # "Not Disclosed"
    devon = parsed[parsed["company"] == "Devon Energy Corp"]
    assert devon["employees"].tolist() == [0]  # "?"


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("nm", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "NM").all()
    # NM publishes no layoff type, street address, or industry.
    assert (df["layoff_type"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
