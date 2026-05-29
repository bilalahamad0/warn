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

def _parsed_df(sample_warn_data):
    """Mimic the monitor's parsed output (lowercase, underscored columns)."""
    df = pd.DataFrame(sample_warn_data)
    df.columns = [c.lower().replace("no. of ", "").replace(" ", "_") for c in df.columns]
    return df


def test_detect_changes_bootstrap_no_ledger(sample_warn_data, tmp_path):
    """First ever run (no ledger): establish a baseline, alert nothing, seed ledger."""
    df = _parsed_df(sample_warn_data)
    ledger = tmp_path / "notified.json"
    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.LATEST_FILE", tmp_path / "missing-latest.json"):
        diff = warn_monitor.detect_changes(df)
        assert diff["new_count"] == 0
        assert diff["new_entries"] == []
        # Ledger seeded with both existing notices so they never alert later.
        assert ledger.exists()
        assert len(warn_monitor._load_notified_keys()) == 2


def test_detect_changes_reports_only_unseen(sample_warn_data, tmp_path):
    """With a ledger present, only notices whose keys are absent count as new."""
    df = _parsed_df(sample_warn_data)
    ledger = tmp_path / "notified.json"
    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.LATEST_FILE", tmp_path / "missing-latest.json"):
        # Pre-seed with only "Another Co" — "Test Company" is unseen.
        warn_monitor._save_notified_keys({"Another Co__2026-03-01__50"})
        diff = warn_monitor.detect_changes(df)
        assert diff["new_count"] == 1
        assert diff["new_entries"][0]["company"] == "Test Company"
        assert diff["total_employees_new"] == 100


def test_no_duplicate_alert_on_feed_oscillation(tmp_path):
    """Regression for the duplicate-email bug: a notice already alerted on must
    NOT alert again when the EDD feed drops it and later serves it again."""
    ledger = tmp_path / "notified.json"
    latest = tmp_path / "latest.json"

    def feed(*companies):
        return pd.DataFrame([
            {"company": c, "effective_date": "2026-06-01", "employees": 100,
             "notice_date": "2026-05-01", "county": "X", "city": "Y",
             "layoff_type": "Layoff", "address": "Z", "industry": "I"}
            for c in companies
        ])

    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.LATEST_FILE", latest):
        warn_monitor._save_notified_keys({"A__2026-06-01__100", "B__2026-06-01__100"})

        # Feed swings UP to include C → C is genuinely new, alerts once.
        diff1 = warn_monitor.detect_changes(feed("A", "B", "C"))
        assert diff1["new_count"] == 1
        assert diff1["new_keys"] == ["C__2026-06-01__100"]
        warn_monitor.record_notified_keys(diff1["new_keys"])  # recorded after send

        # Feed reverts (C dropped) → no alert.
        latest.write_text(json.dumps({"records": [
            {"company": c, "effective_date": "2026-06-01", "employees": 100}
            for c in ("A", "B", "C")
        ]}))
        assert warn_monitor.detect_changes(feed("A", "B"))["new_count"] == 0

        # Feed swings UP again to include C → must NOT re-alert (the fix).
        assert warn_monitor.detect_changes(feed("A", "B", "C"))["new_count"] == 0


def test_record_notified_keys_accumulates(tmp_path):
    ledger = tmp_path / "notified.json"
    with patch("warn_monitor.NOTIFIED_FILE", ledger):
        warn_monitor.record_notified_keys(["a__1__10"])
        warn_monitor.record_notified_keys(["b__2__20", "a__1__10"])  # dup ignored
        assert warn_monitor._load_notified_keys() == {"a__1__10", "b__2__20"}


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
