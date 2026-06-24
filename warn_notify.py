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
    # Or call: notify_if_changes(diff_result) from warn_publish.py
"""

import smtplib
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timezone
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import warn_subscribers

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
DASHBOARD_URL = "https://bilalahamad0.github.io/warn/"

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


def _build_html(diff: dict, summary: dict) -> str:
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
              📋 California WARN Alert
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
            <a href="{DASHBOARD_URL}"
               style="display:inline-block;background:linear-gradient(135deg,#58a6ff,#388bfd);
                      color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;
                      font-weight:600;font-size:14px">
              View Full Dashboard →
            </a>
            &nbsp;
            <a href="{WARN_URL}"
               style="display:inline-block;background:none;border:1px solid #21262d;
                      color:#8b949e;text-decoration:none;padding:12px 28px;border-radius:8px;
                      font-weight:500;font-size:14px">
              Source: CA EDD
            </a>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:20px 32px;border-top:1px solid #21262d;font-size:12px;color:#8b949e">
            You're receiving this because you subscribed to California WARN alerts at
            <a href="{DASHBOARD_URL}" style="color:#58a6ff">
              {DASHBOARD_URL}
            </a>.
            Data source: California Employment Development Department.
            <br/>To unsubscribe, reply to this email with "unsubscribe".
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_text(diff: dict, summary: dict) -> str:
    new_count = diff.get("new_count", 0)
    amend_count = diff.get("amendment_count", 0)
    new_emp = diff.get("total_employees_new", 0)
    entries = diff.get("new_entries", [])[:10]
    lines = [
        "California WARN Alert",
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

    lines += [
        "",
        f"Dashboard: {DASHBOARD_URL}",
        f"Source: {WARN_URL}",
        "",
        'To unsubscribe, reply to this email with "unsubscribe".',
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


def send_email(diff: dict, summary: dict) -> bool:
    """
    Send a notification email if there are new entries.
    Goes to NOTIFY_EMAIL (To) plus every signup subscriber (BCC).
    Returns True if sent successfully.
    """
    gmail_user, gmail_pass, notify_email = _smtp_config()
    if not gmail_user or not gmail_pass:
        log.warning(
            "GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email. "
            "Add them to .env to enable notifications."
        )
        return False

    new_count = diff.get("new_count", 0)
    amend_count = diff.get("amendment_count", 0)
    if new_count == 0 and amend_count == 0:
        log.info("No new notices or amendments — skipping email notification.")
        return False

    if new_count > 0:
        subject = (
            f"🚨 WARN Alert: {new_count} new CA layoff notice{'s' if new_count > 1 else ''} "
            f"({diff.get('total_employees_new', 0):,} employees)"
        )
        if amend_count > 0:
            subject += f" + {amend_count} amended"
    else:
        subject = (
            f"📝 WARN Update: {amend_count} CA layoff "
            f"notice{'s' if amend_count > 1 else ''} amended"
        )

    # Recipients: owner in To; signup subscribers BCC'd (privacy + Gmail caps).
    to_addr = notify_email or gmail_user
    try:
        subscribers = warn_subscribers.get_subscribers()
    except Exception as e:
        log.warning(f"Could not load subscribers (sending to owner only): {e}")
        subscribers = []

    batches = _recipient_batches(to_addr, subscribers)
    if not batches:
        log.warning("No recipients (NOTIFY_EMAIL unset, no subscribers) — skipping.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"WARN Monitor <{gmail_user}>"
    msg["To"] = to_addr
    msg["List-Unsubscribe"] = f"<mailto:{gmail_user}?subject=unsubscribe>"

    msg.attach(MIMEText(_build_text(diff, summary), "plain"))
    msg.attach(MIMEText(_build_html(diff, summary), "html"))
    raw = msg.as_string()

    total = len({r for batch in batches for r in batch})
    try:
        log.info(
            f"Sending alert to {total} recipient(s) "
            f"({len(subscribers)} subscriber(s)) in {len(batches)} batch(es) …"
        )
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            for batch in batches:
                server.sendmail(gmail_user, batch, raw)
        log.info("✓ Alert email sent.")
        return True
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


def notify_if_changes(diff: dict, summary: dict) -> bool:
    """Convenience wrapper — call this from warn_publish.py."""
    return send_email(diff, summary)


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
