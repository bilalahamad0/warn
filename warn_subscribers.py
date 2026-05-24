"""
warn_subscribers.py
--------------------
Fetch the email subscriber list collected by the dashboard signup form.

Signups are stored by a Google Apps Script Web App (see automation/subscribe.gs)
backed by a Google Sheet. This module reads that list so warn_notify.py can email
subscribers when new WARN notices appear.

Config (in .env or environment):
    SIGNUP_ENDPOINT=https://script.google.com/macros/s/XXXX/exec
    SUBSCRIBERS_TOKEN=the-same-value-as-the-LIST_TOKEN-script-property

SUBSCRIBERS_ENDPOINT may be set instead of SIGNUP_ENDPOINT; they are the same URL.
Returns an empty list when unconfigured or on any error (never raises), so the
pipeline degrades gracefully.
"""

import os
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests

log = logging.getLogger("warn_subscribers")


def _endpoint() -> str:
    return (
        os.environ.get("SUBSCRIBERS_ENDPOINT")
        or os.environ.get("SIGNUP_ENDPOINT")
        or ""
    ).strip()


def _token() -> str:
    return os.environ.get("SUBSCRIBERS_TOKEN", "").strip()


def get_subscribers(timeout: int = 20) -> list:
    """Return a de-duplicated list of subscriber email addresses (lowercased).

    Requires both the endpoint and the shared token. Returns [] if either is
    missing or if the request/parse fails for any reason.
    """
    endpoint, token = _endpoint(), _token()
    if not endpoint or not token:
        log.info(
            "SUBSCRIBERS_ENDPOINT / SUBSCRIBERS_TOKEN not set — no subscribers."
        )
        return []

    try:
        resp = requests.get(endpoint, params={"token": token}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log.warning(f"Failed to fetch subscribers: {e}")
        return []

    if not isinstance(payload, dict) or not payload.get("ok"):
        log.warning("Subscriber endpoint did not return ok — check token/endpoint.")
        return []

    seen, emails = set(), []
    for s in payload.get("subscribers", []):
        em = str((s or {}).get("email", "")).strip().lower()
        if em and "@" in em and em not in seen:
            seen.add(em)
            emails.append(em)

    log.info(f"Loaded {len(emails)} subscriber(s).")
    return emails


def get_subscriber_count(timeout: int = 15) -> int:
    """Public signup count (no token required). Returns 0 on error."""
    endpoint = _endpoint()
    if not endpoint:
        return 0
    try:
        resp = requests.get(endpoint, timeout=timeout)
        resp.raise_for_status()
        return int(resp.json().get("count", 0))
    except Exception as e:
        log.warning(f"Failed to fetch subscriber count: {e}")
        return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    subs = get_subscribers()
    print(f"{len(subs)} subscriber(s):")
    for e in subs:
        print(f"  {e}")
