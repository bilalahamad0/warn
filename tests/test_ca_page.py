"""Tests for the California dashboard page (warn_publish.build_site).

Two things are pinned here that the page got wrong before:

* it lived at the site root and opened on a national choropleth, so the
  California page's first impression was indistinguishable from the US one;
* it counted fewer California notices than the US dashboard did.
"""

import json
import re

import pytest

import warn_charts
import warn_datasets
import warn_publish
import warn_site_us
import warn_urls


YEAR = 2026

MANIFEST = {"charts": [], "total_records": 0, "total_employees": 0,
            "last_updated": f"{YEAR}-08-01T00:00:00Z"}
MONITOR = {"diff": {"new_count": 0, "total_employees_new": 0, "new_keys": []}}


@pytest.fixture
def national_file(tmp_path):
    payload = {
        "last_updated": f"{YEAR}-08-01T00:00:00Z",
        "states_live": 2,
        "total_records": 4,
        "total_employees": 1200,
        "states": {
            "CA": {"name": "California"},
            "NJ": {"name": "New Jersey"},
        },
        "records": [
            {"state": "CA", "company": "Acme", "notice_date": f"{YEAR}-03-01",
             "effective_date": f"{YEAR}-05-01", "employees": 500,
             "layoff_type": "Layoff Permanent", "county": "Los Angeles County",
             "city": "LA", "address": "1 Main St", "industry": "Retail"},
            {"state": "CA", "company": "Beta", "notice_date": "2025-02-01",
             "effective_date": "2025-04-01", "employees": 300,
             "layoff_type": "Closure", "county": "Alameda County",
             "city": "Oakland", "address": "2 Oak Ave", "industry": "Tech"},
            # Pre-boundary: on the US dashboard, off the California page.
            {"state": "CA", "company": "Ancient", "notice_date": "2020-04-15",
             "effective_date": "2020-03-19", "employees": 251,
             "layoff_type": "Layoff Temporary", "county": "Los Angeles County"},
            {"state": "NJ", "company": "Gamma", "notice_date": f"{YEAR}-04-01",
             "effective_date": f"{YEAR}-06-01", "employees": 149},
        ],
    }
    path = tmp_path / "warn_national.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def ca_page(tmp_path, national_file, monkeypatch):
    """Build the California page into tmp_path/ca and return (path, html)."""
    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", national_file)
    monkeypatch.setattr(warn_publish, "CHARTS_DIR", tmp_path / "charts")
    warn_datasets.reset_cache()
    out = tmp_path / "ca"
    warn_publish.build_site(MANIFEST, MONITOR, out_dir=out)
    return out, (out / "index.html").read_text()


# ---------------------------------------------------------------------------
# Where the page lives, and what its links resolve to from there
# ---------------------------------------------------------------------------


def test_default_output_is_the_ca_subdirectory():
    assert warn_publish.CA_DIR == warn_publish.OUTPUT_DIR / "ca"
    assert warn_publish.INDEX_HTML == warn_publish.CA_DIR / "index.html"
    assert warn_publish.SITE_DATA == warn_publish.CA_DIR / "data.json"
    # The unsubscribe page is frozen at the site root: every already-mailed
    # signed link resolves there.
    assert warn_publish.UNSUBSCRIBE_HTML == warn_publish.OUTPUT_DIR / "unsubscribe.html"


def test_shared_assets_are_referenced_one_level_up(ca_page):
    """Icons live at the site root and are shared with the US dashboard."""
    _out, html = ca_page
    for asset in ("apple-touch-icon.png", "favicon-32x32.png",
                  "favicon-16x16.png", "favicon.ico", "icon.svg"):
        assert f'href="../{asset}"' in html
    assert '<a href="../architecture.html">' in html
    assert '<a href="../">' in html          # up-link to the US dashboard
    assert 'href="us/"' not in html          # the old sibling link is gone


def test_manifest_is_local_not_shared(ca_page):
    """start_url and scope resolve against the MANIFEST's URL, not the page's —
    a shared root manifest would make an installed California PWA open the US
    dashboard. This is the one asset that must not use ../."""
    _out, html = ca_page
    assert '<link rel="manifest" href="site.webmanifest" />' in html
    assert 'href="../site.webmanifest"' not in html


def test_every_relative_reference_resolves_from_the_ca_directory(ca_page):
    """Catch-all, so the next asset someone adds cannot silently 404.

    Anything not absolute, not a fragment and not inline data must either climb
    to the site root or name a file that actually sits in docs/ca/.
    """
    out, html = ca_page
    local = {"site.webmanifest", "index.html", "data.json"}
    refs = re.findall(r'(?:href|src)="([^"]+)"', html)
    for ref in refs:
        if ref.startswith(("http://", "https://", "//", "#", "data:", "mailto:")):
            continue
        assert ref.startswith("../") or ref.split("?")[0].split("#")[0] in local, (
            f"{ref!r} will 404 from /warn/ca/"
        )


def test_canonical_and_og_point_at_the_ca_subpath(ca_page):
    _out, html = ca_page
    assert f'<link rel="canonical" href="{warn_urls.CA_DASHBOARD_URL}" />' in html
    assert f'<meta property="og:url" content="{warn_urls.CA_DASHBOARD_URL}" />' in html
    assert f'<meta property="og:image" content="{warn_urls.OG_IMAGE_URL}" />' in html


# ---------------------------------------------------------------------------
# The page is about California
# ---------------------------------------------------------------------------


def test_page_carries_no_national_map(ca_page):
    """The national choropleth used to be the default-active tab of the first
    chart section, which is what made /warn/ look like /warn/us/."""
    _out, html = ca_page
    assert "12_us_map" not in html
    assert "pane-12_us_map" not in html


def test_impact_section_opens_on_a_california_chart(ca_page):
    _out, html = ca_page
    tabs = html.split('data-section="impact"', 1)[1]
    first_active = re.search(r'<button class="chart-tab active"[^>]*>([^<]+)<', tabs)
    assert first_active and first_active.group(1) == "Industry Breakdown"


def test_page_states_the_span_it_covers(ca_page):
    """The page shows a window of California filings, not all of them — so it
    says which, and points at the dashboard that has the rest."""
    _out, html = ca_page
    assert 'class="coverage-note"' in html
    assert "Feb 1, 2025" in html and "Mar 1, 2026" in html
    assert "2 California notices" in html
    assert 'class="coverage-note degraded"' not in html


def test_coverage_note_warns_when_the_derivation_fell_back(
    tmp_path, monkeypatch
):
    """A fallback to the raw EDD store under-reports early 2025. Say so rather
    than showing a confident total that disagrees with the US dashboard."""
    cumulative = tmp_path / "warn_cumulative.json"
    cumulative.write_text(json.dumps({"records": [
        {"company": "Live", "notice_date": "2025-06-01",
         "effective_date": "2025-08-01", "employees": 5},
    ]}))
    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(warn_datasets, "CUMULATIVE_FILE", cumulative)
    monkeypatch.setattr(warn_publish, "CHARTS_DIR", tmp_path / "charts")
    warn_datasets.reset_cache()

    out = tmp_path / "ca"
    warn_publish.build_site(MANIFEST, MONITOR, out_dir=out)
    html = (out / "index.html").read_text()

    assert 'class="coverage-note degraded"' in html
    assert "will read lower" in html


# ---------------------------------------------------------------------------
# The published API
# ---------------------------------------------------------------------------


def test_api_is_written_beside_the_page_and_declares_its_scope(ca_page):
    out, _html = ca_page
    api = json.loads((out / "data.json").read_text())
    assert api["scope"] == "ca"
    assert api["coverage_start"] == warn_datasets.CA_COVERAGE_START
    assert {r["company"] for r in api["records"]} == {"Acme", "Beta"}


# ---------------------------------------------------------------------------
# The two dashboards must never disagree about California again
# ---------------------------------------------------------------------------


def test_both_dashboards_report_the_same_california_totals(
    tmp_path, national_file, monkeypatch
):
    """Built from ONE national payload, the California page and the US
    dashboard must agree on California for every year the California page
    covers. This is the regression that started the whole restructure: 2025
    read 756 notices / 40,449 employees on one page and 827 / 45,924 on the
    other.
    """
    monkeypatch.setattr(warn_datasets, "NATIONAL_FILE", national_file)
    monkeypatch.setattr(warn_publish, "CHARTS_DIR", tmp_path / "charts")
    charts = tmp_path / "chartfrag"
    charts.mkdir()
    monkeypatch.setattr(warn_charts, "CHARTS_DIR", charts)
    monkeypatch.setattr(warn_site_us, "CHARTS_DIR", charts)
    monkeypatch.delenv("SIGNUP_ENDPOINT", raising=False)
    warn_datasets.reset_cache()

    site = tmp_path / "site"
    warn_site_us.build_us_site(national_file=national_file, out_dir=site)
    warn_publish.build_site(MANIFEST, MONITOR, out_dir=site / "ca")

    ca = json.loads((site / "ca" / "data.json").read_text())["records"]
    us_ca = [
        r for r in json.loads((site / "data.json").read_text())["records"]
        if r.get("state") == "CA"
        and str(r.get("notice_date") or "") >= warn_datasets.CA_COVERAGE_START
    ]

    def by_year(records):
        out = {}
        for r in records:
            year = str(r["notice_date"])[:4]
            count, employees = out.get(year, (0, 0))
            out[year] = (count + 1, employees + int(r["employees"] or 0))
        return out

    assert by_year(ca) == by_year(us_ca)
    assert by_year(ca)                      # and the comparison is not vacuous
