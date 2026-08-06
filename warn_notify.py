"""
warn_notify.py
--------------
Sends an HTML email alert when new WARN notices are detected.
Uses Gmail SMTP with an App Password (required since Google deprecated
password auth).

Config (in .env):
    GMAIL_USER=your_email@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (16-char Google App Password)
    NOTIFY_EMAIL=recipient@example.com       (destination)

Usage:
    python3 warn_notify.py --test     # send a test email
    # Or call: notify_if_changes(diff_result, summary, state="IL")

Routing: a per-notice alert for state X reaches NOTIFY_EMAIL (the operator,
always) plus only those subscribers whose preferences include X. The whole-US
monthly digest reaches the operator plus subscribers who opted into it. See
warn_subscribers for the preference schema.

Unsubscribe links are per recipient — the URL carries an HMAC of that one
address (warn_subscribers.unsubscribe_url), so a single BCC blast cannot carry
a correct one. Subscriber mail is therefore personalised: one message each,
all sent over ONE SMTP login. When SUBSCRIBERS_TOKEN is unset there is no
signature to mint, so delivery falls back to the original BCC batching rather
than shipping a link that would fail verification.
"""

import smtplib
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _html_escape
from pathlib import Path
from datetime import datetime, timezone
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import warn_subscribers
import warn_urls

log = logging.getLogger("warn_notify")

# Gmail caps recipients per message (~100 free / ~500 Workspace). Stay under it.
MAX_BCC_PER_MESSAGE = 90


def _smtp_config() -> tuple:
    """Resolve (gmail_user, gmail_app_password, notify_email) from the environment.

    Read at call time rather than captured into module globals at import, so
    values from .env (load_dotenv runs at import), CI-exported vars, and test
    monkeypatching are always honoured — not frozen to whatever happened to be
    set the moment this module was first imported.
    """
    return (
        os.environ.get("GMAIL_USER", ""),
        os.environ.get("GMAIL_APP_PASSWORD", ""),
        os.environ.get("NOTIFY_EMAIL", ""),
    )


WARN_URL = "https://edd.ca.gov/en/jobs_and_training/layoff_services_warn"

# Both dashboards come straight from warn_urls. US_DASHBOARD_URL used to be
# derived as ``DASHBOARD_URL + "us/"``, which was only correct while California
# sat at the site root — the moment the root became the national dashboard that
# derivation would have pointed every non-CA alert at a redirect stub without
# anything failing. Independent constants, one source of truth.
CA_DASHBOARD_URL = warn_urls.CA_DASHBOARD_URL
US_DASHBOARD_URL = warn_urls.US_DASHBOARD_URL

# Deprecated alias — the old name for the California dashboard, kept one
# release so any out-of-tree caller does not break silently. Prefer
# CA_DASHBOARD_URL.
DASHBOARD_URL = CA_DASHBOARD_URL

# California is the platform's original (grandfathered) jurisdiction, so its
# labels stay here rather than coming from the registry: WARN_URL is the
# human-readable EDD page, whereas warn_sources.ca.source_url is the raw XLSX.
_CA_META = {
    "name": "California",
    "agency": "California Employment Development Department",
    "url": WARN_URL,
    "dashboard": CA_DASHBOARD_URL,
}


def _state_meta(state: str) -> dict:
    """Display name / agency / source URL / dashboard for a 2-letter code.

    Resolved from the warn_sources registry so state labels have one source of
    truth. warn_sources is imported lazily — this module stays usable (and
    `--test` stays fast) without pulling in the whole scraper stack. Falls back
    to the bare code when the registry is unavailable or the state is unknown.
    """
    code = str(state or "CA").strip().lower()
    if code == "ca":
        return dict(_CA_META)
    fallback = {
        "name": code.upper(),
        "agency": "",
        "url": "",
        "dashboard": US_DASHBOARD_URL,
    }
    try:
        import warn_sources

        cls = warn_sources.SOURCES[code]
    except Exception:  # noqa: BLE001 — labels must never break a send
        return fallback
    return {
        "name": getattr(cls, "name", "") or code.upper(),
        "agency": getattr(cls, "agency", "") or "",
        "url": getattr(cls, "source_url", "") or "",
        "dashboard": US_DASHBOARD_URL,
    }


# ---------------------------------------------------------------------------
# Unsubscribe footer
# ---------------------------------------------------------------------------

# One wording, used by the alert template, the digest footer and the plain-text
# parts, so the visible line always matches the List-Unsubscribe header.
UNSUBSCRIBE_LABEL = "Manage or cancel these alerts"

# Shown when no signed link can be minted (SUBSCRIBERS_TOKEN unset) — exactly
# what every message said before signed links existed.
REPLY_UNSUBSCRIBE_LINE = 'To unsubscribe, reply to this email with "unsubscribe".'


def _unsubscribe_link(email: str) -> str:
    """Signed one-click unsubscribe URL for one address ("" when unavailable).

    Returns "" when SUBSCRIBERS_TOKEN is unset (nothing to sign with) and on any
    error — a missing link is a cosmetic loss, a failed alert is not.
    """
    try:
        return warn_subscribers.unsubscribe_url(email) or ""
    except Exception as e:  # noqa: BLE001 — never block a send on link minting
        log.warning(f"Could not build unsubscribe link: {e}")
        return ""


def _unsubscribe_line_text(url: str) -> str:
    return f"{UNSUBSCRIBE_LABEL}: {url}"


def _unsubscribe_line_html(url: str) -> str:
    """The footer line as markup. The URL is HTML-escaped, not mangled.

    The signed URL joins its two query parameters with ``&``, which is invalid
    raw in markup — in an attribute *or* in text. Mail clients run aggressive
    sanitisers over the body, so escape it and let the client unescape back to
    the exact URL the List-Unsubscribe header carries.
    """
    safe = _html_escape(url, quote=True)
    return f'{UNSUBSCRIBE_LABEL}: <a href="{safe}" style="color:#58a6ff">{safe}</a>'


def _append_unsubscribe_text(text: str, url: str) -> str:
    """Append the footer line to an opaque plain-text body (the digest)."""
    if not url:
        return text
    return f"{text.rstrip()}\n\n{_unsubscribe_line_text(url)}"


def _append_unsubscribe_html(html: str, url: str) -> str:
    """Append the footer block to an opaque HTML body (the digest).

    Inserted just before </body> when there is one so it lands inside the
    rendered document, appended otherwise.
    """
    if not url:
        return html
    block = (
        '<div style="margin:0 auto;padding:20px;max-width:620px;'
        "text-align:center;font-family:Inter,system-ui,sans-serif;"
        'font-size:12px;color:#8b949e">'
        f"{_unsubscribe_line_html(url)}</div>"
    )
    idx = html.lower().rfind("</body>")
    if idx == -1:
        return html + block
    return html[:idx] + block + html[idx:]


# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------


def _fmt_emp(v) -> str:
    """Format an employee count for display, tolerating None/non-int."""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "?"


def _describe_amendment(a: dict) -> str:
    """Human description of what EDD changed in an amended notice."""
    parts = []
    old_eff, new_eff = a.get("old_effective_date"), a.get("new_effective_date")
    if old_eff != new_eff:
        parts.append(f"effective date {old_eff or '?'} → {new_eff or '?'}")
    old_emp, new_emp = a.get("old_employees"), a.get("new_employees")
    if old_emp != new_emp:
        parts.append(f"headcount {_fmt_emp(old_emp)} → {_fmt_emp(new_emp)}")
    return "; ".join(parts) or "details revised"


def _build_html(
    diff: dict, summary: dict, state: str = "CA", unsubscribe_url: str = ""
) -> str:
    meta = _state_meta(state)
    state_name = meta["name"]
    dashboard_url = meta["dashboard"]
    source_label = meta["agency"] or state_name
    # An unregistered code has no known feed URL — link to nothing rather than
    # to some other state's agency.
    source_btn = ""
    if meta["url"]:
        source_btn = (
            f'&nbsp;<a href="{meta["url"]}" style="display:inline-block;'
            "background:none;border:1px solid #21262d;color:#8b949e;"
            "text-decoration:none;padding:12px 28px;border-radius:8px;"
            'font-weight:500;font-size:14px">'
            f"Source: {state_name}</a>"
        )
    new_count = diff.get("new_count", 0)
    amend_count = diff.get("amendment_count", 0)
    rem_count = diff.get("removed_count", 0)
    new_emp = diff.get("total_employees_new", 0)
    new_entries = diff.get("new_entries", [])[:10]  # top 10 in email
    amendments = diff.get("amendments", [])
    total_rec = summary.get("total_records", 0)
    total_emp = summary.get("total_employees", 0)
    now = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    # Left stat card adapts: headline new filings, or amendments when an update
    # carries only revisions.
    if new_count > 0:
        stat_label, stat_value = "New Notices", f"+{new_count:,}"
        stat_sub, stat_color = f"{new_emp:,} employees affected", "#3fb950"
    else:
        stat_label, stat_value = "Amended Notices", f"{amend_count:,}"
        stat_sub, stat_color = "details revised", "#d29922"

    rows_html = ""
    for r in new_entries:
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d">{r.get('company','?')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d;text-align:right">{r.get('employees',0):,}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d">{r.get('effective_date','?')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d">{r.get('county','?')}</td>
        </tr>"""

    more_note = ""
    if new_count > 10:
        more_note = f'<p style="color:#8b949e;font-size:13px">… and {new_count - 10} more. View all on the dashboard.</p>'

    # Amended notices — a known filing whose details EDD revised (most often its
    # effective date). Each is reported at most once (see warn_monitor), so this
    # no longer re-surfaces the same amendment on every feed swing.
    amend_html = ""
    if amendments:
        amend_rows = ""
        for a in amendments[:10]:
            amend_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d">{a.get('company','?')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d">{a.get('county','?')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #21262d;color:#d29922">{_describe_amendment(a)}</td>
        </tr>"""
        amend_more = ""
        if len(amendments) > 10:
            amend_more = f'<p style="color:#8b949e;font-size:13px">… and {len(amendments) - 10} more amended.</p>'
        amend_html = (
            '<tr><td style="padding:0 32px 24px">'
            '<h2 style="font-size:15px;margin:0 0 12px;color:#e6edf3">📝 Amended Notices</h2>'
            '<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #21262d;border-radius:8px;overflow:hidden">'
            '<thead><tr style="background:#0d1117">'
            '<th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">Company</th>'
            '<th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">County</th>'
            '<th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">What changed</th>'
            '</tr></thead><tbody>' + amend_rows + '</tbody></table>' + amend_more +
            '</td></tr>'
        )

    # Footer: the recipient's own signed link when we have one, else the
    # pre-link reply instruction.
    unsub_line = (
        _unsubscribe_line_html(unsubscribe_url)
        if unsubscribe_url
        else REPLY_UNSUBSCRIBE_LINE
    )

    # Genuine withdrawals only — a filing whose whole anchor vanished from the
    # feed, not a revision (those are shown above as amendments).
    removed_note = ""
    if rem_count > 0:
        plural = rem_count != 1
        removed_note = (
            f'<p style="color:#f78166">⚠️ {rem_count} previously filed '
            f'notice{"s" if plural else ""} {"were" if plural else "was"} '
            f"withdrawn from the official file in this update.</p>"
        )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>WARN Alert</title></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:Inter,system-ui,sans-serif;color:#e6edf3">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 20px">
      <table width="620" cellpadding="0" cellspacing="0"
             style="background:#161b22;border-radius:16px;border:1px solid #21262d;overflow:hidden">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#58a6ff,#f78166);padding:28px 32px">
            <h1 style="margin:0;font-size:22px;color:#fff;font-weight:700">
              📋 {state_name} WARN Alert
            </h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:14px">{now}</p>
          </td>
        </tr>

        <!-- Stats -->
        <tr>
          <td style="padding:28px 32px">
            <table width="100%" cellpadding="0" cellspacing="12">
              <tr>
                <td width="50%" style="background:#0d1117;border-radius:10px;padding:16px;border:1px solid #21262d">
                  <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">{stat_label}</div>
                  <div style="font-size:32px;font-weight:700;color:{stat_color}">{stat_value}</div>
                  <div style="font-size:12px;color:#8b949e">{stat_sub}</div>
                </td>
                <td width="50%" style="background:#0d1117;border-radius:10px;padding:16px;border:1px solid #21262d">
                  <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Total on File</div>
                  <div style="font-size:32px;font-weight:700;color:#58a6ff">{total_rec:,}</div>
                  <div style="font-size:12px;color:#8b949e">{total_emp:,} total employees</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- New entries table -->
        {'<tr><td style="padding:0 32px 24px"><h2 style="font-size:15px;margin:0 0 12px;color:#e6edf3">🆕 Newly Filed Notices</h2>' if new_entries else ''}
        {'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #21262d;border-radius:8px;overflow:hidden"><thead><tr style="background:#0d1117"><th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">Company</th><th style="padding:10px 12px;text-align:right;font-size:12px;color:#8b949e">Employees</th><th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">Effective Date</th><th style="padding:10px 12px;text-align:left;font-size:12px;color:#8b949e">County</th></tr></thead><tbody>' + rows_html + '</tbody></table>' if new_entries else ''}
        {more_note}
        {'</td></tr>' if new_entries else ''}

        {amend_html}

        {f'<tr><td style="padding:0 32px 24px">{removed_note}</td></tr>' if removed_note else ''}

        <!-- CTA -->
        <tr>
          <td style="padding:0 32px 32px">
            <a href="{dashboard_url}"
               style="display:inline-block;background:linear-gradient(135deg,#58a6ff,#388bfd);
                      color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;
                      font-weight:600;font-size:14px">
              View Full Dashboard →
            </a>{source_btn}
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:20px 32px;border-top:1px solid #21262d;font-size:12px;color:#8b949e">
            You're receiving this because you subscribed to {state_name} WARN alerts at
            <a href="{dashboard_url}" style="color:#58a6ff">
              {dashboard_url}
            </a>.
            Data source: {source_label}.
            <br/>{unsub_line}
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_text(
    diff: dict, summary: dict, state: str = "CA", unsubscribe_url: str = ""
) -> str:
    meta = _state_meta(state)
    new_count = diff.get("new_count", 0)
    amend_count = diff.get("amendment_count", 0)
    new_emp = diff.get("total_employees_new", 0)
    entries = diff.get("new_entries", [])[:10]
    lines = [
        f"{meta['name']} WARN Alert",
        "=" * 40,
        f"New notices: {new_count:,} ({new_emp:,} employees)",
        f"Amended notices: {amend_count:,}",
        f"Total on file: {summary.get('total_records', 0):,}",
        "",
    ]
    if entries:
        lines.append("New entries (top 10):")
        for r in entries:
            lines.append(
                f"  {r.get('company', '?')} — {r.get('employees', 0):,} employees — "
                f"{r.get('effective_date', '?')} — {r.get('county', '?')}"
            )

    amendments = diff.get("amendments", [])
    if amendments:
        lines.append("")
        lines.append("Amended notices:")
        for a in amendments[:10]:
            lines.append(
                f"  {a.get('company', '?')} ({a.get('county', '?')}) — "
                f"{_describe_amendment(a)}"
            )

    lines += ["", f"Dashboard: {meta['dashboard']}"]
    if meta["url"]:
        lines.append(f"Source: {meta['url']}")
    lines += [
        "",
        (
            _unsubscribe_line_text(unsubscribe_url)
            if unsubscribe_url
            else REPLY_UNSUBSCRIBE_LINE
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def _recipient_batches(to_addr: str, subscribers: List[str]) -> List[List[str]]:
    """Split recipients into envelope batches under Gmail's per-message cap.

    The owner (to_addr) is the visible To and is delivered once, in the first
    batch. Subscribers are added as additional (BCC) envelope recipients, chunked
    so no single message exceeds MAX_BCC_PER_MESSAGE recipients.
    """
    subs = [s for s in dict.fromkeys(subscribers) if s and s != to_addr]
    if not subs:
        return [[to_addr]] if to_addr else []
    batches = []
    for i in range(0, len(subs), MAX_BCC_PER_MESSAGE):
        chunk = subs[i:i + MAX_BCC_PER_MESSAGE]
        recips = ([to_addr] + chunk) if (i == 0 and to_addr) else list(chunk)
        batches.append(recips)
    return batches


def _build_message(
    subject: str,
    to_addr: str,
    text: str,
    html: str,
    unsubscribe_url: str = "",
) -> MIMEMultipart:
    """Assemble one message, wired to ``unsubscribe_url`` when there is one.

    ``List-Unsubscribe`` points at the same per-recipient URL the visible
    footer shows, so mail clients can offer their own unsubscribe affordance
    that opens the confirmation page. Without a signed URL the header falls
    back to the mailto form used before signed links.

    Deliberately NO ``List-Unsubscribe-Post``: RFC 8058 one-click promises the
    URI accepts an HTTPS POST *and* completes the unsubscribe with no further
    interaction. The landing page is a static GitHub Pages asset that cannot
    process POST, and by design nothing changes until the visitor confirms
    their selection — so advertising one-click would send Gmail's native
    button to a dead POST and tell the user they were unsubscribed when they
    were not. (To offer true one-click later, point this header at the Apps
    Script /exec endpoint, which can accept POST, and add a handler there.)
    """
    gmail_user, _, _ = _smtp_config()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"WARN Monitor <{gmail_user}>"
    msg["To"] = to_addr
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    else:
        msg["List-Unsubscribe"] = f"<mailto:{gmail_user}?subject=unsubscribe>"

    msg.attach(MIMEText(text, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))
    return msg


def _plan_deliveries(
    subject: str,
    to_addr: str,
    subscribers: List[str],
    build_text,
    build_html,
) -> List[tuple]:
    """Plan one send as a list of ``(envelope_recipients, message)`` pairs.

    ``build_text``/``build_html`` take the recipient's unsubscribe URL ("" for
    none) and return that recipient's body, so each subscriber's message can
    carry their own signed link.

    Signed: the operator gets their copy unchanged, then one personalised
    message per subscriber (their address in To — no BCC, nobody's link
    reaching anybody else). Unsigned (SUBSCRIBERS_TOKEN unset): a single
    message BCC-batched under Gmail's per-message cap, exactly as before.
    """
    subs = [s for s in dict.fromkeys(subscribers) if s and s != to_addr]
    links = {em: _unsubscribe_link(em) for em in subs}

    if not any(links.values()):
        if subs:
            log.info(
                "SUBSCRIBERS_TOKEN not set — no per-recipient unsubscribe link; "
                "sending one BCC-batched message as before."
            )
        msg = _build_message(subject, to_addr, build_text(""), build_html(""))
        return [(batch, msg) for batch in _recipient_batches(to_addr, subs)]

    deliveries = []
    if to_addr:
        deliveries.append(
            (
                [to_addr],
                _build_message(subject, to_addr, build_text(""), build_html("")),
            )
        )
    for em in subs:
        url = links.get(em, "")
        deliveries.append(
            (
                [em],
                _build_message(
                    subject, em, build_text(url), build_html(url), url
                ),
            )
        )
    return deliveries


def _deliver(deliveries: List[tuple], what: str) -> bool:
    """Log in once and send every planned message. True if any was delivered.

    Personalised sends mean one message per subscriber, so a fresh connection
    per recipient would turn a 500-subscriber alert into 500 TLS handshakes and
    500 Gmail logins — one login, N sendmail commands instead.

    An address Gmail refuses outright is logged and skipped rather than failing
    the whole send: a permanently dead address would otherwise leave the
    alerted-keys ledger unwritten and re-mail everyone else on the next run.
    """
    gmail_user, gmail_pass, _ = _smtp_config()
    if not deliveries:
        return False
    total = len({r for recips, _ in deliveries for r in recips})
    delivered = 0
    try:
        log.info(
            f"Sending {what} to {total} recipient(s) in "
            f"{len(deliveries)} message(s) …"
        )
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            for recips, msg in deliveries:
                try:
                    server.sendmail(gmail_user, recips, msg.as_string())
                    delivered += 1
                except (
                    smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPSenderRefused,
                    smtplib.SMTPDataError,
                ) as e:
                    log.warning(f"Recipient refused ({', '.join(recips)}): {e}")
    except smtplib.SMTPAuthenticationError:
        log.error(
            "Gmail authentication failed. Make sure you're using an App Password, "
            "not your regular Gmail password. See: "
            "https://myaccount.google.com/apppasswords"
        )
        return False
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False

    if not delivered:
        log.error(f"{what.capitalize()} reached nobody — every address refused.")
        return False
    log.info(f"✓ {what.capitalize()} sent ({delivered}/{len(deliveries)} msgs).")
    return True


def load_subscriber_records() -> list:
    """Fetch the subscriber list once, returning [] on any failure.

    Callers (warn_publish) fetch once per pipeline run and thread the result
    through every send as ``records=``, so a run that alerts on N states hits
    the signup sheet once rather than N times.
    """
    try:
        return warn_subscribers.get_subscriber_records()
    except Exception as e:  # noqa: BLE001 — never block a send on the sheet
        log.warning(f"Could not load subscribers (operator-only sends): {e}")
        return []


def _subscribers_for_state(code: str, records=None) -> List[str]:
    try:
        return warn_subscribers.subscribers_for_state(code, records=records)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not load {code} subscribers (operator only): {e}")
        return []


def send_email(
    diff: dict, summary: dict, state: str = "CA", records=None
) -> bool:
    """Send a per-notice alert for one state's WARN feed.

    Goes to NOTIFY_EMAIL (the operator, in To) plus only those subscribers who
    asked for ``state`` — each as their own message carrying their own signed
    unsubscribe link, all over one SMTP login (see ``_plan_deliveries``). A
    state nobody subscribed to still reaches the operator, so pipeline activity
    is never invisible.

    ``records`` is an already-fetched subscriber list (see
    ``load_subscriber_records``); when None the list is fetched here.
    Returns True if sent successfully.
    """
    gmail_user, gmail_pass, notify_email = _smtp_config()
    if not gmail_user or not gmail_pass:
        log.warning(
            "GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email. "
            "Add them to .env to enable notifications."
        )
        return False

    code = str(state or "CA").strip().upper()
    new_count = diff.get("new_count", 0)
    amend_count = diff.get("amendment_count", 0)
    if new_count == 0 and amend_count == 0:
        log.info(f"No new {code} notices or amendments — skipping email.")
        return False

    state_name = _state_meta(code)["name"]
    if new_count > 0:
        plural = "s" if new_count > 1 else ""
        subject = (
            f"🚨 WARN Alert: {new_count} new {state_name} layoff notice{plural} "
            f"({diff.get('total_employees_new', 0):,} employees)"
        )
        if amend_count > 0:
            subject += f" + {amend_count} amended"
    else:
        subject = (
            f"📝 WARN Update: {amend_count} {state_name} layoff "
            f"notice{'s' if amend_count > 1 else ''} amended"
        )

    to_addr = notify_email or gmail_user
    subscribers = _subscribers_for_state(code, records)
    if not subscribers:
        log.info(f"No subscribers requested {code} alerts — operator only.")

    deliveries = _plan_deliveries(
        subject,
        to_addr,
        subscribers,
        lambda url: _build_text(diff, summary, code, url),
        lambda url: _build_html(diff, summary, code, url),
    )
    if not deliveries:
        log.warning(
            f"No recipients for {code} (NOTIFY_EMAIL unset, no subscribers) "
            "— skipping."
        )
        return False

    return _deliver(
        deliveries, f"{code} alert ({len(subscribers)} subscriber(s))"
    )


def send_monthly_digest(digest: dict, records=None) -> bool:
    """Email the whole-US monthly digest built by warn_digest.

    Recipients are NOTIFY_EMAIL (To) plus every subscriber who opted into the
    digest — personalised one per subscriber, with the same signed unsubscribe
    footer and headers as the per-notice alerts. ``digest`` carries
    ``subject``/``html``/``text``; the plain-text part is attached first so
    clients that prefer it get a readable body.
    """
    gmail_user, gmail_pass, notify_email = _smtp_config()
    if not gmail_user or not gmail_pass:
        log.warning("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping digest.")
        return False

    digest = digest or {}
    html = digest.get("html") or ""
    text = digest.get("text") or ""
    if not html and not text:
        log.warning("Digest payload has no html/text body — nothing to send.")
        return False

    subject = digest.get("subject") or "📊 Monthly US WARN digest"
    to_addr = notify_email or gmail_user
    try:
        subscribers = warn_subscribers.digest_subscribers(records=records)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not load digest subscribers (operator only): {e}")
        subscribers = []

    body_text = text or f"View the full digest at {US_DASHBOARD_URL}"
    deliveries = _plan_deliveries(
        subject,
        to_addr,
        subscribers,
        lambda url: _append_unsubscribe_text(body_text, url),
        lambda url: _append_unsubscribe_html(html, url) if html else "",
    )
    if not deliveries:
        log.warning("No digest recipients (NOTIFY_EMAIL unset) — skipping.")
        return False

    return _deliver(
        deliveries, f"monthly digest ({len(subscribers)} subscriber(s))"
    )


def notify_if_changes(
    diff: dict, summary: dict, state: str = "CA", records=None
) -> bool:
    """Convenience wrapper — call this from warn_publish.py."""
    return send_email(diff, summary, state=state, records=records)


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    parser = argparse.ArgumentParser(description="WARN email notifier")
    parser.add_argument(
        "--test", action="store_true", help="Send a sample alert email and exit"
    )
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        sys.exit(0)

    test_diff = {
        "new_count": 3,
        "removed_count": 0,
        "total_employees_new": 450,
        "new_entries": [
            {
                "company": "Acme Corp",
                "employees": 200,
                "effective_date": "2026-05-01",
                "county": "Santa Clara County",
            },
            {
                "company": "Globex Inc",
                "employees": 150,
                "effective_date": "2026-05-15",
                "county": "Los Angeles County",
            },
            {
                "company": "Initech Solutions",
                "employees": 100,
                "effective_date": "2026-06-01",
                "county": "San Francisco County",
            },
        ],
    }
    test_summary = {"total_records": 1102, "total_employees": 61964}

    success = send_email(test_diff, test_summary)
    if success:
        print("✓ Test email sent.")
        sys.exit(0)
    print("✗ Test email failed — check GMAIL_USER / GMAIL_APP_PASSWORD.")
    sys.exit(1)
