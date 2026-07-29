from email import message_from_string
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

import warn_notify


def test_build_text(sample_warn_data):
    # Normalize keys to lowercase as produced by the monitor
    entries = []
    for r in sample_warn_data:
        entries.append({
            k.lower().replace("no. of ", "").replace(" ", "_"): v
            for k, v in r.items()
        })

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
    html = warn_notify._build_html(
        diff, {"total_records": 1000, "total_employees": 5000}
    )
    assert "Amended Notices" in html
    assert "Black Tiger Medical Transportation" in html
    assert "effective date 2026-05-29 → 2026-06-28" in html
    # The old undifferentiated catch-all line must be gone.
    assert "removed/amended" not in html


def test_build_text_includes_amendments():
    diff = {
        "new_count": 0, "amendment_count": 1, "total_employees_new": 0,
        "new_entries": [],
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
        "new_count": 0, "amendment_count": 1, "total_employees_new": 0,
        "new_entries": [],
        "amendments": [{
            "company": "Acme", "county": "LA",
            "old_effective_date": "2026-01-01", "new_effective_date": "2026-02-01",
            "old_employees": 10, "new_employees": 10,
        }],
    }
    with patch(
        "warn_notify.warn_subscribers.get_subscriber_records", return_value=[]
    ), patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        assert warn_notify.send_email(diff, {"total_records": 1}) is True
        assert inst.sendmail.called


def test_send_email_no_new_no_amendments_skips():
    assert warn_notify.send_email(
        {"new_count": 0, "amendment_count": 0}, {}
    ) is False


def _sub(email, states=("CA",), digest=False):
    """A subscriber record in the shape warn_subscribers returns."""
    return {"email": email, "name": "", "states": list(states), "digest": digest}


@patch(
    "warn_notify.warn_subscribers.get_subscriber_records",
    return_value=[_sub("sub@x.com")],
)
@patch("warn_notify.smtplib.SMTP_SSL")
def test_send_email_bccs_subscribers(mock_smtp, mock_subs, monkeypatch):
    """New notices go to NOTIFY_EMAIL and BCC every subscriber for that state."""
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


# ---------------------------------------------------------------------------
# Per-state alert routing
# ---------------------------------------------------------------------------

_NEW_DIFF = {"new_count": 1, "total_employees_new": 10, "new_entries": []}

# One CA-only subscriber, one IL-only, one who wants CA and NY, and one who
# only ever wanted the whole-US monthly digest.
_ROUTING_RECORDS = [
    _sub("ca-only@x.com", ["CA"]),
    _sub("il-only@x.com", ["IL"]),
    _sub("ca-ny@x.com", ["CA", "NY"]),
    _sub("digest-only@x.com", [], digest=True),
]


def _send_for_state(state, records=_ROUTING_RECORDS, diff=None):
    """Send one alert with SMTP mocked; return (result, envelope recipients)."""
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        ok = warn_notify.send_email(
            diff if diff is not None else _NEW_DIFF,
            {"total_records": 1},
            state=state,
            records=records,
        )
    recipients = [
        addr for call in inst.sendmail.call_args_list for addr in call[0][1]
    ]
    return ok, recipients


def test_alert_reaches_only_that_states_subscribers(mock_env):
    """An IL alert must not leak to someone who only asked for California."""
    ok, recipients = _send_for_state("IL")
    assert ok is True
    assert "il-only@x.com" in recipients
    assert "ca-only@x.com" not in recipients
    assert "ca-ny@x.com" not in recipients
    assert "digest-only@x.com" not in recipients
    # The operator is always on the alert.
    assert "notify@example.com" in recipients


def test_multi_state_subscriber_gets_every_state_they_chose(mock_env):
    """states=["CA","NY"] means both alerts land — and nothing else does."""
    _, ca_recipients = _send_for_state("CA")
    _, ny_recipients = _send_for_state("NY")
    assert "ca-ny@x.com" in ca_recipients
    assert "ca-ny@x.com" in ny_recipients
    # The CA-only subscriber gets CA but never NY.
    assert "ca-only@x.com" in ca_recipients
    assert "ca-only@x.com" not in ny_recipients
    # IL-only sees neither.
    assert "il-only@x.com" not in ca_recipients
    assert "il-only@x.com" not in ny_recipients


def test_alert_for_unsubscribed_state_still_reaches_operator(mock_env):
    """Nobody subscribed to WA — the operator must still be told."""
    ok, recipients = _send_for_state("WA")
    assert ok is True
    assert recipients == ["notify@example.com"]


def _decoded_subject(raw: str) -> str:
    """Subject of a raw message, un-RFC2047'd (emoji force encoded headers)."""
    return str(make_header(decode_header(message_from_string(raw)["Subject"])))


def test_subject_and_heading_name_the_state(mock_env):
    """The state is named in plain English, not just as a 2-letter code."""
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, state="IL", records=[]
        )
    assert "new Illinois layoff notice" in _decoded_subject(
        inst.sendmail.call_args[0][2]
    )
    assert "Illinois WARN Alert" in warn_notify._build_text(
        _NEW_DIFF, {"total_records": 1}, "IL"
    )
    assert "Illinois WARN Alert" in warn_notify._build_html(
        _NEW_DIFF, {"total_records": 1}, "IL"
    )


def test_state_defaults_to_california(mock_env):
    """Existing call sites that pass no state keep getting CA alerts."""
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, records=_ROUTING_RECORDS
        )
    recipients = inst.sendmail.call_args[0][1]
    assert "ca-only@x.com" in recipients
    assert "il-only@x.com" not in recipients


def test_routing_uses_passed_records_without_refetching(mock_env):
    """records= is threaded through, so a multi-state run fetches once."""
    with patch(
        "warn_notify.warn_subscribers.get_subscriber_records"
    ) as mock_fetch, patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = MagicMock()
        warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, state="CA", records=_ROUTING_RECORDS
        )
    assert not mock_fetch.called


def test_load_subscriber_records_swallows_failures():
    """A dead signup sheet degrades to operator-only, never an exception."""
    with patch(
        "warn_notify.warn_subscribers.get_subscriber_records",
        side_effect=RuntimeError("sheet down"),
    ):
        assert warn_notify.load_subscriber_records() == []


def test_alert_send_failure_returns_false(mock_env):
    """An SMTP failure is reported, so the caller can skip its ledger write."""
    with patch("warn_notify.smtplib.SMTP_SSL", side_effect=OSError("boom")):
        assert warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, state="CA", records=[]
        ) is False


# ---------------------------------------------------------------------------
# Monthly US digest
# ---------------------------------------------------------------------------

_DIGEST = {
    "subject": "📊 US WARN digest — June 2026",
    "html": "<h1>June 2026</h1>",
    "text": "June 2026 digest",
}


def test_digest_goes_only_to_digest_subscribers(mock_env):
    """Per-notice subscribers must not receive the monthly digest."""
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        assert warn_notify.send_monthly_digest(
            _DIGEST, records=_ROUTING_RECORDS
        ) is True

    recipients = [
        addr for call in inst.sendmail.call_args_list for addr in call[0][1]
    ]
    assert "digest-only@x.com" in recipients
    assert "notify@example.com" in recipients          # operator
    assert "ca-only@x.com" not in recipients
    assert "il-only@x.com" not in recipients
    assert "ca-ny@x.com" not in recipients

    assert "US WARN digest" in _decoded_subject(inst.sendmail.call_args[0][2])


def test_digest_carries_html_and_text_alternatives(mock_env):
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        warn_notify.send_monthly_digest(_DIGEST, records=[])
    raw = inst.sendmail.call_args[0][2]
    assert "text/plain" in raw
    assert "text/html" in raw


def test_digest_with_empty_payload_is_not_sent(mock_env):
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        assert warn_notify.send_monthly_digest({}, records=[]) is False
        assert not mock_smtp.called


def test_digest_batches_large_subscriber_lists(mock_env, monkeypatch):
    """Gmail's per-message recipient cap applies to the digest too."""
    monkeypatch.setattr(warn_notify, "MAX_BCC_PER_MESSAGE", 2)
    records = [_sub(f"d{i}@x.com", [], digest=True) for i in range(5)]
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        assert warn_notify.send_monthly_digest(_DIGEST, records=records) is True
    # 5 subscribers at 2 per message = 3 envelopes.
    assert inst.sendmail.call_count == 3
    delivered = {
        addr for call in inst.sendmail.call_args_list for addr in call[0][1]
    }
    assert delivered == {f"d{i}@x.com" for i in range(5)} | {
        "notify@example.com"
    }


def test_digest_send_failure_returns_false(mock_env):
    with patch("warn_notify.smtplib.SMTP_SSL", side_effect=OSError("boom")):
        assert warn_notify.send_monthly_digest(_DIGEST, records=[]) is False
