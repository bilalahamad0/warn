"""Tests for the Illinois WARN source (warn_sources/il.py)."""

import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from openpyxl import Workbook

import warn_sources
from warn_sources.il import COLUMNS, IllinoisWorkNet, _il_date, _il_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "il_sample.xlsx"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_il():
    assert "il" in warn_sources.SOURCES
    src = warn_sources.get_source("il")
    assert isinstance(src, IllinoisWorkNet)
    assert src.code == "il"
    assert src.name == "Illinois"
    assert src.paths.root.name == "il"


# ---------------------------------------------------------------------------
# Offline parse against real export rows (tests/fixtures/il_sample.xlsx)
# ---------------------------------------------------------------------------


def test_parse_fixture_schema_and_types(tmp_path):
    df = IllinoisWorkNet(tmp_path).parse(FIXTURE)

    assert list(df.columns) == COLUMNS
    # 16 real rows in the fixture; the blank-company junk row is dropped
    assert len(df) == 16
    assert df["company"].str.len().gt(0).all()

    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO_DATE.match(val), f"{col}: {val!r}"

    assert all(isinstance(v, (int, np.integer)) for v in df["employees"])


def test_parse_fixture_field_mapping(tmp_path):
    df = IllinoisWorkNet(tmp_path).parse(FIXTURE)
    rows = {r["company"]: r for r in df.to_dict("records")}

    # Clean row: no Impact Date published -> effective_date stays None
    momence = rows["Momence Packing Co."]
    assert momence["notice_date"] == "2025-06-02"
    assert momence["effective_date"] is None
    assert momence["employees"] == 274
    assert momence["layoff_type"] == "Plant Closure"
    assert momence["county"] == "Kankakee County"
    assert momence["city"] == "Momence"
    assert momence["address"] == "334 W North St"  # trailing space collapsed
    assert momence["industry"] == "Manufacturing"

    # Row with a real Impact Date (05:00 timestamp artifact -> date only);
    # employees from Revised Layoff (4335), not Expected Layoff (1371)
    fca = rows["FCA US, LLC"]
    assert fca["notice_date"] == "2019-02-26"
    assert fca["effective_date"] == "2020-08-25"
    assert fca["employees"] == 4335
    assert fca["layoff_type"] == "Mass Layoff"

    # 1987 row sits exactly on the BLN minimum-year boundary and is kept
    ge = rows["G. E. APPLIANCES"]
    assert ge["notice_date"] == "1987-09-01"
    assert ge["employees"] == 1100

    # IL publishes 0 for unknown head-counts -> stays 0, never None
    assert rows["ALLSTATE"]["employees"] == 0

    # Large-but-plausible count under the 100k BLN cap survives
    assert rows["UNITED AIRLINES"]["employees"] == 14487

    # Per-location reporting: one company, two location rows
    assert df[df["company"] == "True Value Company, LLC"].shape[0] == 2


def test_parse_notice_date_fallback(tmp_path):
    """Missing Initial Date Reported falls back to the notification list."""
    header = [
        "Location Name",
        "Initial Date Reported",
        "Notification(s) Received",  # BLN-era column name also recognised
        "Impact Date",
        "Revised Layoff",
        "Reason",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Layoffs"
    ws.append(header)
    ws.append(["Acme Co.", None, "3/17/2023, 12/9/2022", None, 25, "Layoff"])
    ws.append(["No Dates LLC", None, None, None, 0, "Layoff"])
    path = tmp_path / "il_fallback.xlsx"
    wb.save(path)

    df = IllinoisWorkNet(tmp_path).parse(path)
    rows = {r["company"]: r for r in df.to_dict("records")}
    # newest-first list -> first entry
    assert rows["Acme Co."]["notice_date"] == "2023-03-17"
    assert rows["No Dates LLC"]["notice_date"] is None
    assert rows["No Dates LLC"]["employees"] == 0


# ---------------------------------------------------------------------------
# Value transforms
# ---------------------------------------------------------------------------


def test_il_date_quirks():
    assert _il_date(datetime(2020, 8, 25, 5, 0)) == "2020-08-25"
    assert _il_date("6/2/2025") == "2025-06-02"
    assert _il_date("2025-06-02 00:00:00") == "2025-06-02"
    assert _il_date("") is None
    assert _il_date(None) is None
    assert _il_date(float("nan")) is None
    # Unknown garbage must degrade to None, never a raw string
    assert _il_date("TBD") is None
    # BLN guards: below the 1987 minimum year / too far in the future
    assert _il_date(datetime(1986, 12, 31)) is None
    assert _il_date(datetime.today() + timedelta(days=400)) is None


def test_il_jobs_quirks():
    assert _il_jobs(274) == 274
    assert _il_jobs("1,100") == 1100
    assert _il_jobs(0) == 0
    assert _il_jobs(None) == 0
    assert _il_jobs("") == 0
    assert _il_jobs(-5) == 0
    assert _il_jobs(14487) == 14487
    assert _il_jobs(150000) == 0  # over the BLN 100k sanity cap
