"""Tests for the District of Columbia (DC) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import dc

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real capture of the 2026 page (2026-07-22): the year-link
# block (2017-2025) and the full 10-row table, current-era
# "Number toEmployees Affected" (sic) header.
FIX_2026 = FIXTURES / "dc_2026_sample.html"

# Truncated real capture of the 2020 page: 8 quirk-bearing rows under the
# year's "Number to Employees Affected" header variant.
FIX_2020 = FIXTURES / "dc_2020_sample.html"

# Truncated real capture of the 2019 page: full 10-row table under the
# legacy "Number of Employees Affected" header, incl. the "31, 2019" row.
FIX_2019 = FIXTURES / "dc_2019_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_dc():
    assert "dc" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["dc"]
    assert cls.code == "dc"
    assert cls.name == "District of Columbia"
    assert cls.enabled is True
    assert cls.source_url.startswith(
        "https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications"
    )


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("dc", tmp_path)
    assert src.paths.root == tmp_path / "states" / "dc"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (header-keyed port of BLN warn-scraper dc.py)
# ---------------------------------------------------------------------------


def test_extract_rows_current_layout():
    rows = dc._extract_rows(FIX_2026.read_text(encoding="utf-8"))
    assert len(rows) == 10
    first = rows[0]
    assert first["Notice Date"] == "February 2, 2026"
    assert first["Organization Name"] == "Elior North America"
    assert first["Number toEmployees Affected"] == "76"
    assert first["Effective Layoff Date"] == "June 30, 2026"


def test_extract_rows_canonicalises_header_variants():
    # 2020: "Number to Employees Affected"; 2019: "Number of Employees
    # Affected" — both collapse to BLN's crosswalk key, exactly as BLN's
    # positional CSV does implicitly.
    for fixture in (FIX_2020, FIX_2019):
        rows = dc._extract_rows(fixture.read_text(encoding="utf-8"))
        assert all(dc.EMPLOYEES_KEY in row for row in rows), fixture.name


def test_extract_rows_skips_blank_padding_rows():
    # The 2018 page carries an all-empty <tr> (BLN's any()-filter).
    html = (
        "<html><body><table>"
        "<tr><th>Notice Date</th><th>Organization Name</th>"
        "<th>Number of Employees Affected</th>"
        "<th>Effective Layoff Date</th><th>Code Type</th></tr>"
        "<tr><td>March 2, 2018</td><td>Centerra Group, LLC</td>"
        "<td>243</td><td>March 31, 2018</td><td>1</td></tr>"
        "<tr><td></td><td></td><td></td><td></td><td></td></tr>"
        "</table></body></html>"
    )
    rows = dc._extract_rows(html)
    assert len(rows) == 1
    assert rows[0]["Organization Name"] == "Centerra Group, LLC"


def test_extract_rows_no_table_means_empty_page():
    assert dc._extract_rows("<html><body>No notices yet</body></html>") == []


def test_nested_table_patch():
    # Vendored BLN regex for the June 2025 table-inside-a-cell entry.
    html = (
        "<td>\n <table>\n <tbody>\n <tr>\n <td>Sodexo, Inc.</td>\n"
        " </tr>\n </tbody>\n </table>\n </td>"
    )
    assert "<table>" not in dc._patch_nested_tables(html).replace(
        "<td>\n", "", 1
    )
    # No-op on well-formed pages.
    clean = FIX_2026.read_text(encoding="utf-8")
    assert dc._patch_nested_tables(clean) == clean


def test_year_links_from_root_page():
    links = dc._year_links(FIX_2026.read_text(encoding="utf-8"))
    assert [year for year, _ in links] == list(range(2025, 2016, -1))
    # The 2020/2019 pages live at node/ URLs, not the year-templated path.
    lookup = dict(links)
    assert lookup[2020].endswith("/node/1468786")
    assert lookup[2017].endswith("warn-notifications-2017")


# ---------------------------------------------------------------------------
# Date and count cleaning (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date_formats():
    assert dc._clean_date("February 2, 2026") == "2026-02-02"
    assert dc._clean_date("June 24,2025") == "2025-06-24"       # %B %d,%Y
    assert dc._clean_date("April, 30, 2021") == "2021-04-30"    # %B, %d, %Y
    assert dc._clean_date("") is None
    assert dc._clean_date(None) is None


def test_clean_date_vendored_corrections():
    assert dc._clean_date("TBD") is None
    assert dc._clean_date("31, 2019") == "2019-12-31"
    assert dc._clean_date("May 2 and 5, 2020") == "2020-05-02"
    assert dc._clean_date("March, 20, 2020") == "2020-03-20"
    assert dc._clean_date("December 25, and Feb - Jun 2021") == "2020-12-25"
    assert dc._clean_date("Various Dates through September 30, 2025") == (
        "2025-09-30"
    )
    assert dc._clean_date("May 19 - June 2, 2026") == "2026-05-19"
    # BLN oddities vendored verbatim (see module docstring).
    assert dc._clean_date("February 28, 2022 March 31, 2022") == "2020-02-28"
    assert dc._clean_date("September 30 through September 28, 2025") == (
        "2025-09-30"
    )


def test_clean_date_never_emits_junk():
    assert dc._clean_date("Various dates TBD") is None
    assert dc._clean_date("garbage") is None
    assert dc._clean_date("January 1, 1900") is None  # out of sanity window


def test_clean_employees():
    assert dc._clean_employees("76") == 76
    assert dc._clean_employees("1,604") == 1604
    assert dc._clean_employees("TBD") == 0
    assert dc._clean_employees("All") == 0
    assert dc._clean_employees("") == 0
    assert dc._clean_employees("45 (amended)") == 45
    assert dc._clean_employees("63 (amended)") == 63
    # Generalized fallback for the next amended row the state publishes.
    assert dc._clean_employees("99 (amended)") == 99


# ---------------------------------------------------------------------------
# Offline parse against the consolidated JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_json(tmp_path):
    rows = []
    for year, fixture in ((2026, FIX_2026), (2020, FIX_2020), (2019, FIX_2019)):
        for row in dc._extract_rows(fixture.read_text(encoding="utf-8")):
            rows.append({"Year": year, **row})
    path = tmp_path / "raw_download"
    path.write_text(json.dumps({"source": dc.BASE_URL, "rows": rows}))
    return path


@pytest.fixture
def parsed(tmp_path, raw_json):
    src = warn_sources.get_source("dc", tmp_path)
    return src.parse(raw_json)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "city",
    ]


def test_parse_row_count(parsed):
    assert len(parsed) == 28  # 10 + 8 + 10 fixture rows, none dropped
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_city_is_bln_hardcode(parsed):
    assert (parsed["city"] == "Washington D.C.").all()


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Elior North America"]
    assert row["notice_date"].tolist() == ["2026-02-02"]
    assert row["effective_date"].tolist() == ["2026-06-30"]
    assert row["employees"].tolist() == [76]


def test_parse_tbd_effective_date_stays_none(parsed):
    # "TBD" means the state published no effective date; the notice date
    # is never copied into it.
    row = parsed[parsed["company"] == "Albertson's/Safeway"]
    assert row["notice_date"].tolist() == ["2026-03-13"]
    assert row["effective_date"].tolist() == [None]


def test_parse_quirk_dates(parsed):
    barcelona = parsed[parsed["company"] == "Barcelona Restaurants, LLC"]
    assert barcelona["notice_date"].tolist() == ["2020-05-02"]
    wolfgang = parsed[
        parsed["company"] == "Wolfgang Puck Catering and Newseum"
    ]
    assert wolfgang["notice_date"].tolist() == ["2019-12-31"]  # "31, 2019"
    wmata = parsed[parsed["company"].str.startswith("Washington Metro")]
    assert wmata["employees"].tolist() == [1604]  # "1,604"
    assert wmata["effective_date"].tolist() == ["2020-12-25"]


def test_parse_never_copies_one_date_into_the_other(parsed):
    # Spot-check rows whose published dates genuinely differ.
    for company in ("Elior North America", "Hooters", "Co-Star Group"):
        row = parsed[parsed["company"] == company]
        assert row["notice_date"].tolist() != row["effective_date"].tolist()


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("dc", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "DC").all()
    # DC publishes no county/address/industry and no mapped layoff type
    # (Code Type has no legend and is dropped, per BLN).
    for col in ("county", "address", "industry", "layoff_type"):
        assert (df[col] == "").all(), col
