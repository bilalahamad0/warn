"""Tests for the Idaho (ID) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources.id import IdahoDOL

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real snippet of the live "Idaho WARN Notices" PDF (fetched
# 2026-07-22): page 1 only, cropped to the header plus the first 12 data
# rows by dropping every content-stream object below the row-12 rule and
# shrinking the media box. The rows kept include the quirk cells the
# vendored BLN transforms must handle: an out-of-state HQ filing with
# "(1 in ID)" for the count (Conduent) and a phased layoff whose
# effective cell jams three dates together (LA Semiconductor).
FIX_PDF = FIXTURES / "id_sample.pdf"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_idaho():
    assert "id" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["id"]
    assert cls.code == "id"
    assert cls.name == "Idaho"
    assert cls.enabled is True
    # The triple-s "businesss" is the state's real URL, not a typo.
    assert cls.source_url == (
        "https://www.labor.idaho.gov/businesss/layoff-assistance/"
    )


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("id", tmp_path)
    assert src.paths.root == tmp_path / "states" / "id"
    assert src.paths.latest.name == "warn_latest.json"
    assert src.paths.raw.suffix == ".pdf"


# ---------------------------------------------------------------------------
# PDF link discovery on the landing page
# ---------------------------------------------------------------------------


def test_find_pdf_url_prefers_warn_pdf_anchor():
    html = """
    <div>
      <a href="/businesses/layoff-assistance/services/">Services</a>
      <a href="https://www.dol.gov/agencies/eta/layoffs/warn">Federal WARN</a>
      <a href="/wp-content/uploads/2026/07/Idaho-WARN-Notices-7.9.26.pdf">
        Idaho WARN notices</a>
      <a href="/contact-nav/">Contact us</a>
    </div>
    <h2>Contact</h2>
    <a href="/footer/">Footer link past the Contact heading</a>
    """
    assert IdahoDOL._find_pdf_url(html) == (
        "https://www.labor.idaho.gov"
        "/wp-content/uploads/2026/07/Idaho-WARN-Notices-7.9.26.pdf"
    )


def test_find_pdf_url_falls_back_to_last_anchor_before_contact():
    # BLN heuristic: with no WARN-labelled PDF anchor, the last link
    # before the Contact heading wins; absolute URLs pass through.
    html = """
    <a href="/somewhere/">elsewhere</a>
    <a href="https://www.labor.idaho.gov/uploads/notices.pdf">list</a>
    <h2>Contact us</h2>
    """
    assert IdahoDOL._find_pdf_url(html) == (
        "https://www.labor.idaho.gov/uploads/notices.pdf"
    )


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_live_formats():
    assert IdahoDOL._clean_date("7/1/2026") == "2026-07-01"
    assert IdahoDOL._clean_date("2/6/23") == "2023-02-06"


def test_clean_date_multidate_keeps_first():
    # BLN transform_date: first token wins for phased layoffs.
    assert (
        IdahoDOL._clean_date("4/10/2026 5/1/2026 5/31/2026") == "2026-04-10"
    )
    assert IdahoDOL._clean_date("11/15/2018 12/3/2018") == "2018-11-15"


def test_clean_date_vendored_corrections():
    # BLN warn-transformer date_corrections for ID.
    assert IdahoDOL._clean_date("2/19/219") == "2019-02-19"
    assert IdahoDOL._clean_date("3/7/2010-3/20/2010") == "2010-03-07"
    assert IdahoDOL._clean_date("2/19/219 (rec'd 2/26/19)") == "2019-02-19"
    # "starting" is stripped before tokenizing; the mangled remainder
    # "rting" maps through the corrections table.
    assert IdahoDOL._clean_date("rting") == "2015-02-16"


def test_clean_date_never_emits_junk():
    assert IdahoDOL._clean_date(None) is None
    assert IdahoDOL._clean_date("") is None
    assert IdahoDOL._clean_date("TBD") is None


def test_clean_jobs_plain_ints():
    assert IdahoDOL._clean_jobs("53") == 53
    assert IdahoDOL._clean_jobs("2,000") == 2000
    assert IdahoDOL._clean_jobs("") == 0
    assert IdahoDOL._clean_jobs(None) == 0


def test_clean_jobs_vendored_corrections():
    # BLN warn-transformer jobs_corrections: the in-Idaho figure wins
    # over a nationwide total; unusable cells become 0.
    assert IdahoDOL._clean_jobs("120 (2 in ID)") == 2
    assert IdahoDOL._clean_jobs("22000 (102 in ID)") == 102
    assert IdahoDOL._clean_jobs("(1 in ID)") == 1
    assert IdahoDOL._clean_jobs("8 in ID") == 8
    assert IdahoDOL._clean_jobs("80-100") == 80
    assert IdahoDOL._clean_jobs("2 5s1ta") == 251
    assert IdahoDOL._clean_jobs("TBD") == 0
    assert IdahoDOL._clean_jobs("22000") == 0


def test_clean_jobs_generic_in_id_fallback():
    # Cells the corrections table has not seen yet still resolve to the
    # in-Idaho figure.
    assert IdahoDOL._clean_jobs("500 (7 in ID)") == 7
    assert IdahoDOL._clean_jobs("12 in ID") == 12
    assert IdahoDOL._clean_jobs("40-60") == 40


# ---------------------------------------------------------------------------
# Offline parse against the truncated real PDF
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("id", tmp_path)
    return src.parse(FIX_PDF)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "city",
        "address",
    ]


def test_parse_drops_header_rows(parsed):
    assert len(parsed) == 12
    assert "Company" not in parsed["company"].tolist()
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    # First data row of the live PDF, mapped per the BLN crosswalk:
    # Date of Letter / Company / Address / City / No. of Employees
    # Affected / Effective or Commencing Date.
    first = parsed.iloc[0]
    assert first["company"] == "Fortrex"
    assert first["notice_date"] == "2026-07-01"      # Date of Letter
    assert first["effective_date"] == "2026-07-30"   # Effective/Commencing
    assert first["employees"] == 53
    assert first["city"] == "Kuna"
    assert first["address"] == "17365 S Cole Road"


def test_parse_dates_never_copied_between_fields(parsed):
    # Intermountain Packing's effective date (4/3) precedes its notice
    # date (4/6) — both must survive verbatim, never synthesized.
    imp = parsed[parsed["company"] == "Intermountain Packing"]
    assert imp["notice_date"].tolist() == ["2026-04-06"]
    assert imp["effective_date"].tolist() == ["2026-04-03"]


def test_parse_quirk_cells(parsed):
    # Out-of-state HQ filing: count cell is literally "(1 in ID)".
    conduent = parsed[parsed["company"] == "Conduent"]
    assert conduent["employees"].tolist() == [1]
    assert conduent["city"].tolist() == ["Florham Park"]  # NJ HQ, kept
    # Phased layoff: effective cell jams three dates; the first wins.
    lasemi = parsed[parsed["company"] == "LA Semiconductor LLC"]
    assert lasemi["effective_date"].tolist() == ["2026-04-10"]
    assert lasemi["notice_date"].tolist() == ["2026-02-09"]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("id", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "ID").all()
    # Idaho publishes no layoff type, county, or industry.
    assert (df["layoff_type"] == "").all()
    assert (df["county"] == "").all()
    assert (df["industry"] == "").all()
