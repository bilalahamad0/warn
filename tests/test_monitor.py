import pytest
import pandas as pd
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import warn_monitor

def test_fix_company_name():
    assert warn_monitor._fix_company_name("Test &amp; Co") == "Test & Co"
    assert warn_monitor._fix_company_name("Juul Labs") == "Juul Labs, Inc."
    assert warn_monitor._fix_company_name("  Trim  ") == "Trim"

def test_safe_int():
    assert warn_monitor._safe_int("100") == 100
    assert warn_monitor._safe_int("1,200") == 1200
    assert warn_monitor._safe_int("invalid") is None

def test_safe_date():
    assert warn_monitor._safe_date("2026-04-08") == "2026-04-08"
    assert warn_monitor._safe_date(None) is None

@patch("warn_monitor.requests.get")
@patch("warn_monitor._save_meta")
def test_download_xlsx_304(mock_save, mock_get, tmp_path):
    # Mock a 304 response
    mock_resp = MagicMock()
    mock_resp.status_code = 304
    mock_get.return_value = mock_resp
    
    with patch("warn_monitor.LOCAL_XLSX", tmp_path / "file.xlsx"):
        changed, path = warn_monitor.download_xlsx()
        assert changed is False

@patch("warn_monitor.pd.read_excel")
@patch("warn_monitor.pd.ExcelFile")
def test_parse_warn_xlsx(mock_excel_file, mock_read_excel, sample_warn_data):
    # Mock ExcelFile sheets
    mock_xls = MagicMock()
    mock_xls.sheet_names = ["Sheet1"]
    mock_excel_file.return_value = mock_xls
    
    # Mock read_excel result
    df = pd.DataFrame(sample_warn_data)
    mock_read_excel.return_value = df
    
    result_df = warn_monitor.parse_warn_xlsx("fake_path.xlsx")
    assert len(result_df) == 2
    assert "Test Company" in result_df["company"].values

def test_detect_changes_no_snapshot(sample_warn_data, tmp_path):
    # The monitor expects parsed data (lowercase columns)
    df = pd.DataFrame(sample_warn_data)
    df.columns = [c.lower().replace("no. of ", "").replace(" ", "_") for c in df.columns]

    with patch("warn_monitor.SNAPSHOT_FILE", tmp_path / "missing.json"):
        diff = warn_monitor.detect_changes(df)
        assert diff["new_count"] == 2
        assert diff["total_employees_new"] == 150


def _rec(company, county="LA", emp=10, notice="2026-01-01", eff="2026-03-01"):
    return {
        "company": company, "county": county, "city": "",
        "notice_date": notice, "effective_date": eff, "employees": emp,
    }


def test_record_key_distinguishes_multisite():
    # Same company + notice + effective date but different county/headcount
    # are distinct notices and must not collapse together.
    a = _rec("Meta", county="Alameda", emp=81)
    b = _rec("Meta", county="San Mateo", emp=338)
    assert warn_monitor._record_key(a) != warn_monitor._record_key(b)


def test_update_cumulative_unions_dropped_records(tmp_path):
    cum = tmp_path / "warn_cumulative.json"
    with patch("warn_monitor.CUMULATIVE_FILE", cum):
        # Run 1: two notices observed.
        first = [_rec("Acme", emp=10), _rec("Globex", emp=20)]
        warn_monitor.update_cumulative(first)

        # Run 2: EDD re-export drops Globex and adds Initech.
        second = [_rec("Acme", emp=10), _rec("Initech", emp=30)]
        summary = warn_monitor.update_cumulative(second)

    companies = {r["company"] for r in summary["records"]}
    # Globex must survive even though it vanished from the latest file.
    assert companies == {"Acme", "Globex", "Initech"}
    assert summary["total_records"] == 3
    assert summary["total_employees"] == 60


def test_update_cumulative_latest_wins_on_conflict(tmp_path):
    cum = tmp_path / "warn_cumulative.json"
    with patch("warn_monitor.CUMULATIVE_FILE", cum):
        warn_monitor.update_cumulative([_rec("Acme", emp=10)])
        # Same key, corrected layoff_type — latest version should win.
        updated = _rec("Acme", emp=10)
        updated["layoff_type"] = "Closure Permanent"
        summary = warn_monitor.update_cumulative([updated])

    assert summary["total_records"] == 1
    assert summary["records"][0]["layoff_type"] == "Closure Permanent"
