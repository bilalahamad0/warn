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


def test_describe_amendment_effective_date_only():
    assert warn_notify._describe_amendment({
        "old_effective_date": "2026-05-29", "new_effective_date": "2026-06-28",
        "old_employees": 82, "new_employees": 82,
    }) == "effective date 2026-05-29 → 2026-06-28"


def test_describe_amendment_headcount_and_date():
    desc = warn_notify._describe_amendment({
        "old_effective_date": "2026-05-29", "new_effective_date": "2026-06-28",
        "old_employees": 82, "new_employees": 90,
    })
    assert "effective date 2026-05-29 → 2026-06-28" in desc
    assert "headcount 82 → 90" in desc


def test_build_html_renders_amendments_and_drops_old_line():
    diff = {
        "new_count": 0, "amendment_count": 1, "removed_count": 0,
        "total_employees_new": 0, "new_entries": [],
        "amendments": [{
            "company": "Black Tiger Medical Transportation",
            "county": "San Diego County",
            "old_effective_date": "2026-05-29", "new_effective_date": "2026-06-28",
            "old_employees": 82, "new_employees": 82,
        }],
    }
    html = warn_notify._build_html(diff, {"total_records": 1000, "total_employees": 5000})
    assert "Amended Notices" in html
    assert "Black Tiger Medical Transportation" in html
    assert "effective date 2026-05-29 → 2026-06-28" in html
    # The old undifferentiated catch-all line must be gone.
    assert "removed/amended" not in html


def test_build_text_includes_amendments():
    diff = {
        "new_count": 0, "amendment_count": 1, "total_employees_new": 0, "new_entries": [],
        "amendments": [{
            "company": "Acme", "county": "LA County",
            "old_effective_date": "2026-01-01", "new_effective_date": "2026-02-01",
            "old_employees": 10, "new_employees": 12,
        }],
    }
    text = warn_notify._build_text(diff, {"total_records": 5})
    assert "Amended notices:" in text
    assert "Acme (LA County)" in text
    assert "headcount 10 → 12" in text


def test_build_html_genuine_withdrawal_wording():
    diff = {"new_count": 1, "amendment_count": 0, "removed_count": 2,
            "total_employees_new": 10, "new_entries": [], "amendments": []}
    html = warn_notify._build_html(diff, {"total_records": 5, "total_employees": 5})
    assert "withdrawn from the official file" in html
    assert "removed/amended" not in html


def test_send_email_amendment_only_fires(mock_env):
    """An update with no new filings but a real amendment still notifies."""
    diff = {
        "new_count": 0, "amendment_count": 1, "total_employees_new": 0, "new_entries": [],
        "amendments": [{
            "company": "Acme", "county": "LA",
            "old_effective_date": "2026-01-01", "new_effective_date": "2026-02-01",
            "old_employees": 10, "new_employees": 10,
        }],
    }
    with patch("warn_notify.warn_subscribers.get_subscribers", return_value=[]), \
         patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        assert warn_notify.send_email(diff, {"total_records": 1}) is True
        assert inst.sendmail.called


def test_send_email_no_new_no_amendments_skips():
    assert warn_notify.send_email(
        {"new_count": 0, "amendment_count": 0}, {}
    ) is False


@patch("warn_notify.warn_subscribers.get_subscribers", return_value=["sub@x.com"])
@patch("warn_notify.smtplib.SMTP_SSL")
def test_send_email_bccs_subscribers(mock_smtp, mock_subs, monkeypatch):
    """New notices go to NOTIFY_EMAIL and BCC every signup subscriber."""
    monkeypatch.setenv("GMAIL_USER", "owner@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
    monkeypatch.setenv("NOTIFY_EMAIL", "owner@gmail.com")
    inst = MagicMock()
    mock_smtp.return_value.__enter__.return_value = inst

    diff = {"new_count": 1, "total_employees_new": 10, "new_entries": []}
    assert warn_notify.send_email(diff, {"total_records": 1}) is True

    # The BCC'd subscriber must be in the envelope recipients.
    recipients = inst.sendmail.call_args[0][1]
    assert "sub@x.com" in recipients
