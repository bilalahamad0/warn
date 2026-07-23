"""Tests for the Georgia (GA) WARN source."""

import csv
import re
from pathlib import Path

import pytest

import warn_sources
from warn_sources import ga as ga_module

FIXTURES = Path(__file__).parent / "fixtures"
CSV_FIXTURE = FIXTURES / "ga_sample.csv"
DETAIL_FIXTURE = FIXTURES / "ga_tcsg_detail.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_georgia():
    assert "ga" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["ga"]
    assert cls.code == "ga"
    assert cls.name == "Georgia"
    assert cls.source_url == "https://www.tcsg.edu/warn-public-view/"


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("ga", tmp_path)
    assert src.paths.root == tmp_path / "states" / "ga"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a real-data fixture (consolidated raw CSV)
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("ga", tmp_path)
    return src.parse(CSV_FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "effective_date",
        "employees",
        "layoff_type",
        "county",
        "city",
        "address",
    ]
    # Georgia publishes no notice date; the column must not exist pre-unify
    # (unify() adds it as None) and must never be copied from another date.
    assert "notice_date" not in parsed.columns


def test_parse_requires_company(parsed):
    assert len(parsed) > 0
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for val in parsed["effective_date"]:
        assert val is None or ISO_DATE.match(val), val


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])


def test_parse_live_row_follows_bln_crosswalk(parsed):
    """TCSG row: effective = First Date of Separation, jobs = Total."""
    row = parsed[parsed["company"] == "Dexter Axle Company"]
    assert len(row) == 1
    assert row["effective_date"].tolist() == ["2023-01-09"]  # 01/09/2023
    assert row["employees"].tolist() == [67]
    assert row["layoff_type"].tolist() == ["Permanent Closure"]
    assert row["county"].tolist() == ["Jasper"]  # " County" suffix stripped
    assert row["city"].tolist() == [""]  # never split out of the address
    assert "199 Perimeter Rd" in row["address"].tolist()[0]


def test_parse_historical_row(parsed):
    """GDOL-era row: city populated, no layoff type or street address."""
    row = parsed[parsed["company"] == "Wallin Drilling, LLC"]
    assert len(row) == 1
    assert row["effective_date"].tolist() == ["2021-12-31"]  # 12/31/2021
    assert row["employees"].tolist() == [8]
    assert row["layoff_type"].tolist() == [""]
    assert row["county"].tolist() == ["Dade"]
    assert row["city"].tolist() == ["Trenton"]
    assert row["address"].tolist() == [""]


def test_parse_drops_blank_company_and_junk_rows(tmp_path):
    junk_csv = tmp_path / "junk.csv"
    with open(junk_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ga_module._COLUMNS, restval="")
        writer.writeheader()
        writer.writerow({"Company Name": "Real Co", "County": "Fulton"})
        writer.writerow({"Company Name": "", "County": "Bibb"})
        writer.writerow({"Company Name": "Company Name"})  # header echo
    src = warn_sources.get_source("ga", tmp_path)
    parsed = src.parse(junk_csv)
    assert parsed["company"].tolist() == ["Real Co"]
    assert parsed["employees"].tolist() == [0]  # no count published -> 0
    assert parsed["effective_date"].tolist() == [None]


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("ga", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "GA").all()
    assert df["notice_date"].isna().all()  # never fabricated
    assert (df["industry"] == "").all()  # GA publishes no industry


# ---------------------------------------------------------------------------
# Detail-page field walk (vendored from BLN warn-scraper)
# ---------------------------------------------------------------------------


def test_parse_detail_fields_from_real_snippet():
    fields = ga_module._parse_detail_fields(DETAIL_FIXTURE.read_text())
    assert fields["GA WARN ID"] == "GA202200071"
    assert fields["Company Name"] == "Dexter Axle Company"
    assert fields["County"] == "Jasper County"
    assert fields["Type of Layoff or Closure"] == "Permanent Closure"
    assert fields["First Date of Separation"] == "01/09/2023"
    assert fields["Total Number of Affected Employees"] == "67"


def test_parse_detail_drops_private_rows():
    fields = ga_module._parse_detail_fields(DETAIL_FIXTURE.read_text())
    assert not any("Email" in k for k in fields)
    assert not any("Submitter Information" in k for k in fields)
    assert not any("Acknowledgement" in k for k in fields)


def test_parse_detail_cleans_address_markup():
    fields = ga_module._parse_detail_fields(DETAIL_FIXTURE.read_text())
    assert fields["First Location Address"] == (
        "199 Perimeter Rd, Monticello, GA 31064, Monticello, Georgia"
    )
    assert "Map It" not in fields["First Location Address"]
    assert "<" not in fields["Company Address"]


# ---------------------------------------------------------------------------
# Ajax plumbing
# ---------------------------------------------------------------------------


def test_extract_ajax_config_prefers_datatables_nonce():
    # Real substrings from the live page (2026-07-21): several nonces
    # exist; the DataTables one sits beside the gv_datatables_data action.
    page = (
        '{"nonce":"7440ee4975"} ... "ajax":{"url":"https:\\/\\/www.tcsg.edu'
        '\\/wp-admin\\/admin-ajax.php","type":"POST","data":'
        '{"action":"gv_datatables_data","view_id":77460,"post_id":77462,'
        '"nonce":"adc6011255","getData":false}}'
    )
    nonce, view_id, post_id = ga_module._extract_ajax_config(page)
    assert nonce == "adc6011255"
    assert view_id == 77460
    assert post_id == 77462


def test_extract_entry_links_handles_mixed_row_shapes():
    # The live index mixes list rows and dict rows (with escaped slashes);
    # links are recovered from the raw text, deduplicated by entry id.
    ajax_text = (
        '{"data":[["<a href=\\"https:\\/\\/www.tcsg.edu\\/warn-public-view'
        '\\/entry\\/41068\\/\\">GA202200071<\\/a>","Dexter Axle Company"],'
        '{"0":"<a href=\\"https:\\/\\/www.tcsg.edu\\/warn-public-view'
        '\\/entry\\/41138\\/\\">GA202200072<\\/a>","gv_marker":[{"url":'
        '"https:\\/\\/www.tcsg.edu\\/warn-public-view\\/entry\\/41138\\/"}]}'
        "]}"
    )
    entries = ga_module._extract_entry_links(ajax_text)
    assert entries == [
        (
            "41068",
            "https://www.tcsg.edu/warn-public-view/entry/41068/",
            "GA202200071",
        ),
        (
            "41138",
            "https://www.tcsg.edu/warn-public-view/entry/41138/",
            "GA202200072",
        ),
    ]
