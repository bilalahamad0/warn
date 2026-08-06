"""Tests for warn_datasets — the derivation that keeps the two dashboards
reporting the same numbers for California.

Before this module existed the California dashboard read the live EDD feed
(earliest notice 2025-01-29) while the US dashboard read the national dataset,
whose California slice also carries a backfill reaching back to 2008. The two
pages disagreed: 756 California notices for calendar 2025 on one, 827 on the
other. These tests pin the shape of the fix.
"""

import json

import pytest

import warn_datasets


@pytest.fixture(autouse=True)
def _clear_cache():
    warn_datasets.reset_cache()
    yield
    warn_datasets.reset_cache()


@pytest.fixture
def national(tmp_path):
    """A national payload spanning the coverage boundary, with another state."""
    payload = {
        "last_updated": "2026-08-01T00:00:00Z",
        "total_records": 5,
        "total_employees": 999,
        "records": [
            # Inside the window, full schema.
            {"state": "CA", "company": "Acme", "notice_date": "2026-03-01",
             "effective_date": "2026-05-01", "employees": 500,
             "layoff_type": "Layoff Permanent", "county": "Los Angeles County",
             "city": "LA", "address": "1 Main St", "industry": "Retail"},
            # Inside the window, on the boundary itself, backfill shape: no
            # "city" and no "industry" key at all.
            {"state": "CA", "company": "Boundary", "notice_date": "2025-01-01",
             "effective_date": "2025-03-01", "employees": 10,
             "layoff_type": "Closure", "county": "Alameda County",
             "address": "2 Oak Ave"},
            # Inside the window, the kind of record the live feed never had.
            {"state": "CA", "company": "Gap Filler", "notice_date": "2025-01-15",
             "effective_date": "2025-03-15", "employees": 61,
             "layoff_type": "Layoff Permanent", "county": "Alameda County"},
            # Outside the window — deep history, no industry data.
            {"state": "CA", "company": "Ancient", "notice_date": "2020-04-15",
             "effective_date": "2020-03-19", "employees": 251,
             "layoff_type": "Layoff Temporary", "county": "Los Angeles County"},
            # Another state entirely.
            {"state": "NJ", "company": "Gamma", "notice_date": "2026-04-01",
             "effective_date": "2026-06-01", "employees": 100},
        ],
    }
    path = tmp_path / "warn_national.json"
    path.write_text(json.dumps(payload))
    return path


def test_covers_the_era_and_nothing_else(national):
    payload = warn_datasets.build_ca_dashboard(national)
    companies = {r["company"] for r in payload["records"]}

    assert companies == {"Acme", "Boundary", "Gap Filler"}
    assert "Ancient" not in companies      # before the coverage boundary
    assert "Gamma" not in companies        # not California


def test_the_boundary_is_inclusive(national):
    """CA_COVERAGE_START is the first covered day, not the first excluded one —
    otherwise the 2025 KPI year is short by a day and stops matching the US
    dashboard's California slice."""
    payload = warn_datasets.build_ca_dashboard(national)
    dates = [r["notice_date"] for r in payload["records"]]
    assert warn_datasets.CA_COVERAGE_START in dates


def test_totals_are_recomputed_not_inherited(national):
    """The national payload's totals count 47 jurisdictions. Copying them would
    put the whole country's numbers on California's KPI cards."""
    payload = warn_datasets.build_ca_dashboard(national)
    assert payload["total_records"] == 3
    assert payload["total_employees"] == 571          # 500 + 10 + 61
    assert payload["total_employees"] != 999
    assert payload["date_range_start"] == "2025-01-01"
    assert payload["date_range_end"] == "2026-03-01"


def test_records_are_schema_normalised(national):
    """docs/ca/data.json is a public API. The backfilled records omit "city"
    and "industry" as keys, so every record is projected onto one field set."""
    payload = warn_datasets.build_ca_dashboard(national)
    keysets = {tuple(sorted(r)) for r in payload["records"]}
    assert len(keysets) == 1
    assert set(keysets.pop()) == set(warn_datasets.CA_RECORD_FIELDS)

    boundary = next(r for r in payload["records"] if r["company"] == "Boundary")
    assert boundary["industry"] == ""     # present and empty, never absent
    assert boundary["city"] == ""
    assert boundary["state"] == "CA"
    assert isinstance(boundary["employees"], int)


def test_records_are_newest_first(national):
    payload = warn_datasets.build_ca_dashboard(national)
    dates = [r["notice_date"] for r in payload["records"]]
    assert dates == sorted(dates, reverse=True)


def test_payload_declares_its_scope_and_boundary(national):
    payload = warn_datasets.build_ca_dashboard(national)
    assert payload["scope"] == "ca"
    assert payload["coverage_start"] == warn_datasets.CA_COVERAGE_START


def test_no_covered_era_record_is_dropped(national):
    """The conservation invariant.

    A constant cannot promise the two dashboards agree; this can. Every
    California record in the national dataset dated on or after the coverage
    boundary must survive into the derived payload. Fails loudly if the
    backfill is extended, the aggregation merge changes, or the boundary moves.
    """
    source = json.loads(national.read_text())["records"]
    expected = {
        (r["company"], r["notice_date"])
        for r in source
        if r.get("state") == "CA"
        and r["notice_date"] >= warn_datasets.CA_COVERAGE_START
    }
    derived = {
        (r["company"], r["notice_date"])
        for r in warn_datasets.build_ca_dashboard(national)["records"]
    }
    assert expected <= derived


def test_missing_national_dataset_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        warn_datasets.build_ca_dashboard(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# Source resolution and the degraded fallback
# ---------------------------------------------------------------------------


def test_falls_back_to_the_raw_store_when_national_is_missing(
    tmp_path, monkeypatch
):
    """A derivation failure must leave a working page rather than no page — but
    the payload carries no coverage_start, which is how warn_publish knows to
    warn on the page that the totals are under-reporting."""
    cumulative = tmp_path / "warn_cumulative.json"
    cumulative.write_text(json.dumps({"records": [{"company": "Live"}]}))
    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(warn_datasets, "CUMULATIVE_FILE", cumulative)

    payload = warn_datasets.load_ca_dashboard()
    assert payload["records"] == [{"company": "Live"}]
    assert "coverage_start" not in payload


def test_falls_back_to_latest_when_the_cumulative_store_is_gone(
    tmp_path, monkeypatch
):
    latest = tmp_path / "warn_latest.json"
    latest.write_text(json.dumps({"records": [{"company": "Fresh"}]}))
    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(warn_datasets, "CUMULATIVE_FILE", tmp_path / "gone.json")
    monkeypatch.setattr(warn_datasets, "LATEST_FILE", latest)

    assert warn_datasets.load_ca_dashboard()["records"] == [{"company": "Fresh"}]


def test_raises_when_there_is_no_dataset_at_all(tmp_path, monkeypatch):
    for name in ("NATIONAL_FILE", "CUMULATIVE_FILE", "LATEST_FILE"):
        monkeypatch.setattr(warn_datasets, name, tmp_path / f"no_{name}.json")
    with pytest.raises(FileNotFoundError):
        warn_datasets.load_ca_dashboard()


def test_publish_and_charts_read_the_identical_records(national, monkeypatch):
    """The anti-drift guard.

    warn_publish (KPIs, notices table) and warn_charts (nine California charts)
    used to resolve the dataset independently, which let one page show KPIs
    computed over one record set and charts drawn over another.
    """
    import warn_charts
    import warn_publish

    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", national)
    warn_datasets.reset_cache()

    publish_records = warn_publish._dashboard_records()
    charts_df, charts_payload = warn_charts.load_data()

    assert len(publish_records) == len(charts_df) == 3
    assert charts_payload["records"] == publish_records
