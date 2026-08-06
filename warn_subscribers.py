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

import hashlib
import hmac
import os
import logging
import re
from pathlib import Path
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests

import warn_urls

log = logging.getLogger("warn_subscribers")


def _endpoint() -> str:
    return (
        os.environ.get("SUBSCRIBERS_ENDPOINT")
        or os.environ.get("SIGNUP_ENDPOINT")
        or ""
    ).strip()


def _token() -> str:
    return os.environ.get("SUBSCRIBERS_TOKEN", "").strip()


# Sentinel stored in the sheet's `states` column for the whole-US monthly
# digest. It is not a state code, so it never routes per-notice alerts.
DIGEST_CODE = "US"

# Subscribers who signed up before state preferences existed get California,
# the jurisdiction the platform originally covered.
DEFAULT_STATES = ("CA",)


def _parse_prefs(raw) -> tuple:
    """Split a sheet `states` cell into (state_codes, wants_digest).

    Accepts comma/space/semicolon separated codes, case-insensitive. A blank
    cell means the subscriber predates state preferences → DEFAULT_STATES.
    """
    text = str(raw or "").strip()
    if not text:
        return list(DEFAULT_STATES), False
    tokens = [t.strip().upper() for t in re.split(r"[,;\s]+", text) if t.strip()]
    digest = DIGEST_CODE in tokens
    states = [t for t in tokens if t != DIGEST_CODE and len(t) == 2]
    # Deduplicate, preserving order.
    seen, ordered = set(), []
    for st in states:
        if st not in seen:
            seen.add(st)
            ordered.append(st)
    return ordered, digest


def get_subscriber_records(timeout: int = 20) -> list:
    """Return subscriber dicts: {email, name, states, digest}.

    ``states`` is the list of 2-letter codes that subscriber wants per-notice
    alerts for; ``digest`` is True when they opted into the whole-US monthly
    summary. Returns [] when unconfigured or on any error (never raises).
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

    seen, records = set(), []
    for s in payload.get("subscribers", []):
        s = s or {}
        em = str(s.get("email", "")).strip().lower()
        if not em or "@" not in em or em in seen:
            continue
        seen.add(em)
        states, digest = _parse_prefs(s.get("states"))
        records.append(
            {
                "email": em,
                "name": str(s.get("name", "")).strip(),
                "states": states,
                "digest": digest,
            }
        )

    log.info(f"Loaded {len(records)} subscriber(s).")
    return records


def get_subscribers(timeout: int = 20) -> list:
    """Return a de-duplicated list of subscriber email addresses (lowercased).

    Kept for callers that do not care about per-state routing.
    """
    return [r["email"] for r in get_subscriber_records(timeout=timeout)]


def subscribers_for_state(code: str, timeout: int = 20, records=None) -> list:
    """Emails of subscribers who asked for alerts about one state."""
    code = str(code or "").strip().upper()
    if records is None:
        records = get_subscriber_records(timeout=timeout)
    return [r["email"] for r in records if code in r["states"]]


def digest_subscribers(timeout: int = 20, records=None) -> list:
    """Emails of subscribers who opted into the whole-US monthly digest."""
    if records is None:
        records = get_subscriber_records(timeout=timeout)
    return [r["email"] for r in records if r["digest"]]


# ---------------------------------------------------------------------------
# Signed unsubscribe links
# ---------------------------------------------------------------------------

# Public site root; the unsubscribe page is published alongside the dashboards.
# Both come from warn_urls so the layout has one source of truth — and both are
# frozen: live links in already-sent mail resolve against exactly these values.
SITE_BASE_URL = warn_urls.SITE_BASE_URL
UNSUBSCRIBE_PAGE = warn_urls.UNSUBSCRIBE_PATH.lstrip("/")


def unsubscribe_signature(email: str) -> str:
    """HMAC-SHA256 of the lowercased email, keyed by the shared list token.

    The Apps Script recomputes this with the same key (its LIST_TOKEN script
    property) before showing or changing anyone's preferences, so a link only
    ever works for the address it was minted for — nobody can unsubscribe
    somebody else by editing the query string. Returns "" when the token is
    unset, which callers treat as "no unsubscribe link available".
    """
    secret = _token()
    email = str(email or "").strip().lower()
    if not secret or not email:
        return ""
    return hmac.new(
        secret.encode("utf-8"), email.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def unsubscribe_url(email: str, base: str = SITE_BASE_URL) -> str:
    """Signed one-click unsubscribe URL for one subscriber ("" if unsigned)."""
    sig = unsubscribe_signature(email)
    if not sig:
        return ""
    query = urlencode({"e": str(email).strip().lower(), "s": sig})
    return f"{base.rstrip('/')}/{UNSUBSCRIBE_PAGE}?{query}"


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
    subs = get_subscriber_records()
    print(f"{len(subs)} subscriber(s):")
    for s in subs:
        scope = ",".join(s["states"]) or "—"
        digest = " +US-digest" if s["digest"] else ""
        print(f"  {s['email']}  [{scope}{digest}]")
