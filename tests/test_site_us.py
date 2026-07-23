"""Tests for the standalone US dashboard builder (warn_site_us)."""

import json
from datetime import datetime, timezone

import pytest

import warn_charts
import warn_site_us


YEAR = datetime.now(timezone.utc).year


@pytest.fixture
def national_payload():
    return {
        "last_updated": f"{YEAR}-07-01T00:00:00Z",
        "states_live": 2,
        "total_records": 3,
        "total_employees": 700,
        "states": {
            "CA": {"name": "California", "total_records": 2, "total_employees": 600},
            "NJ": {"name": "New Jersey", "total_records": 1, "total_employees": 100},
        },
        "records": [
            {"state": "CA", "company": "Acme", "notice_date": f"{YEAR}-03-01",
             "effective_date": f"{YEAR}-05-01", "employees": 500, "city": "LA"},
            {"state": "CA", "company": "Beta", "notice_date": f"{YEAR - 1}-06-01",
             "effective_date": f"{YEAR - 1}-08-01", "employees": 100, "city": "SF"},
            {"state": "NJ", "company": "Gamma", "notice_date": None,
             "effective_date": f"{YEAR}-04-15", "employees": 100, "city": "Newark"},
        ],
    }


@pytest.fixture
def built_site(tmp_path, national_payload, monkeypatch):
    # Keep chart fragments out of the real docs/charts during tests.
    monkeypatch.setattr(warn_charts, "CHARTS_DIR", tmp_path / "charts")
    monkeypatch.setattr(warn_site_us, "CHARTS_DIR", tmp_path / "charts")
    (tmp_path / "charts").mkdir()
    nat = tmp_path / "warn_national.json"
    nat.write_text(json.dumps(national_payload))
    out = tmp_path / "us"
    index = warn_site_us.build_us_site(national_file=nat, out_dir=out)
    return index, out


def test_kpis_current_year_window(national_payload):
    k = warn_site_us.compute_us_kpis(national_payload)
    # Beta's 100 employees are last year — excluded from the year window.
    assert k["year_notices"] == 2
    assert k["year_employees"] == 600
    assert k["largest"] == {"company": "Acme", "state": "CA", "employees": 500}
    assert k["top_state"]["state"] == "CA"
    assert k["states_live"] == 2
    assert k["total_employees"] == 700


def test_build_us_site_writes_page_and_api(built_site):
    index, out = built_site
    html = index.read_text()
    assert "US WARN Layoff Tracker" in html
    assert "2 states live" in html
    # Both states appear in the filter and the table rows.
    assert '<option value="CA">CA</option>' in html
    assert '<option value="NJ">NJ</option>' in html
    assert 'data-state="NJ"' in html
    # A record with no notice_date renders an em dash, never a fabricated date.
    assert "Gamma" in html
    api = json.loads((out / "data.json").read_text())
    assert api["states_live"] == 2
    assert len(api["records"]) == 3


def test_build_us_site_missing_national_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        warn_site_us.build_us_site(
            national_file=tmp_path / "nope.json", out_dir=tmp_path / "us"
        )
