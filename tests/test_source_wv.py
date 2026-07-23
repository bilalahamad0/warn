"""Tests for the West Virginia (WV) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import wv

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated verbatim capture of the WorkForce WV WARN listing page
# (2026-07-22): the full 2026 <details> block plus trimmed 2025/2024/2021
# blocks — 16 links covering every parser quirk (the consolidated-PDF
# link, undated titles, the r1 revision marker, underscore filename
# dates, in-window stragglers, the undated VIMO duplicate).
FIXTURE = FIXTURES / "wv_listing_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Five cards exactly as _parse_cards emits them from the real
# consolidated PDF (WV-WARN-Notices-1-1-22-to-1-3-25.pdf, fetched
# 2026-07-22), chosen for their quirks: the multi-site Cygnus card with
# the "Total" count row, a Projected Date range list, and the
# "1 WV Resident" count.
CARDS = [
    {
        "Company": "Vimo, Inc. dba GetInsured",
        "Address": "1305 Terra Bella Avenue\nMountain View, CA 94043",
        "Contact Information": "Nicolla Banks\n605-681-4581",
        "Region": "5",
        "County": "Wetzel",
        "Date of Notice": "12/20/24",
        "Projected Date": "12/6/24",
        "Closure/Mass Layoff": "Terminations",
        "Number Affected": "1",
    },
    {
        "Company": "Cygnus Home Servies, LLC, d/b/a Yelloh",
        "Address": (
            "337 Industrial Park Rd.\nBeaver, WV\n3902 Camden Ave.\n"
            "Parkersburg, WV 26101\n1500 W. Benedum Industrial Park Rd.\n"
            "Bridgeport, WV 26330\n35 Edmond Rd.\nKearneysville, WV 25430\n"
            "53 W. Plum Rd.\nRidgeley, WV 26753"
        ),
        "Contact Information": "Dana Panucci\n507-267-5505",
        "Region": "1, 4, 6, and 7",
        "County": "Raleigh, Wood, Harrison,\nJefferson, and Mineral",
        "Date of Notice": "9/23/24",
        "Projected Date": "11/22/24",
        "Closure/Mass Layoff": "Layoff",
        "Number Affected": "54",
    },
    {
        "Company": "Cleveland Cliffs, Inc.",
        "Address": "100 Pennsylvania Avenue\nWeirton, WV 26062",
        "Contact Information": "Jim Dyckman\n513-240-7361",
        "Region": "5",
        "County": "Hancock",
        "Date of Notice": "2/15/24",
        "Projected Date": "4/15/24",
        "Closure/Mass Layoff": "Indefinite Idle",
        "Number Affected": "885",
    },
    {
        "Company": "Carter Roag Coal Company",
        "Address": "14272 Adolph Rd\nMill Creek, WV 26280",
        "Contact Information": "Brett Morris\n304-255-9030\nExt 7523",
        "Region": "6",
        "County": "Randolph",
        "Date of Notice": "6/19/23",
        "Projected Date": "7/12/23-7/26/23\n8/19/23-9/1/23",
        "Closure/Mass Layoff": "Closure",
        "Number Affected": "271",
    },
    {
        "Company": "Watsonville Community Hospital",
        "Address": "75 Nielson Street\nWatsonville, CA 95076",
        "Contact Information": "Matko Vranjes\n831-763-6016",
        "Region": "5",
        "County": "Hancock",
        "Date of Notice": "1/25/22",
        "Projected Date": "3/18/22 to 3/25/22",
        "Closure/Mass Layoff": "Mass Layoff",
        "Number Affected": "1 WV Resident",
    },
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_west_virginia():
    assert "wv" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["wv"]
    assert cls.code == "wv"
    assert cls.name == "West Virginia"
    assert cls.enabled is True
    assert cls.source_url.startswith("https://workforcewv.org/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("wv", tmp_path)
    assert src.paths.root == tmp_path / "states" / "wv"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Listing HTML extraction
# ---------------------------------------------------------------------------


def test_parse_listing_reads_year_sections_off_real_markup():
    html = FIXTURE.read_text(encoding="utf-8")
    links, consolidated = wv._parse_listing(html)
    assert len(links) == 15  # 16 links minus the consolidated PDF
    assert consolidated == (
        "https://workforcewv.org/wp-content/uploads/2025/01/"
        "WV-WARN-Notices-1-1-22-to-1-3-25.pdf"
    )
    per_year = {}
    for link in links:
        per_year[link["year"]] = per_year.get(link["year"], 0) + 1
    assert per_year == {2026: 6, 2025: 5, 2024: 2, 2021: 2}
    # hrefs come out absolute
    assert all(link["href"].startswith("https://") for link in links)


def test_parse_listing_raises_on_alien_page():
    with pytest.raises(ValueError):
        wv._parse_listing("<html><body>maintenance</body></html>")


# ---------------------------------------------------------------------------
# Cleaning rules
# ---------------------------------------------------------------------------


def test_link_company_and_date():
    cases = {
        # (title, href filename) -> (company, notice_date)
        ("JeniusBank WARN 6-4-26", "x.pdf"): ("JeniusBank", "2026-06-04"),
        ("Beckley Mechanic Shop r1 WARN 6-4-25", "x.pdf"): (
            "Beckley Mechanic Shop",
            "2025-06-04",
        ),
        ("Greenbrier Minerals July WARN", "x.pdf"): (
            "Greenbrier Minerals",
            None,
        ),
        ("Mettiki_Supplemental_WARN_State_Notice_04_1_2026", "x.pdf"): (
            "Mettiki",
            "2026-04-01",
        ),
        ("Monongalia County Coal Resources WARN 6-4-2021", "x.pdf"): (
            "Monongalia County Coal Resources",
            "2021-06-04",
        ),
        ("Mylan Pharmaceuticals WARN 5-25-21 Update", "x.pdf"): (
            "Mylan Pharmaceuticals",
            "2021-05-25",
        ),
        # href-keyed correction beats generic token stripping
        (
            "WARN Notice State – West Virginia Conduent",
            "WARN-Notice-State-West-Virginia-Conduent.pdf",
        ): ("Conduent", None),
    }
    for (title, name), expected in cases.items():
        assert wv._link_company_and_date(title, name) == expected


def test_first_date():
    assert wv._first_date("12/20/24") == "2024-12-20"
    assert wv._first_date("7/12/23-7/26/23\n8/19/23-9/1/23") == "2023-07-12"
    assert wv._first_date("8/4/23 and 8/18/23") == "2023-08-04"
    assert wv._first_date("3/18/22 to 3/25/22") == "2022-03-18"
    assert wv._first_date("") is None
    assert wv._first_date(None) is None
    assert wv._first_date("12323") is None  # digits without separators
    assert wv._first_date("13/45/22") is None  # impossible month/day


def test_clean_employees():
    assert wv._clean_employees("885") == 885
    assert wv._clean_employees("1 WV Resident") == 1
    assert wv._clean_employees("") == 0
    assert wv._clean_employees(None) == 0
    assert wv._clean_employees("Total") == 0
    assert wv._clean_employees("99999") == 0  # BLN maximum_jobs cap


def test_card_window_parses_the_consolidated_filename():
    assert wv._card_window("WV-WARN-Notices-1-1-22-to-1-3-25.pdf") == (
        "2022-01-01",
        "2025-01-03",
    )
    # unparseable name -> the known shipped window, never a crash
    assert wv._card_window("WV-WARN-Notices.pdf") == wv.DEFAULT_CARD_WINDOW


# ---------------------------------------------------------------------------
# Offline parse against the raw JSON exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_json(tmp_path):
    links, consolidated = wv._parse_listing(
        FIXTURE.read_text(encoding="utf-8")
    )
    path = tmp_path / "raw_download"
    path.write_text(
        json.dumps(
            {
                "source": wv.PAGE_URL,
                "consolidated_url": consolidated,
                "card_window": list(wv._card_window(consolidated)),
                "listing": links,
                "cards": CARDS,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return path


@pytest.fixture
def parsed(tmp_path, raw_json):
    src = warn_sources.get_source("wv", tmp_path)
    return src.parse(raw_json)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "address",
    ]


def test_parse_row_count_and_required_company(parsed):
    # 5 cards + 11 listing rows (6 of 2026, 3 kept of 2025, 2 of 2021);
    # the 2024 section, both in-window stragglers, and the undated VIMO
    # re-link are dropped as consolidated-card duplicates.
    assert len(parsed) == 16
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_card_crosswalk(parsed):
    row = parsed[parsed["company"] == "Cleveland Cliffs, Inc."].iloc[0]
    assert row["notice_date"] == "2024-02-15"     # Date of Notice
    assert row["effective_date"] == "2024-04-15"  # Projected Date
    assert row["employees"] == 885                # Number Affected
    assert row["layoff_type"] == "Indefinite Idle"
    assert row["county"] == "Hancock"
    assert row["address"] == "100 Pennsylvania Avenue, Weirton, WV 26062"


def test_parse_card_quirks(parsed):
    # Multi-site Cygnus card: the "Total" row count, every site address.
    cygnus = parsed[parsed["company"].str.startswith("Cygnus")]
    assert len(cygnus) == 1  # the 7-16-24 listing straggler was dropped
    assert cygnus["employees"].tolist() == [54]
    assert "Ridgeley, WV 26753" in cygnus["address"].iloc[0]
    # Projected-Date range list -> only its FIRST date.
    roag = parsed[parsed["company"] == "Carter Roag Coal Company"]
    assert roag["effective_date"].tolist() == ["2023-07-12"]
    # "1 WV Resident" -> 1.
    wch = parsed[parsed["company"] == "Watsonville Community Hospital"]
    assert wch["employees"].tolist() == [1]


def test_parse_drops_card_duplicates_from_the_listing(parsed):
    # The undated "WARN VIMO INC" link duplicates the 12/20/24 card.
    vimo = parsed[parsed["company"].str.contains("Vimo", case=False)]
    assert len(vimo) == 1
    assert vimo["employees"].tolist() == [1]
    # The whole 2024 section lives in the cards.
    assert len(parsed[parsed["company"] == "Cleveland Cliffs, Inc."]) == 1
    assert not parsed["company"].str.contains("Stonebrook").any()


def test_parse_listing_rows_publish_no_counts_or_effective_dates(parsed):
    jenius = parsed[parsed["company"] == "JeniusBank"].iloc[0]
    assert jenius["notice_date"] == "2026-06-04"
    assert jenius["effective_date"] is None  # never synthesized
    assert jenius["employees"] == 0          # listing publishes no counts
    # Undated titles keep None, never the section year.
    conduent = parsed[parsed["company"] == "Conduent"].iloc[0]
    assert conduent["notice_date"] is None


def test_parse_never_fabricates_missing_fields(tmp_path, parsed):
    # WV publishes no city or industry: unify() must leave them empty.
    src = warn_sources.get_source("wv", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "WV").all()
    assert (df["city"] == "").all()
    assert (df["industry"] == "").all()
