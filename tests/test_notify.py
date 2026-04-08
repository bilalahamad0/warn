import pytest
from unittest.mock import MagicMock, patch
import warn_notify

def test_build_text(sample_warn_data):
    # Normalize keys to lowercase as produced by the monitor
    entries = []
    for r in sample_warn_data:
        entries.append({k.lower().replace("no. of ", "").replace(" ", "_"): v for k, v in r.items()})

    diff = {
        "new_count": 2,
        "total_employees_new": 150,
        "new_entries": entries,
    }
    summary = {"total_records": 1000}
    text = warn_notify._build_text(diff, summary)
    assert "California WARN Alert" in text
    assert "New notices: 2" in text
    assert "Test Company" in text

@patch("warn_notify.smtplib.SMTP_SSL")
def test_send_email_success(mock_smtp, sample_warn_data, mock_env):
    diff = {
        "new_count": 1,
        "total_employees_new": 100,
        "new_entries": sample_warn_data[:1],
    }
    summary = {"total_records": 1000}
    
    # Mock SMTP instance and module-level environment variables
    mock_inst = MagicMock()
    mock_smtp.return_value.__enter__.return_value = mock_inst
    
    # Patch the global variables because they are loaded at import time
    with patch("warn_notify.GMAIL_USER", "test@gmail.com"), \
         patch("warn_notify.GMAIL_APP_PASS", "test_pass"):
        success = warn_notify.send_email(diff, summary)
    
    assert success is True
    assert mock_inst.login.called
    assert mock_inst.sendmail.called

def test_send_email_no_changes():
    diff = {"new_count": 0}
    summary = {}
    success = warn_notify.send_email(diff, summary)
    assert success is False
