"""Tests for the South Dakota (SD) WARN source."""

import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import sd

FIXTURES = Path(__file__).parent / "fixtures"

# Truncated real capture of the DLR notices page (2026-07-22): the real
# header plus 9 of the 79 live rows, markup verbatim — including every
# BLN jobs_corrections quirk still visible in the feed ("1-5",
# "324<br>(11 reside in South Dakota)", "173 (nationwide)"), the
# &nbsp;-only employees cell (KBR/EROS), the "n/a" location (Conduent),
# and the one single-digit-day date "01/5/2012" (Bosselman).
FIXTURE = FIXTURES / "sd_notices_sample.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_south_dakota():
    assert "sd" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["sd"]
    assert cls.code == "sd"
    assert cls.name == "South Dakota"
    assert cls.enabled is True
    assert cls.source_url.startswith("https://dlr.sd.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("sd", tmp_path)
    assert src.paths.root == tmp_path / "states" / "sd"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# HTML extraction (vendored BLN warn-scraper sd.py flow)
# ---------------------------------------------------------------------------


def test_parse_table_keys_rows_off_the_real_header():
    rows = sd._parse_table(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 9
    # "Employees<br/>Affected" header cell squishes to the crosswalk key.
    assert set(rows[0]) == {
        "Company",
        "Location",
        "Date Received",
        "Employees Affected",
    }
    assert rows[0]["Company"] == "Conduent"
    assert rows[1]["Location"] == "Rapid City, Sioux Falls"
    # The <br>-split count cell squishes to BLN's exact correction key.
    assert rows[5]["Employees Affected"] == (
        "324 (11 reside in South Dakota)"
    )
    # The &nbsp;-only cell squishes to empty.
    assert rows[4]["Employees Affected"] == ""


def test_parse_table_raises_on_missing_or_alien_table():
    with pytest.raises(ValueError):
        sd._parse_table("<html><body>maintenance</body></html>")
    with pytest.raises(ValueError):
        sd._parse_table(
            "<table><tr><td>Widget</td><td>Price</td></tr></table>"
        )


# ---------------------------------------------------------------------------
# Cleaning rules (BLN transformer quirks)
# ---------------------------------------------------------------------------


def test_clean_date():
    assert sd._clean_date("06/29/2026") == "2026-06-29"
    assert sd._clean_date("01/5/2012") == "2012-01-05"  # unpadded live day
    assert sd._clean_date("") is None
    assert sd._clean_date(None) is None
    assert sd._clean_date("garbage") is None
    assert sd._clean_date("01/01/0201") is None  # junk year -> no date


def test_clean_employees_vendored_corrections():
    assert sd._clean_employees("53") == 53
    assert sd._clean_employees("1-5") == 1
    assert sd._clean_employees("324 (11 reside in South Dakota)") == 11
    assert sd._clean_employees("173 (nationwide)") == 0  # BLN: None -> 0
    assert sd._clean_employees("n/a") == 0
    assert sd._clean_employees("") == 0
    assert sd._clean_employees("99999") == 0  # BLN maximum_jobs cap


# ---------------------------------------------------------------------------
# Offline parse against the consolidated CSV exactly as fetch() writes it
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_csv(tmp_path):
    import csv

    rows = sd._parse_table(FIXTURE.read_text(encoding="utf-8"))
    path = tmp_path / "raw_download"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=sd.RAW_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def parsed(tmp_path, raw_csv):
    src = warn_sources.get_source("sd", tmp_path)
    return src.parse(raw_csv)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "employees",
        "city",
    ]


def test_parse_keeps_every_company_row(parsed):
    assert len(parsed) == 9
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["notice_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_field_crosswalk(parsed):
    row = parsed[parsed["company"] == "Republic National Distributing Company"]
    assert row["notice_date"].tolist() == ["2026-05-19"]  # Date Received
    assert row["employees"].tolist() == [53]              # Employees Affected
    assert row["city"].tolist() == ["Rapid City, Sioux Falls"]  # Location


def test_parse_quirk_rows(parsed):
    by_company = parsed.set_index("company")
    # "n/a" location placeholder -> empty city.
    assert by_company.loc["Conduent", "city"] == ""
    # &nbsp;-only count -> 0, never a fabricated number.
    kbr = parsed[parsed["company"].str.startswith("KBR")]
    assert kbr["employees"].tolist() == [0]
    # Nationwide-only counts carry no SD figure -> 0.
    jenius = parsed[parsed["company"].str.startswith("JeniusBank")]
    assert jenius["employees"].tolist() == [0]
    # Only the in-state share of a mixed count is kept.
    a360 = parsed[parsed["company"].str.startswith("Accelerate360")]
    assert a360["employees"].tolist() == [11]
    # "1-5" range -> its lower bound.
    assert by_company.loc["Verety LLC", "employees"] == 1
    # Single-digit live day parses.
    assert by_company.loc["Bosselman Travel Center", "notice_date"] == (
        "2012-01-05"
    )


def test_parse_never_fabricates_missing_fields(tmp_path, parsed):
    # SD publishes no effective date / county / address / industry /
    # layoff type: unify() must leave them empty, never copied.
    src = warn_sources.get_source("sd", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "SD").all()
    assert df["effective_date"].isna().all()
    assert (df["county"] == "").all()
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
    assert (df["layoff_type"] == "").all()
