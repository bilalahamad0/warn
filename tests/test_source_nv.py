"""Tests for the Nevada (NV) WARN source."""

import csv
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import nv

FIXTURES = Path(__file__).parent / "fixtures"

# Real 2026 master PDF fetched live 2026-07-22 from
# https://detr.nv.gov/content/media/WARN_and_Non_WARN_Master_w_Logo_.pdf
# (one page, 17 data rows — the gridless text-era layout). It carries the
# format's landmines: cells jammed without whitespace ("3/10/2026Layoff",
# "113iHerb"), a row with no dates at all (Intuit), an "unknown" count,
# and a multi-site row whose city/county cells visually touch
# ("Las Vegas/RenoClark/Washoe").
FIX_PDF = FIXTURES / "nv_2026_sample.pdf"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_nevada():
    assert "nv" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["nv"]
    assert cls.code == "nv"
    assert cls.name == "Nevada"
    assert cls.enabled is True
    assert cls.source_url.startswith("https://detr.nv.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("nv", tmp_path)
    assert src.paths.root == tmp_path / "states" / "nv"
    assert src.paths.latest.name == "warn_latest.json"
    assert src.paths.raw.suffix == ".csv"


# ---------------------------------------------------------------------------
# Date and count cleaning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1/8/2026", "2026-01-08"),
        ("12/7/22", "2022-12-07"),
        ("8/25/2028", "2028-08-25"),  # state's own typo, kept as published
        ("Jun-26", "2026-06-01"),  # month-year shorthand correction
        ("Unknown", None),
        ("Multiple", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_clean_date(raw, expected):
    assert nv.NevadaDETR._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("209", 209),
        ("1,097", 1097),
        ("20+", 20),  # stated minimum kept
        ("unknown", 0),
        ("NR", 0),
        ("", 0),
        (None, 0),
    ],
)
def test_clean_jobs(raw, expected):
    assert nv.NevadaDETR._clean_jobs(raw) == expected


# ---------------------------------------------------------------------------
# Left-region untangling (the gridless era's jammed cells)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (  # fully jammed row
            "1/8/2025 3/10/2026Layoff 3SMBC Manubank",
            ("1/8/2025", "3/10/2026", "Layoff", "3", "SMBC Manubank"),
        ),
        (  # received jammed with a free-text effective
            "2/14/2025Unknown Closure Unknown JoAnn Stores LLC",
            ("2/14/2025", "Unknown", "Closure", "Unknown", "JoAnn Stores LLC"),
        ),
        (  # standalone count, employer starting with digits stays whole
            "4/29/2024 7/3/2024 Closure NR 99 Cent Only Stores, LLC",
            ("4/29/2024", "7/3/2024", "Closure", "NR", "99 Cent Only Stores, LLC"),
        ),
        (  # no dates at all (Intuit 2026) — nothing synthesized
            "Layoff 162Intuit Inc",
            (None, None, "Layoff", "162", "Intuit Inc"),
        ),
        (  # missing effective and count (Lumber Liquidators 2024)
            "9/5/2024 Closure Lumber Liquidators INC",
            ("9/5/2024", None, "Closure", None, "Lumber Liquidators INC"),
        ),
        ("no type token here", None),
    ],
)
def test_parse_left_region(text, expected):
    assert nv._parse_left_region(text) == expected


# ---------------------------------------------------------------------------
# Grid-era header mapping (2017-2020 files)
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class _FakePDF:
    def __init__(self, *tables):
        self.pages = [_FakePage(t) for t in tables]


def test_extract_grid_drops_notice_date_and_junk():
    """2020-style table: 'Notice Date' unmapped, blank/companyless dropped."""
    pdf = _FakePDF(
        [
            [
                "Received Date", "Notice Date", "Effective Date", "Type",
                "Affected Total", "Employer", "City", "County",
            ],
            [
                "2/18/2020", "2/4/2020", "4/12/2020", "Closure", "96",
                "Transform KM LLC", "Las Vegas", "Clark",
            ],
            ["", "", "", "", "", "", "", ""],
            [
                "11/18/2020", "11/16/2020", "1/25/2021", "", "",
                "Southwest Airlines - WARN Rescinded 1/8/21",
                "Las Vegas", "Clark",
            ],
            ["1/1/2020", "1/1/2020", "1/2/2020", "Layoff", "5", "", "X", "Y"],
        ],
        # page 2 has no header row; mapping must carry over
        [
            [
                "9/18/2020", "9/17/2020", "3/23/2020", "Reduced", "75",
                "P.F. Chang's China Bistro", "Reno", "Washoe",
            ],
        ],
    )
    rows = nv._extract_grid(pdf)
    assert len(rows) == 3
    assert rows[0]["received_date"] == "2/18/2020"
    assert rows[0]["effective_date"] == "4/12/2020"  # not the Notice Date
    assert "notice" not in " ".join(rows[0])  # column never mapped
    assert rows[1]["type"] == ""  # rescinded row kept, type as published
    assert rows[2]["type"] == "Reduced"
    assert rows[2]["employer"] == "P.F. Chang's China Bistro"


# ---------------------------------------------------------------------------
# Offline extraction + parse against the real 2026 PDF fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_rows():
    return nv._extract_pdf(FIX_PDF)


def test_extract_pdf_textual(raw_rows):
    assert len(raw_rows) == 17
    first = raw_rows[0]
    assert first["received_date"] == "1/8/2025"  # state's typo, as published
    assert first["effective_date"] == "3/10/2026"
    assert first["type"] == "Layoff"
    assert first["affected"] == "3"
    assert first["employer"] == "SMBC Manubank"
    assert first["city"] == "Remote"
    assert first["county"] == "Remote"
    assert first["notification"] == "Non-WARN"
    # multi-site row whose city/county cells touch in the PDF
    spirit = next(r for r in raw_rows if r["affected"] == "999")
    assert spirit["city"] == "Las Vegas/Reno"
    assert spirit["county"] == "Clark/Washoe"
    # the Intuit row publishes no dates — none invented
    intuit = next(r for r in raw_rows if "Intuit" in r["employer"])
    assert intuit["received_date"] == ""
    assert intuit["effective_date"] == ""
    assert intuit["affected"] == "162"


def test_parse_unified_schema(tmp_path, raw_rows):
    src = warn_sources.get_source("nv", tmp_path)
    src.paths.ensure()
    csv_path = tmp_path / "raw_download.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=nv.RAW_COLUMNS)
        writer.writeheader()
        for rec in raw_rows:
            writer.writerow({**rec, "source_year": 2026})

    df = src.parse(csv_path)
    assert list(df.columns) == [
        "company", "notice_date", "effective_date", "employees",
        "layoff_type", "county", "city",
    ]
    assert len(df) == 17
    assert (df["company"] != "").all()
    for col in ("notice_date", "effective_date"):
        for val in df[col]:
            assert val is None or ISO_DATE.match(val), (col, val)
    assert all(isinstance(v, int) for v in df["employees"])

    first = df.iloc[0]
    assert first["company"] == "SMBC Manubank"
    assert first["notice_date"] == "2025-01-08"
    assert first["effective_date"] == "2026-03-10"
    assert first["employees"] == 3
    assert first["layoff_type"] == "Layoff (Non-WARN)"
    # "unknown" count -> 0
    dig = df[df["company"] == "Dig It Coffee Co"].iloc[0]
    assert dig["employees"] == 0
    assert dig["layoff_type"] == "Closure (Non-WARN)"
    # dateless row stays dateless
    intuit = df[df["company"] == "Intuit Inc"].iloc[0]
    assert intuit["notice_date"] is None
    assert intuit["effective_date"] is None
    assert intuit["employees"] == 162
