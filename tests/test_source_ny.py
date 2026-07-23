"""Tests for the New York (Tableau Public CSV) WARN source."""

import re
from pathlib import Path

import warn_sources
from warn_sources.ny import NewYorkDOL, _clean_date

FIXTURE = Path(__file__).parent / "fixtures" / "ny_tableau_sample.csv"

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_ny_registered():
    assert "ny" in warn_sources.SOURCES
    assert warn_sources.SOURCES["ny"] is NewYorkDOL


def test_ny_metadata():
    assert NewYorkDOL.code == "ny"
    assert NewYorkDOL.name == "New York"
    assert "tableau.com" in NewYorkDOL.source_url


def test_ny_uses_per_state_paths(tmp_path):
    src = warn_sources.get_source("ny", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ny"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a truncated real Tableau export
# ---------------------------------------------------------------------------


def test_parse_fixture_columns_and_types(tmp_path):
    df = NewYorkDOL(tmp_path).parse(FIXTURE)

    # Only fields NY actually publishes — no city/industry fabrication.
    assert sorted(df.columns) == sorted(
        [
            "company",
            "notice_date",
            "effective_date",
            "employees",
            "layoff_type",
            "county",
            "address",
        ]
    )
    assert len(df) == 12  # every fixture data row survives

    # company required and clean
    assert (df["company"].str.len() > 0).all()
    assert df["company"].iloc[0] == "420 Park FB LLC"

    # dates are strict ISO strings
    for col in ("notice_date", "effective_date"):
        assert df[col].map(lambda v: v is None or bool(ISO_RE.match(v))).all()
    row = df[df["company"] == "Amazon"].iloc[0]
    assert row["notice_date"] == "2026-01-28"
    assert row["effective_date"] == "2026-04-28"

    # employees are real ints (from the trailing-space duplicated column)
    assert df["employees"].map(lambda v: isinstance(v, int)).all()
    assert row["employees"] == 135

    # layoff_type combines the state's two columns, CA-style wording
    assert row["layoff_type"] == "Layoff Permanent"
    assert set(df["layoff_type"]) <= {"Layoff Permanent", "Closure Permanent"}

    # location fields NY publishes
    assert row["county"] == "New York"
    assert row["address"].startswith("1440 Broadway")


def test_parse_drops_junk_rows(tmp_path):
    # A repeated header line and a blank-company line must be dropped.
    junk = tmp_path / "junk.csv"
    junk.write_text(
        FIXTURE.read_text()
        + "Business Legal Name,Date Layoff/Closure Starts,"
        "Date of WARN Notice ,Date Posted  ,Impacted Site Address,"
        "Impacted Site County,Layoff or Closure?,"
        "Permanent or Temporary Layoff?,Reason for Layoff/Closure   ,"
        "Index,Number of Affected Workers ,Number of Affected Workers \n"
        ",2026-01-01,2026-01-01,2026-01-01,addr,Kings,Layoff,Permanent,"
        "Other,99,10,10\n"
    )
    df = NewYorkDOL(tmp_path).parse(junk)
    assert len(df) == 12
    assert not (df["company"].str.lower() == "business legal name").any()


def test_parse_unify_roundtrip(tmp_path):
    src = NewYorkDOL(tmp_path)
    df = src.unify(src.parse(FIXTURE))
    assert (df["state"] == "NY").all()
    assert (df["city"] == "").all()       # not published -> empty, not faked
    assert (df["industry"] == "").all()


# ---------------------------------------------------------------------------
# Date cleaning (BLN quirks)
# ---------------------------------------------------------------------------


def test_clean_date_handles_bln_quirks():
    assert _clean_date("2026-04-06") == "2026-04-06"
    assert _clean_date("2022-09-29 00:00:00") == "2022-09-29"  # first token
    assert _clean_date("3/6/2023") == "2023-03-06"
    assert _clean_date("929/2022") == "2022-09-29"    # known typo correction
    assert _clean_date("2/2/2024`") == "2024-02-02"   # stray backtick
    assert _clean_date("TBD") is None                 # junk -> None, not junk
    assert _clean_date("") is None
    assert _clean_date(None) is None
