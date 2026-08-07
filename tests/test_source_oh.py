"""Tests for the Ohio (OH) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import oh as oh_module

FIXTURE = Path(__file__).parent / "fixtures" / "oh_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_ohio():
    assert "oh" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["oh"]
    assert cls.code == "oh"
    assert cls.name == "Ohio"
    assert cls.source_url.startswith("https://jfs.ohio.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("oh", tmp_path)
    assert src.paths.root == tmp_path / "states" / "oh"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# csvUrl extraction + feed-line cleanup (vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------

# Verbatim (trimmed) snippet of the escaped Next.js payload from the live
# ODJFS notices page, fetched 2026-07-21.
PAGE_SNIPPET = (
    '{\\"csvUrl\\":\\"https://dam.assets.ohio.gov/raw/upload/f_auto/'
    'q_auto/v1776197484/jfs.ohio.gov/2026/2026-warn-notice.csv\\"}'
)

# Verbatim head of the live 2026 feed CSV: junk preamble then real rows.
FEED_HEAD = (
    "s,s,h,s,s,s,s,s,s\n"
    ",,,,,,,,\n"
    "Company,Date Received,URL,City/County,Layoff/Closure,"
    "Potential Number Affected,Layoff Date(s),Phone Number,Union\n"
    "Notions Marketing,7/6/2026,https://example.test/x.pdf,"
    "Strongsville/Cuyahoga,Layoff,3,6/28/2026,(616) 608-9946,N\n"
)


def test_extract_csv_url_from_escaped_payload():
    url = oh_module._extract_csv_url(PAGE_SNIPPET)
    assert url == (
        "https://dam.assets.ohio.gov/raw/upload/f_auto/q_auto/"
        "v1776197484/jfs.ohio.gov/2026/2026-warn-notice.csv"
    )


def test_extract_csv_url_plain_json_fallback():
    assert oh_module._extract_csv_url(
        '{"csvUrl":"https://example.test/feed.csv"}'
    ) == "https://example.test/feed.csv"
    with pytest.raises(RuntimeError):
        oh_module._extract_csv_url("<html>no data here</html>")


def test_read_feed_csv_drops_junk_preamble():
    rows = oh_module._read_feed_csv(FEED_HEAD)
    assert len(rows) == 1
    assert rows[0]["Company"] == "Notions Marketing"
    assert rows[0]["Date Received"] == "7/6/2026"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("oh", tmp_path)
    return src.parse(FIXTURE)


def _row(parsed, company):
    match = parsed[parsed["company"] == company]
    assert len(match) == 1, company
    return match.iloc[0]


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "city",
    ]


def test_parse_drops_junk_rows_and_requires_company(parsed):
    # Fixture has 17 real rows plus a blank row and a repeated header row.
    assert len(parsed) == 17
    assert (parsed["company"].str.strip() != "").all()
    assert "Company" not in set(parsed["company"])


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int_with_zero_default(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    # "" and "Unknown" -> no published count -> 0.
    assert _row(parsed, "Allpro Parking Ohio, LLC")["employees"] == 0
    assert _row(parsed, "Consolidated Glass and Mirror, LLC")["employees"] == 0


def test_parse_field_crosswalk_follows_bln(parsed):
    row = _row(parsed, "UPDATE Refresco Beverages US Inc")
    assert row["notice_date"] == "2026-07-14"    # Date Received
    assert row["effective_date"] == "2026-06-24"  # Layoff Date(s)
    assert row["employees"] == 44                # Potential Number Affected
    assert row["layoff_type"] == "Layoff"        # Layoff/Closure
    assert row["city"] == "Carlisle"             # City/County split
    assert row["county"] == "Warren"


def test_parse_date_ranges_keep_first_date(parsed):
    assert _row(parsed, "Shiloh Industries")["effective_date"] == "2026-08-30"
    assert (
        _row(parsed, "A.O. Smith Corporation")["effective_date"] == "2026-08-24"
    )
    # " to " ranges (historical style)
    assert _row(parsed, "Things Remembered")["effective_date"] == "2023-01-13"


def test_parse_vendored_date_corrections(parsed):
    # "08/14/02018" and "01/30/201 7" are known-bad feed values.
    meggitt = _row(parsed, "Meggitt Aircraft Braking Systems Corporation")
    assert meggitt["notice_date"] == "2018-08-14"
    impact = _row(parsed, "Impact Fulfillment Services")
    assert impact["notice_date"] == "2017-01-30"
    # literal "None"/"Unknown" placeholders are nulled, never kept as text.
    assert _row(parsed, "The Cottages of Clayton")["effective_date"] is None
    assert _row(parsed, "Bobby Layman Chevrolet")["effective_date"] is None


def test_parse_strips_revised_prefix_without_copying_dates(parsed):
    row = _row(parsed, "Allpro Parking Ohio, LLC")
    assert row["notice_date"] == "2018-05-18"      # "Revised - 05/18/2018"
    assert row["effective_date"] == "2018-11-15"   # kept distinct


def test_parse_vendored_jobs_corrections(parsed):
    assert _row(parsed, "Coney Island Inc.")["employees"] == 13   # "13 FT"
    nemf = _row(parsed, "New England Motor Freight (NEMF)")
    assert nemf["employees"] == 58                       # "58 94 97 35"


def test_parse_location_split(parsed):
    row = _row(parsed, "Shiloh Industries")
    assert (row["city"], row["county"]) == ("Valley City", "Medina")
    remembered = _row(parsed, "Things Remembered")
    assert remembered["city"] == "Richmond Heights and North Jackson"
    assert remembered["county"] == "Various Counties"
    # Unsplittable values stay verbatim in city; county is never guessed.
    toys = _row(parsed, "Toys R Us / Babies R Us")
    assert (toys["city"], toys["county"]) == ("Statewide", "")
    nemf = _row(parsed, "New England Motor Freight (NEMF)")
    assert nemf["county"] == ""
    assert nemf["city"].startswith("Cincinnati/Hamilton")


def test_parse_layoff_type_only_on_current_feed_rows(parsed):
    types = set(parsed["layoff_type"])
    assert types <= {"Layoff", "Closure", "Temp Layoff", ""}
    pioneer = _row(parsed, "Pioneer Cladding & Glazing Systems, Inc.")
    assert pioneer["layoff_type"] == "Temp Layoff"
    # historical rows predate the Layoff/Closure column
    assert _row(parsed, "Coney Island Inc.")["layoff_type"] == ""


def test_parse_strips_company_whitespace(parsed):
    assert "Wood Group USA Inc." in set(parsed["company"])


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("oh", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "OH").all()
    # Ohio publishes neither address nor industry.
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
