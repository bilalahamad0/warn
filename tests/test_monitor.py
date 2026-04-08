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
