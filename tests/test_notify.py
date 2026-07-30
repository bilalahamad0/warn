import smtplib
from email import message_from_string
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

import pytest

import warn_notify
import warn_subscribers


@pytest.fixture(autouse=True)
def _unsigned_by_default(monkeypatch):
    """Default every test to the unsigned path unless it asks for `signed`.

    Without this, a SUBSCRIBERS_TOKEN in the ambient environment (CI exports it
    for the pipeline step) would silently flip delivery to personalised mode and
    change what `sendmail.call_args` means for the older assertions here.
    """
    monkeypatch.delenv("SUBSCRIBERS_TOKEN", raising=False)


# The shared contract's reference token (see warn_subscribers): with it,
# "me@example.com" must sign to b71371db806cc29b7660ac1369591ea5.
SIGNING_TOKEN = "test-secret-123"


@pytest.fixture
def signed(monkeypatch):
    """Turn on signed, per-recipient unsubscribe links."""
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", SIGNING_TOKEN)
    return SIGNING_TOKEN


def _sent(inst):
    """Every sendmail call as (envelope_recipients, parsed message)."""
    return [
        (list(call[0][1]), message_from_string(call[0][2]))
        for call in inst.sendmail.call_args_list
    ]


def _by_recipient(inst):
    """Parsed messages keyed by their single envelope recipient."""
    return {recips[0]: msg for recips, msg in _sent(inst) if len(recips) == 1}


def _parts(msg) -> dict:
    """{content_type: decoded body} for one message."""
    out = {}
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        out[part.get_content_type()] = part.get_payload(decode=True).decode()
    return out


def _body(msg) -> str:
    """Every part of a message concatenated — for 'appears nowhere' checks."""
    return "".join(_parts(msg).values())


def _href(url: str) -> str:
    """How ``url`` appears in an HTML body: `&` escaped, per the markup spec.

    A client unescapes this back to exactly ``url`` — the same one the
    List-Unsubscribe header carries.
    """
    return 'href="' + url.replace("&", "&amp;") + '"'


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


# ---------------------------------------------------------------------------
# Signed per-recipient unsubscribe links
# ---------------------------------------------------------------------------

_TWO_SUBS = [_sub("alice@x.com"), _sub("bob@x.com")]


def _send_alert(records, state="CA", diff=None):
    """Send one alert with SMTP mocked; return (result, smtp class, instance)."""
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        ok = warn_notify.send_email(
            diff if diff is not None else _NEW_DIFF,
            {"total_records": 1},
            state=state,
            records=records,
        )
    return ok, mock_smtp, inst


def _send_digest(records, digest=None):
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = inst
        ok = warn_notify.send_monthly_digest(
            digest if digest is not None else _DIGEST, records=records
        )
    return ok, mock_smtp, inst


def test_link_matches_the_shared_contract_reference_vector(monkeypatch):
    """The exact vector both sides of the contract must reproduce.

    warn_notify only mints links; the Apps Script re-derives the signature with
    the same key before honouring one. If either side drifts, every link in
    every email silently stops verifying — so pin the value here too.
    """
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", "test-secret-123")
    assert warn_notify._unsubscribe_link("me@example.com") == (
        "https://bilalahamad0.github.io/warn/unsubscribe.html"
        "?e=me%40example.com&s=b71371db806cc29b7660ac1369591ea5"
    )


def test_link_is_empty_without_a_token():
    assert warn_notify._unsubscribe_link("me@example.com") == ""


def test_link_failure_degrades_to_no_link(signed):
    """A blow-up in link minting must not take the alert down with it."""
    with patch(
        "warn_notify.warn_subscribers.unsubscribe_url",
        side_effect=RuntimeError("nope"),
    ):
        assert warn_notify._unsubscribe_link("a@x.com") == ""


def test_each_subscriber_gets_a_link_signed_for_their_own_address(
    mock_env, signed
):
    """The whole point: Alice's link unsubscribes Alice, never Bob.

    A BCC blast physically cannot do this — one body, one link — which is why
    subscriber delivery is personalised.
    """
    ok, _, inst = _send_alert(_TWO_SUBS)
    assert ok is True

    msgs = _by_recipient(inst)
    assert set(msgs) == {"notify@example.com", "alice@x.com", "bob@x.com"}

    alice_sig = warn_subscribers.unsubscribe_signature("alice@x.com")
    bob_sig = warn_subscribers.unsubscribe_signature("bob@x.com")
    assert alice_sig and bob_sig and alice_sig != bob_sig

    alice_body, bob_body = _body(msgs["alice@x.com"]), _body(msgs["bob@x.com"])
    assert alice_sig in alice_body and bob_sig not in alice_body
    assert bob_sig in bob_body and alice_sig not in bob_body
    # The address the link acts on is theirs too, not just the signature.
    assert "alice%40x.com" in alice_body and "bob%40x.com" not in alice_body
    assert "bob%40x.com" in bob_body and "alice%40x.com" not in bob_body


def test_personalised_messages_never_expose_another_subscriber(mock_env, signed):
    """Their own address in To, one envelope recipient — no BCC, no leak."""
    _, _, inst = _send_alert(_TWO_SUBS)
    for recips, msg in _sent(inst):
        assert len(recips) == 1
        assert msg["To"] == recips[0]
    msgs = _by_recipient(inst)
    assert "bob@x.com" not in msgs["alice@x.com"].as_string()
    assert "alice@x.com" not in msgs["bob@x.com"].as_string()


def test_operator_copy_is_unchanged(mock_env, signed):
    """NOTIFY_EMAIL is the operator, not a subscriber — no signed link."""
    _, _, inst = _send_alert(_TWO_SUBS)
    op = _by_recipient(inst)["notify@example.com"]
    assert op["List-Unsubscribe"] == "<mailto:test@gmail.com?subject=unsubscribe>"
    assert op["List-Unsubscribe-Post"] is None
    body = _body(op)
    assert "unsubscribe.html?" not in body
    assert "reply to this email" in body
    # …and no subscriber's address rides along on it.
    assert "alice@x.com" not in op.as_string()


def test_list_unsubscribe_headers_match_the_body_link(mock_env, signed):
    """Mail clients render their unsubscribe affordance off this header.

    No List-Unsubscribe-Post: the landing page is a static asset that cannot
    accept the RFC 8058 POST, and unsubscribing requires confirming a
    selection — advertising one-click would strand the user on a dead POST
    believing they had been removed.
    """
    _, _, inst = _send_alert(_TWO_SUBS)
    msgs = _by_recipient(inst)
    for addr in ("alice@x.com", "bob@x.com"):
        url = warn_subscribers.unsubscribe_url(addr)
        msg = msgs[addr]
        assert msg["List-Unsubscribe"] == f"<{url}>"
        assert msg["List-Unsubscribe-Post"] is None
        parts = _parts(msg)
        # Visible in BOTH alternatives, and the same URL the headers carry.
        assert f"Manage or cancel these alerts: {url}" in parts["text/plain"]
        assert "Manage or cancel these alerts" in parts["text/html"]
        assert _href(url) in parts["text/html"]
        # The pre-link instruction is replaced, not doubled up.
        assert "reply to this email" not in parts["text/plain"]


def test_personalised_send_reuses_one_smtp_connection(mock_env, signed):
    """One login for the whole run — not one handshake per subscriber."""
    records = [_sub(f"s{i}@x.com") for i in range(5)]
    _, mock_smtp, inst = _send_alert(records)
    assert mock_smtp.call_count == 1
    assert inst.login.call_count == 1
    assert inst.sendmail.call_count == 6          # operator + 5 subscribers


def test_unsigned_send_carries_no_link_and_still_delivers(mock_env):
    """SUBSCRIBERS_TOKEN unset: exactly the pre-link behaviour, no broken URL."""
    ok, _, inst = _send_alert(_TWO_SUBS)
    assert ok is True

    sent = _sent(inst)
    assert len(sent) == 1                        # one BCC-batched message
    recips, msg = sent[0]
    assert set(recips) == {"notify@example.com", "alice@x.com", "bob@x.com"}
    assert msg["List-Unsubscribe-Post"] is None
    assert msg["List-Unsubscribe"] == "<mailto:test@gmail.com?subject=unsubscribe>"
    body = _body(msg)
    assert "unsubscribe.html" not in body        # no half-built link
    assert "&s=" not in body
    assert "reply to this email" in body


def test_unsigned_state_alert_is_logged_once(mock_env, caplog):
    """The operator is told why links are missing — once, not per recipient."""
    with caplog.at_level("INFO", logger="warn_notify"):
        _send_alert(_TWO_SUBS)
    hits = [r for r in caplog.records if "SUBSCRIBERS_TOKEN not set" in r.message]
    assert len(hits) == 1


def test_no_unsigned_warning_when_there_are_no_subscribers(mock_env, caplog):
    """Operator-only sends have nobody to link, so nothing to explain."""
    with caplog.at_level("INFO", logger="warn_notify"):
        _send_alert([])
    assert not [r for r in caplog.records if "SUBSCRIBERS_TOKEN" in r.message]


def test_digest_carries_the_same_signed_treatment(mock_env, signed):
    records = [
        _sub("d1@x.com", [], digest=True),
        _sub("d2@x.com", [], digest=True),
    ]
    ok, mock_smtp, inst = _send_digest(records)
    assert ok is True
    assert mock_smtp.call_count == 1              # still one connection

    msgs = _by_recipient(inst)
    assert set(msgs) == {"notify@example.com", "d1@x.com", "d2@x.com"}
    for addr in ("d1@x.com", "d2@x.com"):
        url = warn_subscribers.unsubscribe_url(addr)
        msg = msgs[addr]
        assert msg["List-Unsubscribe"] == f"<{url}>"
        # No one-click promise — see test_list_unsubscribe_headers_*.
        assert msg["List-Unsubscribe-Post"] is None
        parts = _parts(msg)
        assert f"Manage or cancel these alerts: {url}" in parts["text/plain"]
        assert _href(url) in parts["text/html"]
        # The digest's own body survives the appended footer.
        assert "June 2026" in parts["text/html"]

    other = warn_subscribers.unsubscribe_signature("d2@x.com")
    assert other not in _body(msgs["d1@x.com"])
    # The operator's digest copy is untouched.
    assert msgs["notify@example.com"]["List-Unsubscribe-Post"] is None


def test_unsigned_digest_still_delivers_without_a_link(mock_env):
    records = [_sub("d1@x.com", [], digest=True)]
    ok, _, inst = _send_digest(records)
    assert ok is True
    sent = _sent(inst)
    assert len(sent) == 1
    assert set(sent[0][0]) == {"notify@example.com", "d1@x.com"}
    assert "unsubscribe.html" not in _body(sent[0][1])


def test_digest_without_html_still_gets_a_text_footer(mock_env, signed):
    """A text-only digest must not grow an empty HTML alternative."""
    payload = {"subject": "s", "html": "", "text": "just text"}
    _, _, inst = _send_digest(
        [_sub("d1@x.com", [], digest=True)], digest=payload
    )
    parts = _parts(_by_recipient(inst)["d1@x.com"])
    assert set(parts) == {"text/plain"}
    assert warn_subscribers.unsubscribe_url("d1@x.com") in parts["text/plain"]


def test_a_refused_address_does_not_sink_the_rest_of_the_send(mock_env, signed):
    """One dead mailbox must not strand the alert ledger and re-mail everyone."""
    def sendmail(sender, recips, raw):
        if "bob@x.com" in recips:
            raise smtplib.SMTPRecipientsRefused({"bob@x.com": (550, b"no such")})

    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        inst.sendmail.side_effect = sendmail
        mock_smtp.return_value.__enter__.return_value = inst
        ok = warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, state="CA", records=_TWO_SUBS
        )
    assert ok is True
    assert inst.sendmail.call_count == 3          # attempted all three


def test_send_is_a_failure_when_every_address_is_refused(mock_env, signed):
    with patch("warn_notify.smtplib.SMTP_SSL") as mock_smtp:
        inst = MagicMock()
        inst.sendmail.side_effect = smtplib.SMTPRecipientsRefused({})
        mock_smtp.return_value.__enter__.return_value = inst
        assert warn_notify.send_email(
            _NEW_DIFF, {"total_records": 1}, state="CA", records=_TWO_SUBS
        ) is False


def test_body_builders_render_the_footer_when_given_a_url():
    url = "https://example.test/unsubscribe.html?e=a%40x.com&s=abc"
    text = warn_notify._build_text(_NEW_DIFF, {"total_records": 1}, "CA", url)
    html = warn_notify._build_html(_NEW_DIFF, {"total_records": 1}, "CA", url)
    assert f"Manage or cancel these alerts: {url}" in text
    assert _href(url) in html
    assert "reply to this email" not in text
    assert "reply to this email" not in html


def test_body_builders_keep_the_reply_line_without_a_url():
    text = warn_notify._build_text(_NEW_DIFF, {"total_records": 1})
    html = warn_notify._build_html(_NEW_DIFF, {"total_records": 1})
    assert "reply to this email" in text
    assert "reply to this email" in html


def test_html_footer_lands_inside_the_document_body():
    """Appended to an opaque digest, the block goes before </body>."""
    out = warn_notify._append_unsubscribe_html(
        "<html><body><p>hi</p></body></html>", "https://example.test/u"
    )
    assert out.endswith("</body></html>")
    assert out.index("Manage or cancel") < out.index("</body>")


def test_footer_appenders_are_no_ops_without_a_url():
    assert warn_notify._append_unsubscribe_html("<p>hi</p>", "") == "<p>hi</p>"
    assert warn_notify._append_unsubscribe_text("hi", "") == "hi"
