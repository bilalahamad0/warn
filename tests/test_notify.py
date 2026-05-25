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
    
    # Mock SMTP instance
    mock_inst = MagicMock()
    mock_smtp.return_value.__enter__.return_value = mock_inst
    
    success = warn_notify.send_email(diff, summary)
    
    assert success is True
    assert mock_inst.login.called
    assert mock_inst.sendmail.called

def test_send_email_no_changes():
    diff = {"new_count": 0}
    summary = {}
    success = warn_notify.send_email(diff, summary)
    assert success is False


def test_recipient_batches_owner_only():
    assert warn_notify._recipient_batches("owner@x.com", []) == [["owner@x.com"]]


def test_recipient_batches_dedupes_owner():
    batches = warn_notify._recipient_batches(
        "owner@x.com", ["a@x.com", "owner@x.com", "b@x.com"]
    )
    assert batches == [["owner@x.com", "a@x.com", "b@x.com"]]


def test_recipient_batches_chunks(monkeypatch):
    monkeypatch.setattr(warn_notify, "MAX_BCC_PER_MESSAGE", 2)
    batches = warn_notify._recipient_batches(
        "owner@x.com", ["a@x.com", "b@x.com", "c@x.com"]
    )
    # First batch carries the owner + first 2 subs; second batch the remainder.
    assert batches == [["owner@x.com", "a@x.com", "b@x.com"], ["c@x.com"]]


@patch("warn_notify.warn_subscribers.get_subscribers", return_value=["sub@x.com"])
@patch("warn_notify.smtplib.SMTP_SSL")
def test_send_email_bccs_subscribers(mock_smtp, mock_subs, monkeypatch):
    """New notices go to NOTIFY_EMAIL and BCC every signup subscriber."""
    monkeypatch.setattr(warn_notify, "GMAIL_USER", "owner@gmail.com")
    monkeypatch.setattr(warn_notify, "GMAIL_APP_PASS", "pass")
    monkeypatch.setattr(warn_notify, "NOTIFY_EMAIL", "owner@gmail.com")
    inst = MagicMock()
    mock_smtp.return_value.__enter__.return_value = inst

    diff = {"new_count": 1, "total_employees_new": 10, "new_entries": []}
    assert warn_notify.send_email(diff, {"total_records": 1}) is True

    # The BCC'd subscriber must be in the envelope recipients.
    recipients = inst.sendmail.call_args[0][1]
    assert "sub@x.com" in recipients
