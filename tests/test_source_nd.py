"""Tests for the North Dakota (ND) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import nd

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snippet of the live "WARN Notices 2015 to present" PDF
# (fetched 2026-07-22): page 1 only, cropped to the header plus the first
# 20 data rows (2015-2017) by deleting every pdfium page object below the
# row-20 rule and shrinking the media box. The table's grid is one path
# object spanning the whole page, so pdfplumber still emits blank phantom
# rows below the cut — the parser must skip those structurally. The rows
# kept include both vendored date corrections that live on page 1
# ("Began 1/10/17", "starts 10/29/2017").
FIX_PDF = FIXTURES / "nd_sample.pdf"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_north_dakota():
    assert "nd" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["nd"]
    assert cls.code == "nd"
    assert cls.name == "North Dakota"
    assert cls.enabled is True
    assert cls.source_url.startswith("https://www.jobsnd.com/")
    assert cls.source_url.endswith(".pdf")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("nd", tmp_path)
    assert src.paths.root == tmp_path / "states" / "nd"
    assert src.paths.latest.name == "warn_latest.json"
    assert src.paths.raw.suffix == ".pdf"


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_live_formats():
    assert nd.NorthDakotaJSND._clean_date("7/31/2015") == "2015-07-31"
    assert nd.NorthDakotaJSND._clean_date("1/5/16") == "2016-01-05"


def test_clean_date_vendored_corrections():
    # BLN warn-transformer date_corrections: double-date cells keep the
    # FIRST date; prefixed cells keep their embedded date.
    assert nd.NorthDakotaJSND._clean_date("3/3/2025 5/2/2025") == "2025-03-03"
    assert nd.NorthDakotaJSND._clean_date("Began 1/10/17") == "2017-01-10"
    assert (
        nd.NorthDakotaJSND._clean_date("starts 10/29/2017") == "2017-10-29"
    )
    assert nd.NorthDakotaJSND._clean_date("Not stated") is None


def test_clean_date_future_multidate_falls_back_to_first_token():
    # Not in the corrections table — the regex fallback keeps date one.
    assert (
        nd.NorthDakotaJSND._clean_date("1/2/2027 3/4/2027") == "2027-01-02"
    )


def test_clean_date_never_emits_junk():
    assert nd.NorthDakotaJSND._clean_date(None) is None
    assert nd.NorthDakotaJSND._clean_date("") is None
    assert nd.NorthDakotaJSND._clean_date("garbage") is None


def test_clean_jobs():
    assert nd.NorthDakotaJSND._clean_jobs("95") == 95
    assert nd.NorthDakotaJSND._clean_jobs("1,204") == 1204
    assert nd.NorthDakotaJSND._clean_jobs("") == 0
    assert nd.NorthDakotaJSND._clean_jobs(None) == 0
    # Vendored BLN jobs_corrections.
    assert nd.NorthDakotaJSND._clean_jobs("25+") == 25
    assert (
        nd.NorthDakotaJSND._clean_jobs(
            "approx. 2200 nationwide (14 reported in ND)"
        )
        == 14
    )


# ---------------------------------------------------------------------------
# Offline parse against the truncated real PDF
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("nd", tmp_path)
    return src.parse(FIX_PDF)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "city",
    ]


def test_parse_drops_header_and_phantom_rows(parsed):
    # 20 data rows survive; the header row and the blank grid rows below
    # the crop are dropped structurally.
    assert len(parsed) == 20
    assert "Company Name" not in parsed["company"].tolist()
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    # First data row of the live PDF, mapped per the BLN crosswalk:
    # Company Name / Location / WARN Dated / Date of Layoff/Closure /
    # Number Laid Off/Affected.
    first = parsed.iloc[0]
    assert first["company"] == "EGS Customer Care Inc"
    assert first["notice_date"] == "2015-07-31"     # WARN Dated
    assert first["effective_date"] == "2015-12-31"  # Date of Layoff/Closure
    assert first["employees"] == 95
    assert first["city"] == "Fargo, ND"             # Location (free text)


def test_parse_dates_never_copied_between_fields(parsed):
    # Baker Hughes filed after the layoff: WARN Dated 10/5/2015 but
    # layoff 9/23/2015 — both must survive verbatim, never synthesized.
    baker = parsed[parsed["company"] == "Baker Hughes"]
    assert baker["notice_date"].tolist() == ["2015-10-05"]
    assert baker["effective_date"].tolist() == ["2015-09-23"]


def test_parse_correction_cells_honored(parsed):
    # Raw cells "Began 1/10/17" and "starts 10/29/2017" are in the
    # vendored corrections table.
    rust = parsed[parsed["company"] == "Rust Contractors Inc"]
    assert rust["effective_date"].tolist() == ["2017-01-10"]
    here = parsed[parsed["company"] == "HERE North America LLC"]
    assert here["effective_date"].tolist() == ["2017-10-29"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("nd", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "ND").all()
    # ND publishes no layoff type, county, street address, or industry.
    assert (df["layoff_type"] == "").all()
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
