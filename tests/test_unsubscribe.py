"""Build-time contract for the signed unsubscribe page (warn_unsubscribe).

The page's *runtime* behaviour is exercised in a real browser (fetch stubbed,
POST bodies inspected); these tests pin the things the build is responsible
for: the file lands where the emails link to, the endpoint is injected safely,
an unset endpoint degrades to an explanation instead of a dead button, and the
page stays entirely self-contained.
"""

import json
import re

import pytest

import warn_unsubscribe
from warn_subscribers import DIGEST_CODE, UNSUBSCRIBE_PAGE

ENDPOINT = "https://script.google.com/macros/s/AKfakeDeployment/exec"


@pytest.fixture
def built(tmp_path):
    """Page built into a temp dir with a known endpoint."""
    path = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path, endpoint=ENDPOINT
    )
    return path, path.read_text()


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def test_writes_the_page_the_emails_link_to(built, tmp_path):
    path, html = built
    # warn_subscribers mints links to this exact filename.
    assert path == tmp_path / UNSUBSCRIBE_PAGE
    assert path.name == "unsubscribe.html"
    assert path.exists()
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")


def test_creates_missing_output_directory(tmp_path):
    out = tmp_path / "docs" / "nested"
    path = warn_unsubscribe.build_unsubscribe_page(
        out_dir=out, endpoint=ENDPOINT
    )
    assert path.exists()
    assert path.parent == out


def test_defaults_to_docs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(warn_unsubscribe, "DOCS_DIR", tmp_path / "docs")
    path = warn_unsubscribe.build_unsubscribe_page(endpoint=ENDPOINT)
    assert path == tmp_path / "docs" / "unsubscribe.html"
    assert path.exists()


def test_rebuild_is_byte_identical(tmp_path):
    first = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path, endpoint=ENDPOINT
    ).read_bytes()
    second = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path, endpoint=ENDPOINT
    ).read_bytes()
    assert first == second


# ---------------------------------------------------------------------------
# Endpoint injection
# ---------------------------------------------------------------------------


def test_endpoint_injected_as_js_string(built):
    _, html = built
    assert f'var ENDPOINT = "{ENDPOINT}";' in html


def test_endpoint_read_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNUP_ENDPOINT", ENDPOINT)
    html = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path
    ).read_text()
    assert f'var ENDPOINT = "{ENDPOINT}";' in html


def test_environment_endpoint_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNUP_ENDPOINT", f"  {ENDPOINT}\n")
    html = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path
    ).read_text()
    assert f'var ENDPOINT = "{ENDPOINT}";' in html


@pytest.mark.parametrize("value", ["", "   ", None])
def test_unset_endpoint_degrades_gracefully(tmp_path, monkeypatch, value):
    """No endpoint → the page still builds and explains itself."""
    monkeypatch.delenv("SIGNUP_ENDPOINT", raising=False)
    path = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path, endpoint=value
    )
    html = path.read_text()
    assert path.exists() and len(html) > 1000
    assert 'var ENDPOINT = "";' in html
    # The visitor is told why, rather than shown a button that cannot work.
    assert "Unsubscribe isn" in html and "configured yet" in html
    assert "script.google.com" not in html


def test_endpoint_cannot_break_out_of_the_script_block(tmp_path):
    """A hostile/typo'd endpoint stays inside its JS string literal."""
    nasty = 'https://x.test/exec"; alert(1); //</script><script>'
    html = warn_unsubscribe.build_unsubscribe_page(
        out_dir=tmp_path, endpoint=nasty
    ).read_text()
    assert "</script><script>" not in html
    assert "\\u003c/script>" in html
    # Exactly one script block in the document.
    assert html.count("<script>") == 1
    assert html.count("</script>") == 1


# ---------------------------------------------------------------------------
# Self-contained: no external assets
# ---------------------------------------------------------------------------


def test_no_external_resources(built):
    _, html = built
    assert "<script src" not in html
    assert "<link" not in html
    assert "<img" not in html
    assert "@import" not in html
    assert "url(http" not in html
    assert "fonts.googleapis" not in html
    assert "cdn." not in html
    # Inline style + inline script only.
    assert "<style>" in html and "<script>" in html


def test_only_outbound_url_is_the_endpoint(built):
    _, html = built
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    assert urls == {ENDPOINT}


def test_links_back_to_both_dashboards(built):
    """The page sits at the site root, so "./" is the national dashboard and
    California is one level down. Both must stay relative — the page is allowed
    exactly one absolute URL (test_only_outbound_url_is_the_endpoint)."""
    _, html = built
    assert 'href="./"' in html          # US dashboard (site root)
    assert 'href="ca/"' in html         # California dashboard
    assert 'href="us/"' not in html     # the pre-2026-08 US address is gone


# ---------------------------------------------------------------------------
# Structure the runtime JS (and the browser test) depends on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "element_id",
    [
        "status",
        "notice",
        "unsub-form",
        "who",
        "mode-keep",
        "mode-all",
        "subs",
        "sub-rows",
        "effect",
        "confirm-btn",
        "done",
        "done-title",
        "done-detail",
    ],
)
def test_expected_element_ids_present(built, element_id):
    _, html = built
    assert f'id="{element_id}"' in html


def test_global_choice_is_a_real_radio_pair(built):
    _, html = built
    assert (
        '<input type="radio" id="mode-keep" name="mode" value="keep" checked>'
        in html
    )
    assert '<input type="radio" id="mode-all" name="mode" value="all">' in html
    # Two options, one name, and "keep" is the safe default.
    assert html.count('name="mode"') == 2


def test_labels_are_real_and_wired(built):
    _, html = built
    assert 'for="mode-keep"' in html
    assert 'for="mode-all"' in html
    # Per-subscription rows are built at runtime; they must get a label too.
    assert "row.setAttribute('for', id)" in html
    assert "Keep the selected subscriptions" in html
    assert "Unsubscribe from everything" in html


def test_accessibility_affordances(built):
    _, html = built
    assert html.count('aria-live="polite"') >= 2
    assert html.count('role="status"') >= 2
    assert '<meta name="viewport" content="width=device-width' in html
    assert 'lang="en"' in html
    # Touch targets: the button and every selection row clear 44px.
    assert "min-height:48px" in html          # .btn
    assert "min-height:52px" in html          # .row
    assert "alert(" not in html               # never a native alert()


def test_no_horizontal_overflow_guards(built):
    _, html = built
    assert "max-width:620px" in html
    assert "overflow-wrap:break-word" in html
    assert "overflow-wrap:anywhere" in html   # long addresses in .who


# ---------------------------------------------------------------------------
# Wire contract with the Apps Script endpoint
# ---------------------------------------------------------------------------


def test_prefs_are_read_with_a_signed_get(built):
    _, html = built
    assert "'action=prefs&e=' + encodeURIComponent(email) +" in html
    assert "'&s=' + encodeURIComponent(sig)" in html
    assert "fetch(url, { method: 'GET' })" in html


def test_confirm_posts_the_agreed_payload(built):
    _, html = built
    body = html[html.index("body: JSON.stringify({"):]
    body = body[: body.index("})")]
    # Order matters only for readability; presence and names are the contract.
    for key in ("action: 'unsubscribe'", "e: email", "s: sig",
                "states: states", "digest: digest"):
        assert key in body
    # One POST in the whole page, and it is the Confirm handler.
    assert html.count("method: 'POST'") == 1


def test_full_unsubscribe_sends_empty_states_and_false_digest(built):
    _, html = built
    assert "var states = full ? [] : keptStates();" in html
    assert "var digest = full ? false : keptDigest();" in html
    # An empty tick-list is treated as a full removal, and says so first.
    assert "return !keptStates().length && !keptDigest();" in html
    assert "Confirming removes this address from every " in html


def test_digest_sentinel_matches_warn_subscribers(built):
    _, html = built
    assert f'var DIGEST_CODE = "{DIGEST_CODE}";' in html
    assert "Monthly US summary" in html


def test_state_names_embedded_for_full_labels(built):
    _, html = built
    match = re.search(r"var STATE_NAMES = (\{.*?\});", html)
    assert match, "STATE_NAMES literal not found"
    names = json.loads(match.group(1))
    assert names["CA"] == "California"
    assert names["NY"] == "New York"
    assert names["DC"] == "District of Columbia"
    assert len(names) >= 51
    assert DIGEST_CODE not in names   # "US" is a sentinel, not a state


def test_handles_the_documented_failure_modes(built):
    _, html = built
    assert "function showForbidden()" in html
    assert "function showMissing()" in html
    assert "function showUnconfigured()" in html
    assert "function showNoParams()" in html
    assert "Network error" in html
    assert "isn" in html and "valid any more" in html


def test_opening_the_link_never_changes_anything(built):
    """A prefetching mail client must not unsubscribe anybody."""
    _, html = built
    # The POST lives inside submit(), which is only bound to the form event.
    assert "elForm.addEventListener('submit', submit);" in html
    submit_at = html.index("function submit(ev)")
    assert html.index("method: 'POST'") > submit_at
