"""
warn_urls.py
------------
The published site's URL layout, in one place.

Every other module derives its links from here rather than hardcoding a path,
because the layout has moved once already and the failure mode is silent: an
email goes out with a link to a page that no longer means what the copy around
it says.

Layout (GitHub Pages, served from ``docs/``):

    /warn/                  US national dashboard   (warn_site_us.py)
    /warn/data.json         national JSON API
    /warn/ca/               California dashboard    (warn_publish.py)
    /warn/ca/data.json      California JSON API
    /warn/us/               redirect stub → /warn/  (legacy; pre-2026-08 layout)
    /warn/unsubscribe.html  signed unsubscribe landing page
    /warn/architecture.html hand-maintained system-design page

``UNSUBSCRIBE_PATH`` is frozen. Every subscriber email ever sent carries an
HMAC-signed link to it, those links live in people's inboxes indefinitely, and
the Apps Script recomputes the signature against the same address — so the page
can never move without stranding them. ``tests/test_notify.py`` pins the exact
URL and signature as the guard.

This module imports nothing else from the project. Keep it that way: it sits
underneath warn_subscribers / warn_notify / warn_digest / warn_publish, all of
which import it, so any project-level import here risks a cycle.
"""

# Public site root, no trailing slash. Changing this changes every unsubscribe
# link ever minted from here on — see the note above.
SITE_BASE_URL = "https://bilalahamad0.github.io/warn"

# Per-view paths, relative to SITE_BASE_URL.
US_PATH = "/"
CA_PATH = "/ca/"
LEGACY_US_PATH = "/us/"
UNSUBSCRIBE_PATH = "/unsubscribe.html"
ARCHITECTURE_PATH = "/architecture.html"
OG_IMAGE_PATH = "/icon-512.png"


def url(path: str) -> str:
    """Absolute site URL for a layout path (``url("/ca/")`` → ``…/warn/ca/``)."""
    return f"{SITE_BASE_URL.rstrip('/')}/{str(path).lstrip('/')}"


US_DASHBOARD_URL = url(US_PATH)
CA_DASHBOARD_URL = url(CA_PATH)
LEGACY_US_DASHBOARD_URL = url(LEGACY_US_PATH)
UNSUBSCRIBE_URL = url(UNSUBSCRIBE_PATH)
ARCHITECTURE_URL = url(ARCHITECTURE_PATH)
OG_IMAGE_URL = url(OG_IMAGE_PATH)
