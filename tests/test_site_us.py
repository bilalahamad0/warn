"""Tests for the standalone US dashboard builder (warn_site_us)."""

import json
from datetime import datetime, timezone

import pytest

import warn_charts
import warn_site_us
import warn_urls


YEAR = datetime.now(timezone.utc).year


@pytest.fixture
def national_payload():
    return {
        "last_updated": f"{YEAR}-07-01T00:00:00Z",
        "states_live": 2,
        "total_records": 3,
        "total_employees": 700,
        "states": {
            "CA": {"name": "California", "total_records": 2, "total_employees": 600},
            "NJ": {"name": "New Jersey", "total_records": 1, "total_employees": 100},
        },
        "records": [
            {"state": "CA", "company": "Acme", "notice_date": f"{YEAR}-03-01",
             "effective_date": f"{YEAR}-05-01", "employees": 500, "city": "LA"},
            {"state": "CA", "company": "Beta", "notice_date": f"{YEAR - 1}-06-01",
             "effective_date": f"{YEAR - 1}-08-01", "employees": 100, "city": "SF"},
            {"state": "NJ", "company": "Gamma", "notice_date": None,
             "effective_date": f"{YEAR}-04-15", "employees": 100, "city": "Newark"},
        ],
    }


def _build(tmp_path, payload, monkeypatch, endpoint=None):
    """Build the US site from an arbitrary payload into tmp_path."""
    charts = tmp_path / "charts"
    charts.mkdir(exist_ok=True)
    # Keep chart fragments out of the real docs/charts during tests.
    monkeypatch.setattr(warn_charts, "CHARTS_DIR", charts)
    monkeypatch.setattr(warn_site_us, "CHARTS_DIR", charts)
    if endpoint is None:
        monkeypatch.delenv("SIGNUP_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("SIGNUP_ENDPOINT", endpoint)
    nat = tmp_path / "warn_national.json"
    nat.write_text(json.dumps(payload))
    # Named "site", not "us": this builder writes the site root now.
    out = tmp_path / "site"
    index = warn_site_us.build_us_site(national_file=nat, out_dir=out)
    return index, out


@pytest.fixture
def built_site(tmp_path, national_payload, monkeypatch):
    return _build(tmp_path, national_payload, monkeypatch)


def _decode_index_row(row):
    """Python mirror of the page's decodeRow(), for round-trip assertions.

    The trailing four fields are located from the end of the string because a
    company name may legitimately contain the separator.
    """
    end, cuts = len(row), []
    for _ in range(4):
        end = row.rfind(warn_site_us.INDEX_SEP, 0, end)
        cuts.insert(0, end)
    first = row.index(warn_site_us.INDEX_SEP)

    def date(v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if v else "—"

    def num(v):
        return f"{int(v):,}" if v else "—"

    return [
        row[:first],
        row[first + 1:cuts[0]],
        row[cuts[0] + 1:cuts[1]],
        date(row[cuts[1] + 1:cuts[2]]),
        date(row[cuts[2] + 1:cuts[3]]),
        num(row[cuts[3] + 1:]),
    ]


def test_kpis_current_year_window(national_payload):
    k = warn_site_us.compute_us_kpis(national_payload)
    # Beta's 100 employees are last year — excluded from the year window.
    assert k["year_notices"] == 2
    assert k["year_employees"] == 600
    assert k["largest"] == {"company": "Acme", "state": "CA", "employees": 500}
    assert k["top_state"]["state"] == "CA"
    assert k["states_live"] == 2
    assert k["total_employees"] == 700


def test_build_us_site_writes_page_and_api(built_site):
    index, out = built_site
    html = index.read_text()
    assert "US WARN Layoff Tracker" in html
    assert "2 states live" in html
    # Both states appear in the filter and the table rows.
    assert '<option value="CA">CA</option>' in html
    assert '<option value="NJ">NJ</option>' in html
    assert 'data-state="NJ"' in html
    # A record with no notice_date renders an em dash, never a fabricated date.
    assert "Gamma" in html
    api = json.loads((out / "data.json").read_text())
    assert api["states_live"] == 2
    assert len(api["records"]) == 3


def test_build_us_site_missing_national_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        warn_site_us.build_us_site(
            national_file=tmp_path / "nope.json", out_dir=tmp_path / "us"
        )


# ---------------------------------------------------------------------------
# Search index — searching pages through the whole dataset, not one page
# ---------------------------------------------------------------------------


def test_search_index_written_for_every_dated_record(built_site):
    index, out = built_site
    idx = json.loads((out / warn_site_us.SEARCH_INDEX_NAME).read_text())
    assert idx["page_size"] == warn_site_us.PAGE_SIZE
    # All three fixture records carry a usable event date.
    assert idx["total"] == 3 == len(idx["rows"])
    companies = [_decode_index_row(r)[1] for r in idx["rows"]]
    assert sorted(companies) == ["Acme", "Beta", "Gamma"]


def test_search_index_rows_match_the_page_files_exactly(built_site):
    """A search hit must render identically to the same row when browsing."""
    index, out = built_site
    idx = json.loads((out / warn_site_us.SEARCH_INDEX_NAME).read_text())
    page = json.loads((out / "pages" / "all" / "1.json").read_text())
    decoded = [_decode_index_row(r) for r in idx["rows"]]
    assert decoded == page["rows"]


def test_search_index_reports_missing_values_as_em_dash(built_site):
    """Gamma has no notice date — the index must not invent one."""
    index, out = built_site
    idx = json.loads((out / warn_site_us.SEARCH_INDEX_NAME).read_text())
    gamma = [r for r in idx["rows"] if "Gamma" in r][0]
    assert _decode_index_row(gamma)[3] == "—"


def test_search_index_survives_a_company_name_containing_the_separator(
    tmp_path, national_payload, monkeypatch
):
    piped = "Bed Bath & Beyond | Buy Buy Baby Inc | BBB Value Services"
    national_payload["records"].append(
        {"state": "NJ", "company": piped, "notice_date": f"{YEAR}-02-02",
         "effective_date": f"{YEAR}-03-03", "employees": 1293, "city": "Union"}
    )
    index, out = _build(tmp_path, national_payload, monkeypatch)
    idx = json.loads((out / warn_site_us.SEARCH_INDEX_NAME).read_text())
    row = [r for r in idx["rows"] if "Buy Buy Baby" in r][0]
    # Company text is stored verbatim; the other fields still decode.
    assert _decode_index_row(row) == [
        "NJ", piped, "Union", f"{YEAR}-02-02", f"{YEAR}-03-03", "1,293"
    ]


def test_search_index_is_not_loaded_until_the_visitor_searches(built_site):
    """No tag references the index — only the JS fetch triggered by typing."""
    index, out = built_site
    html = index.read_text()
    name = warn_site_us.SEARCH_INDEX_NAME
    assert f"var SEARCH_INDEX_URL = '{name}'" in html
    assert f'src="{name}"' not in html
    assert f'href="{name}"' not in html
    assert "loading search index…" in html
    # The old per-page-only search is gone.
    assert "search applies per page" not in html
    assert "matching record(s) for" in html


# ---------------------------------------------------------------------------
# Email signup
# ---------------------------------------------------------------------------


def test_signup_form_offers_a_checkbox_per_live_state(built_site):
    index, _out = built_site
    html = index.read_text()
    for code in ("CA", "NJ"):
        assert (
            f'<input type="checkbox" class="sub-state" value="{code}">{code}'
            in html
        )
    # Nothing is pre-selected — subscribers opt in explicitly.
    grid = html.split('<div class="sub-grid">')[1].split("</div>")[0]
    assert 'value="CA"' in grid and "checked" not in grid
    # Honeypot, digest opt-in, submit and status message all present.
    assert 'id="sub-company-hp"' in html
    assert 'id="sub-digest"' in html
    assert "Monthly summary of" in html and "whole US" in html
    assert 'id="sub-submit"' in html
    assert 'id="subscribe-msg"' in html
    # Digest sentinel is appended after the state codes: "CA,US".
    assert "var DIGEST_CODE = 'US';" in html
    assert "states: states.join(',')" in html
    assert "Pick at least one state, or the monthly US summary." in html
    assert "alert(" not in html


def test_signup_form_has_no_checkbox_for_states_without_data(built_site):
    index, _out = built_site
    html = index.read_text()
    assert 'class="sub-state" value="AR"' not in html


def test_signup_endpoint_injected_at_build_time(
    tmp_path, national_payload, monkeypatch
):
    url = "https://script.google.com/macros/s/TESTID/exec"
    index, _out = _build(tmp_path, national_payload, monkeypatch, endpoint=url)
    assert f'var SIGNUP_ENDPOINT = "{url}";' in index.read_text()


def test_signup_degrades_when_endpoint_unset(built_site):
    index, _out = built_site
    html = index.read_text()
    assert 'var SIGNUP_ENDPOINT = "";' in html
    assert "Signups aren't configured yet" in html


# ---------------------------------------------------------------------------
# Site layout: this builder owns the site ROOT
# ---------------------------------------------------------------------------


def test_default_out_dir_is_the_site_root():
    """The national dashboard is the front page.

    Guards the standalone `python3 warn_site_us.py` entry point: while the
    default was docs/us/ that command would rebuild the whole 14 MB site on top
    of the redirect stub that now lives there.
    """
    assert warn_site_us.SITE_DIR.name == "docs"
    assert warn_site_us.LEGACY_US_DIR.name == "us"
    assert warn_site_us.LEGACY_US_DIR.parent == warn_site_us.SITE_DIR


def test_write_pages_only_clears_its_own_directory(
    tmp_path, national_payload, monkeypatch
):
    """`pages/` is wiped wholesale each build — at the site root that rmtree is
    one directory away from California, the chart fragments and the unsubscribe
    page, so pin exactly what it may delete."""
    out = tmp_path / "site"
    (out / "ca").mkdir(parents=True)
    (out / "ca" / "index.html").write_text("california")
    (out / "charts").mkdir(parents=True)
    (out / "charts" / "12_us_map.html").write_text("chart")
    (out / "us").mkdir(parents=True)
    (out / "us" / "index.html").write_text("stub")
    (out / "unsubscribe.html").write_text("unsub")
    (out / "pages" / "stale").mkdir(parents=True)
    (out / "pages" / "stale" / "9.json").write_text("[]")

    charts = tmp_path / "chartfrag"
    charts.mkdir()
    monkeypatch.setattr(warn_charts, "CHARTS_DIR", charts)
    monkeypatch.setattr(warn_site_us, "CHARTS_DIR", charts)
    monkeypatch.delenv("SIGNUP_ENDPOINT", raising=False)
    nat = tmp_path / "warn_national.json"
    nat.write_text(json.dumps(national_payload))
    warn_site_us.build_us_site(national_file=nat, out_dir=out)

    assert (out / "ca" / "index.html").read_text() == "california"
    assert (out / "charts" / "12_us_map.html").read_text() == "chart"
    assert (out / "us" / "index.html").read_text() == "stub"
    assert (out / "unsubscribe.html").read_text() == "unsub"
    assert not (out / "pages" / "stale").exists()   # only its own dir is pruned
    assert (out / "pages" / "all" / "1.json").exists()


def test_us_page_routes_visitors_to_california(built_site):
    """California is a sibling directory now, not the parent."""
    index, _out = built_site
    html = index.read_text()
    assert 'href="ca/"' in html
    assert 'href="../"' not in html     # the old up-link would leave the site


def test_state_filter_points_ca_visitors_at_the_ca_dashboard(built_site):
    """Selecting CA filters the table but leaves every chart national, so the
    page has to offer the route the filter itself is not."""
    index, _out = built_site
    html = index.read_text()
    assert 'id="ca-hint"' in html
    assert "stfilterEl.value !== 'CA'" in html


def test_head_carries_icons_manifest_canonical_and_og(built_site):
    index, _out = built_site
    html = index.read_text()
    for asset in ("apple-touch-icon.png", "favicon-32x32.png", "favicon-16x16.png",
                  "favicon.ico", "icon.svg", "site.webmanifest"):
        # Bare relative names: this page sits at the root beside its assets.
        assert f'href="{asset}"' in html
    assert f'<link rel="canonical" href="{warn_urls.US_DASHBOARD_URL}">' in html
    assert f'<meta property="og:url" content="{warn_urls.US_DASHBOARD_URL}">' in html
    assert f'<meta property="og:image" content="{warn_urls.OG_IMAGE_URL}">' in html


def test_api_payload_declares_its_scope(built_site):
    """/warn/data.json used to serve California. It serves the nation now, and
    GitHub Pages cannot redirect a JSON file — so the payload says which it is."""
    _index, out = built_site
    assert json.loads((out / "data.json").read_text())["scope"] == "us"


# ---------------------------------------------------------------------------
# The /us/ redirect stub
# ---------------------------------------------------------------------------


def test_legacy_stub_redirects_to_the_root(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    site.joinpath("data.json").write_text('{"scope": "us"}')
    legacy = site / "us"

    stub = warn_site_us.build_legacy_us_redirect(site_dir=site, legacy_dir=legacy)
    html = stub.read_text()

    assert stub == legacy / "index.html"
    assert '<meta http-equiv="refresh" content="0; url=../">' in html
    assert f'<link rel="canonical" href="{warn_urls.US_DASHBOARD_URL}">' in html
    assert "location.replace('../' + location.search + location.hash)" in html
    assert '<a href="../">' in html      # works with JS and meta-refresh off
    assert '<a href="../ca/">' in html   # California is still reachable
    # A 0-second refresh reads as a permanent redirect and consolidates ranking
    # into the canonical; noindex would suppress that instead.
    assert "noindex" not in html
    # canonical must precede the refresh, for crawlers that stop parsing early.
    assert html.index("canonical") < html.index("http-equiv")


def test_legacy_data_json_is_byte_identical_to_the_root(tmp_path):
    """Old integrations polling /warn/us/data.json keep working, and the copy is
    made from the written file rather than re-serialised so the two cannot
    drift (git stores one blob for both)."""
    site = tmp_path / "site"
    site.mkdir()
    payload = json.dumps({"scope": "us", "records": [1, 2, 3]})
    site.joinpath("data.json").write_text(payload)
    legacy = site / "us"

    warn_site_us.build_legacy_us_redirect(site_dir=site, legacy_dir=legacy)
    assert (legacy / "data.json").read_bytes() == (site / "data.json").read_bytes()


def test_legacy_stub_stays_tiny(tmp_path):
    """Fails loudly if anyone ever re-points the full build at docs/us/."""
    site = tmp_path / "site"
    site.mkdir()
    stub = warn_site_us.build_legacy_us_redirect(site_dir=site, legacy_dir=site / "us")
    assert len(stub.read_text()) < 4000
