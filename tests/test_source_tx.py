"""Tests for the Texas (TWC) WARN source.

The fixture ``tests/fixtures/tx_sample.xlsx`` uses the live per-year TWC
column layout (NOTICE_DATE, JOB_SITE_NAME, COUNTY_NAME, WDA_NAME,
TOTAL_LAYOFF_NUMBER, LayOff_Date, WFDD_RECEIVED_DATE, CITY_NAME) filled
with 12 real 2019 filings from BLN's public TX historical dataset, plus
documented feed quirks: the verbatim 1930-03-30 date typo (see BLN
warn-transformer date_corrections), a row with no layoff count, a
repeated header row, and a blank row.
"""

import re
from pathlib import Path

import pandas as pd

import warn_sources
from warn_sources.tx import TexasTWC

FIXTURE = Path(__file__).parent / "fixtures" / "tx_sample.xlsx"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_registry_contains_texas():
    assert "tx" in warn_sources.SOURCES
    assert warn_sources.SOURCES["tx"] is TexasTWC


def test_texas_disabled_while_bot_walled():
    # twc.texas.gov sits behind an AWS WAF challenge (see module docstring);
    # the source must not be part of the live run until that changes.
    assert TexasTWC.enabled is False
    assert all(s.code != "tx" for s in warn_sources.all_sources())


def test_parse_fixture_xlsx(tmp_path):
    src = TexasTWC(tmp_path)
    df = src.parse(FIXTURE)

    # Only fields TWC really publishes — no fabricated columns.
    assert list(df.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "county",
        "city",
    ]

    # 14 data rows survive; repeated-header and blank rows are dropped.
    assert len(df) == 14
    assert not (df["company"] == "JOB_SITE_NAME").any()
    assert (df["company"].str.len() > 0).all()

    # Dates are ISO YYYY-MM-DD strings (or None).
    for col in ("notice_date", "effective_date"):
        assert all(v is None or ISO_DATE.match(v) for v in df[col])

    # NOTICE_DATE and LayOff_Date map to distinct fields — never copied.
    first = df.iloc[0]
    assert first["company"] == "El Rio Grande Facility"
    assert first["notice_date"] == "2019-09-03"
    assert first["effective_date"] == "2019-09-30"
    assert first["county"] == "Dallas"
    assert first["city"] == "Garland"

    # employees is always an int; 0 when the state published no count.
    assert pd.api.types.is_integer_dtype(df["employees"])
    assert int(first["employees"]) == 116
    no_count = df[df["company"] == "No Count Published LLC"].iloc[0]
    assert int(no_count["employees"]) == 0

    # BLN date correction: the feed's 1930-03-30 typo becomes 2020-03-30.
    quirk = df[df["company"] == "Quirk Hotel Co (feed date typo)"].iloc[0]
    assert quirk["effective_date"] == "2020-03-30"
    assert quirk["notice_date"] == "2020-03-25"


def test_parse_consolidated_csv_roundtrip(tmp_path):
    # fetch() consolidates the per-year workbooks into a CSV at paths.raw;
    # parse() must read that CSV branch identically to the XLSX branch.
    src = TexasTWC(tmp_path)
    raw = pd.read_excel(FIXTURE, dtype=object)
    csv_path = src.paths.raw
    src.paths.ensure()
    raw.to_csv(csv_path, index=False)

    df_csv = src.parse(csv_path)
    df_xlsx = src.parse(FIXTURE)
    assert df_csv.equals(df_xlsx)


def test_unify_stamps_tx_and_fills_unpublished_fields(tmp_path):
    src = TexasTWC(tmp_path)
    df = src.unify(src.parse(FIXTURE))
    assert (df["state"] == "TX").all()
    assert (df["layoff_type"] == "").all()   # TWC does not publish these
    assert (df["address"] == "").all()
    assert (df["industry"] == "").all()
