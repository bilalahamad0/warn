"""
warn_unsubscribe.py
-------------------
Builds the signed unsubscribe / preference page at ``docs/unsubscribe.html``.

Every alert and digest email carries a signed link minted by
``warn_subscribers.unsubscribe_url`` — ``unsubscribe.html?e=<email>&s=<sig>``,
where ``s`` is an HMAC of the lowercased address keyed by the shared
``SUBSCRIBERS_TOKEN``. This page is the landing surface for that link: it asks
the Apps Script endpoint what the visitor is currently subscribed to, shows one
row per subscription (per-state alerts plus the whole-US monthly summary), and
posts back the subscriptions they chose to keep.

Design rules:

* **Nothing changes until the visitor presses Confirm.** Opening the link is
  never itself an unsubscribe — mail clients and security scanners prefetch
  links, so a GET must stay read-only.
* **Rows start ticked**, i.e. pre-selected as *subscribed*; unticking one marks
  it for removal. Checkboxes (not radios) because subscriptions are
  independent. The all-or-nothing decision above them is a real radio pair,
  since those two outcomes genuinely exclude each other.
* **An empty selection is a full unsubscribe**, and the page says so in words
  before the button is pressed.
* **Self-contained**: inline CSS and JS, no external scripts, fonts or images,
  so the page works from any inbox, on any network, offline caches included.
* The endpoint is injected at build time from ``SIGNUP_ENDPOINT``. When it is
  unset the page still renders and explains that unsubscribe is not configured
  yet — it never fails silently or shows a dead button.

Usage:
    python3 warn_unsubscribe.py          # (re)build docs/unsubscribe.html
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

try:  # SIGNUP_ENDPOINT lives in .env locally, in CI vars on Actions.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass

from warn_digest import STATE_NAMES
from warn_subscribers import DIGEST_CODE

BASE_DIR = Path(__file__).parent
DOCS_DIR = BASE_DIR / "docs"
PAGE_NAME = "unsubscribe.html"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
log = logging.getLogger("warn_unsubscribe")


def _js(value) -> str:
    """Serialise a Python value as a JS literal that is safe inside <script>.

    ``json.dumps`` leaves ``<`` alone, so a value containing ``</script>``
    would end the block early; escaping it as ``\\u003c`` keeps the literal
    inert while parsing back to exactly the same string.
    """
    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


# The template carries a lot of literal CSS/JS braces, so it uses @@TOKEN@@
# placeholders rather than str.format() — no brace doubling to get wrong.
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Email preferences · WARN Layoff Tracker</title>
<style>
/* Mobile-first: base styles target phones, the media query adds the wider
   layout. Same palette as the dashboards (warn_site_us.py). */
:root { --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
        --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --err:#f78166;
        --warn:#d29922; }
* { box-sizing:border-box; margin:0; padding:0; }
html { -webkit-text-size-adjust:100%; }
body { background:var(--bg); color:var(--text);
       font:14px/1.55 Inter,system-ui,sans-serif;
       overflow-wrap:break-word; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
header { border-bottom:1px solid var(--border); padding:12px 14px; }
header .brand { font-size:15px; font-weight:600; }
header .sub { display:block; color:var(--muted); font-size:12px;
              margin-top:2px; }
main { max-width:620px; margin:0 auto; padding:16px 14px 40px; }
h1 { font-size:19px; margin-bottom:6px; }
.lede { color:var(--muted); font-size:13px; margin-bottom:16px; }
.card { background:var(--card); border:1px solid var(--border);
        border-radius:12px; padding:14px; margin-bottom:14px; }
.who { font-size:12.5px; color:var(--muted); margin-bottom:14px;
       overflow-wrap:anywhere; }
.who b { color:var(--text); font-weight:600; }
.status { font-size:13px; color:var(--muted); padding:12px 14px;
          border:1px solid var(--border); border-radius:10px;
          background:var(--card); margin-bottom:14px; }
.status.err { color:var(--err); border-color:#f7816655; }
.status.ok { color:var(--ok); border-color:#3fb95055; }
fieldset { border:0; margin:0 0 16px; padding:0; }
legend { font-size:12px; letter-spacing:.05em; text-transform:uppercase;
         color:var(--muted); margin-bottom:8px; padding:0; }
legend .hint { text-transform:none; letter-spacing:0; }
/* Big, touch-friendly selection rows: ≥44px tall on the smallest phone. */
.row { display:flex; align-items:flex-start; gap:12px; width:100%;
       min-height:52px; padding:13px 14px; margin-bottom:8px;
       background:var(--bg); border:1px solid var(--border);
       border-radius:10px; cursor:pointer; }
.row input { width:20px; height:20px; flex:0 0 auto; margin-top:1px;
             accent-color:var(--accent); cursor:pointer; }
.row .rowtext { min-width:0; }
.row b { display:block; font-size:14.5px; font-weight:600; }
.row .hint { display:block; color:var(--muted); font-size:12px;
             margin-top:3px; }
.row:hover { border-color:var(--accent); }
.row:focus-within { border-color:var(--accent);
                    box-shadow:0 0 0 2px rgba(88,166,255,.35); }
.row.code b { font-variant-numeric:tabular-nums; }
.subs.off { opacity:.45; }
.subs.off .row { cursor:not-allowed; }
.effect { font-size:13px; color:var(--muted); min-height:1.2em;
          margin-bottom:14px; }
.effect.warn { color:var(--warn); }
.btn { display:block; width:100%; min-height:48px; padding:13px 18px;
       background:var(--accent); color:#0d1117; border:0; border-radius:10px;
       font-size:15px; font-weight:600; cursor:pointer; }
.btn:hover { filter:brightness(1.07); }
.btn:disabled { opacity:.6; cursor:not-allowed; }
.btn.ghost { background:var(--card); color:var(--accent);
             border:1px solid var(--border); }
.done h2 { font-size:16px; margin-bottom:6px; }
.done .detail { color:var(--muted); font-size:13px; margin-top:8px; }
.links { font-size:13px; color:var(--muted); text-align:center;
         margin-top:22px; }
[hidden] { display:none !important; }
@media (min-width: 720px) {
  body { font-size:15px; }
  header { padding:14px 24px; }
  main { padding:26px 24px 56px; }
  h1 { font-size:22px; }
  .card { padding:20px; }
  .btn { width:auto; min-width:260px; }
}
</style>
</head>
<body>
<header>
  <span class="brand">WARN Layoff Tracker</span>
  <span class="sub">Email preferences</span>
</header>
<main>
  <h1>Manage your WARN emails</h1>
  <p class="lede">Nothing changes until you press Confirm.</p>

  <div id="status" class="status" role="status" aria-live="polite">
    Loading your subscriptions…</div>
  <div id="notice" class="card" hidden></div>

  <form id="unsub-form" class="card" hidden>
    <p class="who" id="who"></p>

    <fieldset id="mode-group">
      <legend>What would you like to do?</legend>
      <label class="row" for="mode-keep">
        <input type="radio" id="mode-keep" name="mode" value="keep" checked>
        <span class="rowtext"><b>Keep the selected subscriptions</b>
          <span class="hint">Only the ones you untick below are
            removed.</span></span>
      </label>
      <label class="row" for="mode-all">
        <input type="radio" id="mode-all" name="mode" value="all">
        <span class="rowtext"><b>Unsubscribe from everything</b>
          <span class="hint">Removes this address from every WARN
            email.</span></span>
      </label>
    </fieldset>

    <fieldset id="subs" class="subs">
      <legend>Your subscriptions
        <span class="hint">— ticked = you stay subscribed</span></legend>
      <div id="sub-rows"></div>
    </fieldset>

    <p class="effect" id="effect" role="status" aria-live="polite"></p>
    <button type="submit" class="btn" id="confirm-btn">Confirm</button>
  </form>

  <div id="done" class="card done" hidden>
    <h2 id="done-title"></h2>
    <p class="detail" id="done-detail"></p>
  </div>

  <p class="links">
    <a href="./">California dashboard</a> ·
    <a href="us/">US dashboard</a>
  </p>
</main>
<script>
(function () {
  'use strict';
  var ENDPOINT = @@ENDPOINT@@;
  var STATE_NAMES = @@STATE_NAMES@@;
  var DIGEST_CODE = @@DIGEST_CODE@@;
  var DIGEST_LABEL = 'Monthly US summary';

  var params = new URLSearchParams(window.location.search);
  var email = (params.get('e') || '').trim();
  var sig = (params.get('s') || '').trim();

  var elStatus = document.getElementById('status');
  var elNotice = document.getElementById('notice');
  var elForm = document.getElementById('unsub-form');
  var elWho = document.getElementById('who');
  var elRows = document.getElementById('sub-rows');
  var elSubs = document.getElementById('subs');
  var elEffect = document.getElementById('effect');
  var elBtn = document.getElementById('confirm-btn');
  var elDone = document.getElementById('done');
  var elDoneTitle = document.getElementById('done-title');
  var elDoneDetail = document.getElementById('done-detail');

  var original = { states: [], digest: false };
  var savedKeep = null;   // selection stashed while "everything" is picked

  var LINKS = ' <a href="./">California dashboard</a> or ' +
              '<a href="us/">US dashboard</a>.';

  function setStatus(text, kind) {
    elStatus.textContent = text || '';
    elStatus.className = 'status' + (kind ? ' ' + kind : '');
    elStatus.hidden = !text;
  }

  // Only ever called with page-authored markup — never with server data.
  function setNotice(html) {
    elNotice.innerHTML = html;
    elNotice.hidden = false;
    setStatus('');
    elForm.hidden = true;
  }

  function stateLabel(code) {
    return STATE_NAMES[code] || code;
  }

  function labelFor(code) {
    return code === DIGEST_CODE
      ? DIGEST_LABEL
      : code + ' — ' + stateLabel(code);
  }

  function addRow(id, value, kind, title, hint) {
    var row = document.createElement('label');
    row.className = 'row code';
    row.setAttribute('for', id);
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.id = id;
    box.className = 'keep';
    box.value = value;
    box.checked = true;
    box.setAttribute('data-kind', kind);
    var text = document.createElement('span');
    text.className = 'rowtext';
    var strong = document.createElement('b');
    strong.textContent = title;
    var small = document.createElement('span');
    small.className = 'hint';
    small.textContent = hint;
    text.appendChild(strong);
    text.appendChild(small);
    row.appendChild(box);
    row.appendChild(text);
    elRows.appendChild(row);
    box.addEventListener('change', updateEffect);
  }

  function boxes() {
    return Array.prototype.slice.call(document.querySelectorAll('.keep'));
  }

  function currentMode() {
    var picked = document.querySelector('input[name=mode]:checked');
    return picked ? picked.value : 'keep';
  }

  function keptStates() {
    return boxes()
      .filter(function (b) {
        return b.checked && b.getAttribute('data-kind') === 'state';
      })
      .map(function (b) { return b.value; });
  }

  function keptDigest() {
    return boxes().some(function (b) {
      return b.checked && b.getAttribute('data-kind') === 'digest';
    });
  }

  function joinList(items) {
    if (items.length <= 1) return items.join('');
    if (items.length === 2) return items[0] + ' and ' + items[1];
    return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }

  function isFullRemoval() {
    if (currentMode() === 'all') return true;
    return !keptStates().length && !keptDigest();
  }

  function updateEffect() {
    if (isFullRemoval()) {
      elEffect.className = 'effect warn';
      elEffect.textContent = 'Confirming removes this address from every ' +
        'WARN email — per-state alerts and the monthly summary. You can ' +
        'sign up again any time on the dashboards.';
      elBtn.textContent = 'Confirm — unsubscribe from everything';
      return;
    }
    var states = keptStates();
    var digest = keptDigest();
    var kept = states.map(labelFor);
    if (digest) kept.push(DIGEST_LABEL);
    var dropped = original.states.filter(function (c) {
      return states.indexOf(c) < 0;
    }).map(labelFor);
    if (original.digest && !digest) dropped.push(DIGEST_LABEL);
    elEffect.className = 'effect';
    elEffect.textContent = dropped.length
      ? 'Confirming stops ' + joinList(dropped) + '. You keep ' +
        joinList(kept) + '.'
      : 'Nothing is deselected — confirming keeps ' + joinList(kept) + '.';
    elBtn.textContent = 'Confirm';
  }

  function syncMode() {
    var all = currentMode() === 'all';
    if (all) {
      if (savedKeep === null) {
        savedKeep = boxes().map(function (b) { return b.checked; });
      }
      boxes().forEach(function (b) { b.checked = false; b.disabled = true; });
    } else {
      boxes().forEach(function (b, i) {
        b.disabled = false;
        if (savedKeep) b.checked = savedKeep[i];
      });
      savedKeep = null;
    }
    elSubs.classList.toggle('off', all);
    updateEffect();
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('input[name=mode]'),
    function (r) { r.addEventListener('change', syncMode); }
  );

  function render(prefs) {
    original = prefs;
    elRows.innerHTML = '';
    prefs.states.forEach(function (code) {
      addRow('keep-' + code, code, 'state', labelFor(code),
             'An email for each new ' + stateLabel(code) +
             ' WARN notice.');
    });
    if (prefs.digest) {
      addRow('keep-digest', DIGEST_CODE, 'digest', DIGEST_LABEL,
             'One email a month covering every state we track.');
    }
    elWho.innerHTML = 'Subscriptions for <b></b>';
    elWho.querySelector('b').textContent = prefs.email || email;
    setStatus('');
    elNotice.hidden = true;
    elForm.hidden = false;
    savedKeep = null;
    syncMode();
  }

  function parsePrefs(data) {
    var raw = data && data.states;
    var list = Array.isArray(raw)
      ? raw.slice()
      : String(raw === undefined || raw === null ? '' : raw).split(/[,;\\s]+/);
    var digest = !!(data && data.digest);
    var seen = {};
    var states = [];
    list.forEach(function (token) {
      var code = String(token || '').trim().toUpperCase();
      if (!code) return;
      if (code === DIGEST_CODE) { digest = true; return; }
      if (code.length !== 2 || seen[code]) return;
      seen[code] = true;
      states.push(code);
    });
    return {
      states: states,
      digest: digest,
      email: (data && data.email) || ''
    };
  }

  function errorCode(data) {
    if (!data) return '';
    return String(data.error || data.reason || data.message || '')
      .toLowerCase();
  }

  function looksForbidden(status, data) {
    if (status === 401 || status === 403) return true;
    var code = errorCode(data);
    return /forbid|signature|unauthor|invalid|expired|bad_?sig/.test(code);
  }

  function looksMissing(data) {
    if (data && data.found === false) return true;
    return /not_?found|not_?subscribed|unknown|no_?such/
      .test(errorCode(data));
  }

  function showForbidden() {
    setNotice('<h2>That link isn\\'t valid any more</h2>' +
      '<p class="detail">The unsubscribe link is either mistyped, ' +
      'incomplete or has been replaced by a newer one. Open the link ' +
      'straight from your most recent WARN email, or manage your ' +
      'subscription from the' + LINKS + '</p>');
  }

  function showMissing() {
    setNotice('<h2>Nothing to unsubscribe</h2>' +
      '<p class="detail">This address isn\\'t on the WARN mailing list, ' +
      'so there is nothing to remove. You can subscribe on the' +
      LINKS + '</p>');
  }

  function showUnconfigured() {
    setNotice('<h2>Unsubscribe isn\\'t configured yet</h2>' +
      '<p class="detail">This copy of the site was built without a ' +
      'subscription endpoint, so preferences cannot be changed here. ' +
      'Reply to any WARN alert email and you will be removed by hand. ' +
      'Dashboards:' + LINKS + '</p>');
  }

  function showNoParams() {
    setNotice('<h2>Open this page from a WARN email</h2>' +
      '<p class="detail">Unsubscribe links are personal and signed, so ' +
      'this page needs the full link from the bottom of one of your ' +
      'WARN emails. Dashboards:' + LINKS + '</p>');
  }

  function showDone(states, digest) {
    elForm.hidden = true;
    elNotice.hidden = true;
    setStatus('');
    if (!states.length && !digest) {
      elDoneTitle.textContent =
        "You've been unsubscribed from all WARN emails.";
      elDoneDetail.textContent = 'Anything already queued may still ' +
        'arrive, but nothing new will be sent. You are welcome back any ' +
        'time from the dashboards below.';
    } else {
      var kept = states.map(labelFor);
      if (digest) kept.push(DIGEST_LABEL);
      elDoneTitle.textContent = 'Your subscriptions were updated.';
      elDoneDetail.textContent = 'You will keep receiving ' +
        joinList(kept) + '. Everything else has been removed.';
    }
    elDone.hidden = false;
    elDone.setAttribute('tabindex', '-1');
    elDone.focus();
  }

  function submit(ev) {
    if (ev) ev.preventDefault();
    var full = currentMode() === 'all';
    var states = full ? [] : keptStates();
    var digest = full ? false : keptDigest();
    var label = elBtn.textContent;
    elBtn.disabled = true;
    elBtn.textContent = 'Saving…';
    setStatus('');
    var status = 0;
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({
        action: 'unsubscribe',
        e: email,
        s: sig,
        states: states,
        digest: digest
      })
    })
      .then(function (r) { status = r.status; return r.json(); })
      .then(function (data) {
        if (data && data.ok) {
          showDone(states, digest);
        } else if (looksForbidden(status, data)) {
          showForbidden();
        } else if (looksMissing(data)) {
          showMissing();
        } else {
          setStatus('Something went wrong and nothing was changed. ' +
            'Please try again in a moment.', 'err');
        }
      })
      .catch(function () {
        setStatus('Network error — nothing was changed. Check your ' +
          'connection and press Confirm again.', 'err');
      })
      .then(function () {
        elBtn.disabled = false;
        if (elBtn.textContent === 'Saving…') elBtn.textContent = label;
      });
  }

  elForm.addEventListener('submit', submit);

  function loadPrefs() {
    if (!ENDPOINT) { showUnconfigured(); return; }
    if (!email || !sig) { showNoParams(); return; }
    setStatus('Loading your subscriptions…');
    var url = ENDPOINT + (ENDPOINT.indexOf('?') < 0 ? '?' : '&') +
      'action=prefs&e=' + encodeURIComponent(email) +
      '&s=' + encodeURIComponent(sig);
    var status = 0;
    fetch(url, { method: 'GET' })
      .then(function (r) { status = r.status; return r.json(); })
      .then(function (data) {
        if (looksForbidden(status, data)) { showForbidden(); return; }
        if (!data || data.ok === false) {
          if (looksMissing(data)) { showMissing(); return; }
          setStatus('Could not load your subscriptions. Please try ' +
            'again in a moment.', 'err');
          return;
        }
        var prefs = parsePrefs(data);
        if (!prefs.states.length && !prefs.digest) {
          showMissing();
          return;
        }
        render(prefs);
      })
      .catch(function () {
        setStatus('Network error — could not load your subscriptions. ' +
          'Check your connection and reload this page.', 'err');
      });
  }

  loadPrefs();
})();
</script>
</body>
</html>
"""


def render_unsubscribe_html(endpoint: str = "") -> str:
    """Return the finished page HTML with the endpoint baked in."""
    return (
        TEMPLATE.replace("@@ENDPOINT@@", _js(str(endpoint or "").strip()))
        .replace("@@STATE_NAMES@@", _js(STATE_NAMES))
        .replace("@@DIGEST_CODE@@", _js(DIGEST_CODE))
    )


def build_unsubscribe_page(
    out_dir: Optional[Path] = None, endpoint: Optional[str] = None
) -> Path:
    """Write ``unsubscribe.html`` into ``out_dir`` (default ``docs/``).

    ``endpoint`` defaults to the ``SIGNUP_ENDPOINT`` environment variable — the
    same Apps Script ``/exec`` URL the signup forms post to. When it is empty
    the page is still written; it then tells visitors that unsubscribe is not
    configured instead of showing a button that cannot work.
    """
    out = Path(out_dir) if out_dir is not None else DOCS_DIR
    out.mkdir(parents=True, exist_ok=True)
    if endpoint is None:
        endpoint = os.getenv("SIGNUP_ENDPOINT", "")
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        log.warning(
            "SIGNUP_ENDPOINT not set — unsubscribe page built in "
            "'not configured' mode."
        )
    path = out / PAGE_NAME
    path.write_text(render_unsubscribe_html(endpoint), encoding="utf-8")
    log.info(f"Wrote {path}")
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    built = build_unsubscribe_page()
    print(f"Built {built} ({built.stat().st_size:,} bytes)")
