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


# ---------------------------------------------------------------------------
# Amendment detection
# ---------------------------------------------------------------------------


def test_anchor_key_survives_effective_date_revision():
    """The anchor must be identical before/after an effective-date amendment so
    the revised line maps back to the original filing."""
    old = _rec("Black Tiger", county="San Diego", emp=82,
               notice="2026-04-27", eff="2026-05-29")
    new = _rec("Black Tiger", county="San Diego", emp=82,
               notice="2026-04-27", eff="2026-06-28")
    assert warn_monitor._anchor_key(old) == warn_monitor._anchor_key(new)
    # But the notice key (which includes the effective date) differs.
    assert warn_monitor._notice_key(old) != warn_monitor._notice_key(new)


def _bt_feed(eff, emp=82):
    return pd.DataFrame([{
        "company": "Black Tiger", "effective_date": eff, "employees": emp,
        "notice_date": "2026-04-27", "county": "San Diego", "city": "",
        "layoff_type": "Layoff", "address": "Z", "industry": "I",
    }])


def _bt_latest(eff, emp=82):
    return json.dumps({"records": [{
        "company": "Black Tiger", "effective_date": eff, "employees": emp,
        "notice_date": "2026-04-27", "county": "San Diego", "city": "",
    }]})


def test_detect_changes_reports_amendment_not_new_or_removed(tmp_path):
    """An EDD revision of a filing's effective date is classified as an
    amendment — not a new filing and not a withdrawal."""
    ledger, amended, latest = (tmp_path / "n.json", tmp_path / "a.json", tmp_path / "l.json")
    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.AMENDED_FILE", amended), \
         patch("warn_monitor.LATEST_FILE", latest):
        warn_monitor._save_notified_keys({"Black Tiger__2026-05-29__82"})
        latest.write_text(_bt_latest("2026-05-29"))

        diff = warn_monitor.detect_changes(_bt_feed("2026-06-28"))
        assert diff["new_count"] == 0
        assert diff["removed_count"] == 0
        assert diff["amendment_count"] == 1
        a = diff["amendments"][0]
        assert a["old_effective_date"] == "2026-05-29"
        assert a["new_effective_date"] == "2026-06-28"
        assert diff["amendment_keys"] == ["Black Tiger__2026-06-28__82"]
        # The superseded old version is flagged for cumulative eviction, and the
        # revised key joins new_keys so it never later alerts as a brand-new filing.
        assert diff["amend_superseded"][0]["effective_date"] == "2026-05-29"
        assert "Black Tiger__2026-06-28__82" in diff["new_keys"]


def test_amendment_reported_only_once_across_feed_oscillation(tmp_path):
    """Regression for the '1 previously filed notice removed/amended' noise: the
    same amendment must be reported once, never again as the feed swings."""
    ledger, amended, latest = (tmp_path / "n.json", tmp_path / "a.json", tmp_path / "l.json")
    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.AMENDED_FILE", amended), \
         patch("warn_monitor.LATEST_FILE", latest):
        warn_monitor._save_notified_keys({"Black Tiger__2026-05-29__82"})
        latest.write_text(_bt_latest("2026-05-29"))

        # First sight of the amendment → reported, then recorded as sent.
        diff1 = warn_monitor.detect_changes(_bt_feed("2026-06-28"))
        assert diff1["amendment_count"] == 1
        warn_monitor.record_notified_keys(diff1["new_keys"])
        warn_monitor.record_amended_keys(diff1["amendment_keys"])

        # Feed oscillates BACK to the old date → reversion suppressed.
        latest.write_text(_bt_latest("2026-06-28"))
        rev = warn_monitor.detect_changes(_bt_feed("2026-05-29"))
        assert rev["amendment_count"] == 0
        assert rev["new_count"] == 0
        assert rev["removed_count"] == 0

        # Feed swings forward to the amended date AGAIN → must NOT re-report.
        latest.write_text(_bt_latest("2026-05-29"))
        again = warn_monitor.detect_changes(_bt_feed("2026-06-28"))
        assert again["amendment_count"] == 0
        # …but it still flags the stale version for eviction (self-healing dups).
        assert again["amend_superseded"][0]["effective_date"] == "2026-05-29"


def test_amendment_and_new_filing_disambiguated(tmp_path):
    """A run carrying both a revision and a brand-new filing splits them cleanly."""
    ledger, amended, latest = (tmp_path / "n.json", tmp_path / "a.json", tmp_path / "l.json")

    def feed(rows):
        return pd.DataFrame([{
            "company": c, "effective_date": e, "employees": m, "notice_date": n,
            "county": "X", "city": "", "layoff_type": "L", "address": "Z", "industry": "I",
        } for (c, e, m, n) in rows])

    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.AMENDED_FILE", amended), \
         patch("warn_monitor.LATEST_FILE", latest):
        warn_monitor._save_notified_keys({"Acme__2026-06-01__50"})
        latest.write_text(json.dumps({"records": [{
            "company": "Acme", "effective_date": "2026-06-01", "employees": 50,
            "notice_date": "2026-05-01", "county": "X", "city": ""}]}))

        diff = warn_monitor.detect_changes(feed([
            ("Acme", "2026-06-15", 50, "2026-05-01"),   # effective date revised
            ("Globex", "2026-07-01", 30, "2026-05-10"),  # genuinely new filing
        ]))
        assert diff["new_count"] == 1
        assert diff["new_entries"][0]["company"] == "Globex"
        assert diff["amendment_count"] == 1
        assert diff["amendments"][0]["company"] == "Acme"
        assert set(diff["new_keys"]) == {"Globex__2026-07-01__30", "Acme__2026-06-15__50"}


def test_detect_changes_genuine_removal_when_anchor_vanishes(tmp_path):
    """A filing whose whole anchor disappears from the feed is a real removal."""
    ledger, amended, latest = (tmp_path / "n.json", tmp_path / "a.json", tmp_path / "l.json")

    def feed(*companies):
        return pd.DataFrame([{
            "company": c, "effective_date": "2026-06-01", "employees": 100,
            "notice_date": "2026-05-01", "county": "X", "city": "",
            "layoff_type": "L", "address": "Z", "industry": "I",
        } for c in companies])

    with patch("warn_monitor.NOTIFIED_FILE", ledger), \
         patch("warn_monitor.AMENDED_FILE", amended), \
         patch("warn_monitor.LATEST_FILE", latest):
        warn_monitor._save_notified_keys({"A__2026-06-01__100", "B__2026-06-01__100"})
        latest.write_text(json.dumps({"records": [
            {"company": c, "effective_date": "2026-06-01", "employees": 100,
             "notice_date": "2026-05-01", "county": "X", "city": ""}
            for c in ("A", "B")]}))

        diff = warn_monitor.detect_changes(feed("A"))  # B withdrawn entirely
        assert diff["removed_count"] == 1
        assert diff["removed_entries"][0]["company"] == "B"
        assert diff["amendment_count"] == 0


def test_record_amended_keys_accumulates(tmp_path):
    led = tmp_path / "amended.json"
    with patch("warn_monitor.AMENDED_FILE", led):
        warn_monitor.record_amended_keys(["x__2026-06-28__5"])
        warn_monitor.record_amended_keys(["y__2026-07-01__9", "x__2026-06-28__5"])
        assert warn_monitor._load_amended_keys() == {
            "x__2026-06-28__5", "y__2026-07-01__9"
        }


def test_update_cumulative_evicts_superseded_amendment(tmp_path):
    """The pre-amendment version is dropped from the cumulative store so a
    revised notice never shows up twice on the dashboard."""
    cum = tmp_path / "warn_cumulative.json"
    with patch("warn_monitor.CUMULATIVE_FILE", cum):
        old = _rec("Black Tiger", county="San Diego", emp=82,
                   notice="2026-04-27", eff="2026-05-29")
        warn_monitor.update_cumulative([old])
        new = _rec("Black Tiger", county="San Diego", emp=82,
                   notice="2026-04-27", eff="2026-06-28")
        summary = warn_monitor.update_cumulative([new], superseded=[old])

    assert summary["total_records"] == 1
    assert summary["records"][0]["effective_date"] == "2026-06-28"


def test_update_cumulative_collapses_amended_via_ledger(tmp_path):
    """Even when an oscillating feed reintroduces the superseded line, the
    cumulative store keeps only the canonical (recorded-amended) version."""
    cum = tmp_path / "warn_cumulative.json"
    amended = tmp_path / "amended.json"
    with patch("warn_monitor.CUMULATIVE_FILE", cum), \
         patch("warn_monitor.AMENDED_FILE", amended):
        warn_monitor._save_amended_keys({"Black Tiger__2026-06-28__82"})
        old = _rec("Black Tiger", county="San Diego", emp=82,
                   notice="2026-04-27", eff="2026-05-29")
        new = _rec("Black Tiger", county="San Diego", emp=82,
                   notice="2026-04-27", eff="2026-06-28")
        summary = warn_monitor.update_cumulative([old, new])  # both lines present
    assert summary["total_records"] == 1
    assert summary["records"][0]["effective_date"] == "2026-06-28"


def test_update_cumulative_leaves_unamended_multisite_alone(tmp_path):
    """Two genuinely distinct filings that happen to share an anchor must NOT be
    collapsed when neither is a recorded amendment."""
    cum = tmp_path / "warn_cumulative.json"
    amended = tmp_path / "amended.json"
    with patch("warn_monitor.CUMULATIVE_FILE", cum), \
         patch("warn_monitor.AMENDED_FILE", amended):
        warn_monitor._save_amended_keys({"Unrelated__2026-06-28__5"})
        a = _rec("Multi", county="LA", emp=10, notice="2026-04-01", eff="2026-06-01")
        b = _rec("Multi", county="LA", emp=20, notice="2026-04-01", eff="2026-06-15")
        summary = warn_monitor.update_cumulative([a, b])
    # Same anchor, but no recorded amendment for it → both survive.
    assert summary["total_records"] == 2
