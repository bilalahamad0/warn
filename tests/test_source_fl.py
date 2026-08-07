"""Tests for the Florida (REACT WARN paginated HTML) source."""

import json
import re
from pathlib import Path

import warn_sources
from warn_sources.fl import (
    FloridaCommerce,
    _clean_date,
    _html_to_rows,
    _next_page_url,
)

# Truncated real snippet of
# https://reactwarn.floridajobs.org/WarnList/Records?year=2026 (page 1,
# fetched 2026-07-21): real markup incl. malformed </br> line breaks,
# multi-line company cells, "thru" date ranges, and the tfoot pager.
FIXTURE = Path(__file__).parent / "fixtures" / "fl_records_sample.html"

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _raw_json(tmp_path):
    """Consolidated raw file exactly as fetch() writes it."""
    rows = _html_to_rows(FIXTURE.read_text())
    raw = tmp_path / "raw_download"
    raw.write_text(json.dumps({"years": [2025, 2026], "rows": rows}))
    return raw


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_fl_registered():
    assert "fl" in warn_sources.SOURCES
    assert warn_sources.SOURCES["fl"] is FloridaCommerce


def test_fl_metadata():
    assert FloridaCommerce.code == "fl"
    assert FloridaCommerce.name == "Florida"
    assert FloridaCommerce.enabled
    assert FloridaCommerce.source_url.startswith(
        "https://reactwarn.floridajobs.org/")


def test_fl_uses_per_state_paths(tmp_path):
    src = warn_sources.get_source("fl", tmp_path)
    assert src.paths.root == tmp_path / "states" / "fl"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (truncated real page)
# ---------------------------------------------------------------------------


def test_html_to_rows_extracts_all_cells():
    rows = _html_to_rows(FIXTURE.read_text())
    assert len(rows) == 7
    assert all(set(r) == {
        "Company Name",
        "State Notification Date",
        "Layoff Date",
        "Employees Affected",
        "Industry",
        "Attachment",
    } for r in rows)
    # </br> breaks became newlines so the company block keeps structure
    assert rows[0]["Company Name"].startswith("Parallel Florida, LLC\n")
    assert rows[0]["State Notification Date"] == "07-21-26"


def test_next_page_url_from_footer():
    html = FIXTURE.read_text()
    assert _next_page_url(html, 2) == (
        "https://reactwarn.floridajobs.org/WarnList/Records?year=2026&page=2"
    )
    assert _next_page_url(html, 3) is None  # last page -> stop


# ---------------------------------------------------------------------------
# Offline parse
# ---------------------------------------------------------------------------


def test_parse_fixture_columns_and_types(tmp_path):
    df = FloridaCommerce(tmp_path).parse(_raw_json(tmp_path))

    # Only fields FL actually publishes — no county/layoff_type fabrication.
    assert sorted(df.columns) == sorted(
        [
            "company",
            "notice_date",
            "effective_date",
            "employees",
            "city",
            "address",
            "industry",
        ]
    )
    assert len(df) == 7  # every fixture data row survives

    # company = first line of the multi-line cell (BLN transform_company)
    assert (df["company"].str.len() > 0).all()
    assert not df["company"].str.contains("\n").any()
    assert df["company"].iloc[0] == "Parallel Florida, LLC"

    # dates are strict ISO strings; effective = start of the "thru" range
    for col in ("notice_date", "effective_date"):
        assert df[col].map(lambda v: v is None or bool(ISO_RE.match(v))).all()
    row = df[df["company"] == "Amazon"].iloc[0]
    assert row["notice_date"] == "2026-04-17"
    assert row["effective_date"] == "2026-07-02"  # not the 09-30 range end

    # employees are real ints
    assert df["employees"].map(lambda v: isinstance(v, int)).all()
    assert row["employees"] == 616

    # city = last line's first comma segment (BLN transform_location);
    # address = the middle lines of the block
    assert row["city"] == "HOMESTEAD"
    assert row["address"] == "27505 SW 132 Ave, TMB8"
    assert row["industry"] == "Transportation and Warehousing"


def test_parse_drops_junk_rows(tmp_path):
    rows = _html_to_rows(FIXTURE.read_text())
    rows.append({"Company Name": "COMPANY NAME"})  # stray repeated header
    rows.append({"Company Name": "  \n "})  # blank cell
    raw = tmp_path / "raw_junk"
    raw.write_text(json.dumps({"rows": rows}))
    df = FloridaCommerce(tmp_path).parse(raw)
    assert len(df) == 7
    assert not (df["company"].str.upper() == "COMPANY NAME").any()


def test_parse_unify_roundtrip(tmp_path):
    src = FloridaCommerce(tmp_path)
    df = src.unify(src.parse(_raw_json(tmp_path)))
    assert (df["state"] == "FL").all()
    assert (df["county"] == "").all()       # not published -> empty, not faked
    assert (df["layoff_type"] == "").all()


# ---------------------------------------------------------------------------
# Date cleaning (BLN quirks)
# ---------------------------------------------------------------------------


def test_clean_date_handles_fl_quirks():
    assert _clean_date("07-21-26") == "2026-07-21"          # %m-%d-%y
    assert _clean_date("07/21/2026") == "2026-07-21"        # %m/%d/%Y
    assert _clean_date("07-06-26\n\n thru \n \n07-20-26") == "2026-07-06"
    assert _clean_date("07-06-26 thru 07-20-26") == "2026-07-06"
    assert _clean_date("TBD") is None                       # junk -> None
    assert _clean_date("") is None
    assert _clean_date(None) is None
