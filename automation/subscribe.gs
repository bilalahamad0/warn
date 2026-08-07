/**
 * subscribe.gs — Google Apps Script Web App backing the dashboard signup form.
 *
 * It stores {timestamp, name, email, source} rows in a Google Sheet and lets the
 * WARN pipeline read the list (with a shared token) so subscribers can be emailed.
 *
 * It also keeps a simple dashboard visitor counter in a "pageviews" sheet,
 * incremented via GET ?action=hit and read (without incrementing) via
 * GET ?action=views. The dashboard footer calls these to show a visit count.
 *
 * ── Endpoints ───────────────────────────────────────────────────────────────
 * POST {name, email, source, states}          → sign up / ADD preferences
 *      → { ok: true }
 *      → { ok: true, duplicate: true, updated: bool, states: "CA,NY" }
 *      Signup is additive: a returning address keeps every state it already
 *      had and gains whatever was submitted (see _mergeStates). Only the
 *      unsubscribe/preferences flow below can remove a subscription.
 * POST {action:'unsubscribe', e, s, states[], digest}
 *      → keep exactly the confirmed selection for address `e` — the ONLY
 *        path that may narrow a subscription, because its page loads and
 *        displays the current selection first
 *      → { ok: true, removed: true,  found: bool }        (row deleted)
 *      → { ok: true, removed: false, found: true, states: "CA,US" }
 *      → { ok: false, error: 'forbidden' }                (bad signature)
 * GET  ?action=hit    → { ok: true, count: N }   (increments the counter)
 * GET  ?action=views  → { ok: true, count: N }
 * GET  ?action=prefs&e=<email>&s=<sig>
 *      → { ok: true, email, states: [...], digest: bool, found: bool }
 *      → { ok: false, error: 'forbidden' }
 * GET  (no token)     → { count: N }
 * GET  ?token=<LIST_TOKEN> → { ok: true, count, subscribers: [...] }
 *
 * ── Signed unsubscribe links ────────────────────────────────────────────────
 * `s` is HMAC-SHA256(key = LIST_TOKEN, msg = lowercased email), hex, first 32
 * chars — exactly what warn_subscribers.unsubscribe_signature() mints for the
 * links in outgoing email. The signature is the only credential the prefs and
 * unsubscribe endpoints accept: it is address-specific, so nobody can read or
 * change somebody else's preferences by editing the query string, and the
 * shared LIST_TOKEN never leaves the server. A wrong or missing signature
 * always answers 'forbidden' without revealing whether the address exists.
 *
 * ── One-time setup ──────────────────────────────────────────────────────────
 * 1. Create a Google Sheet (this becomes the subscriber database).
 * 2. Extensions ▸ Apps Script. Delete the boilerplate and paste this whole file.
 * 3. Project Settings ▸ Script properties ▸ add property:
 *        LIST_TOKEN = <a long random string>
 *    Use the SAME value as the SUBSCRIBERS_TOKEN GitHub secret. It doubles as
 *    the key that signs unsubscribe links, so the two MUST stay identical.
 * 4. Deploy ▸ New deployment ▸ type "Web app".
 *        Execute as: Me
 *        Who has access: Anyone
 *    Deploy, authorize, and copy the Web app URL ending in /exec.
 * 5. In GitHub: add repo variable  SIGNUP_ENDPOINT = <that /exec URL>
 *               add repo secret    SUBSCRIBERS_TOKEN = <same as LIST_TOKEN>
 * 6. RE-DEPLOY after pasting this version: Manage deployments ▸ edit (pencil)
 *    ▸ Version: New version ▸ Deploy. The /exec URL keeps serving the OLD
 *    code until you do — the prefs/unsubscribe endpoints stay 404-ish and
 *    every unsubscribe link in already-sent email fails silently.
 *
 * Re-deploy (Manage deployments ▸ edit ▸ Version: New version) after any edits.
 */

var SHEET_NAME = 'subscribers';

/**
 * Column E holds subscription preferences: comma-separated 2-letter state
 * codes, plus the sentinel "US" for the whole-country monthly digest
 * (e.g. "CA", "CA,NY", "US", "TX,US"). Rows created before preferences
 * existed have a blank cell and are treated as "CA" by the pipeline, so
 * existing subscribers keep getting California alerts.
 */
var PREF_COL = 5;
var DEFAULT_STATES = 'CA';

/** Sentinel in the states column meaning "whole-US monthly digest", never a
 *  state (matches warn_subscribers.DIGEST_CODE). */
var DIGEST_CODE = 'US';

function _sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(['timestamp', 'name', 'email', 'source', 'states']);
    return sh;
  }
  // Add the states header to sheets created before preferences existed.
  if (sh.getLastColumn() < PREF_COL) {
    sh.getRange(1, PREF_COL).setValue('states');
  }
  return sh;
}

/**
 * Union of what a subscriber already has with what they just asked for.
 *
 * Signup is ADDITIVE, and deliberately so. Neither signup form loads the
 * subscriber's current selection — the California form has no state picker at
 * all, and the US dashboard's picker starts blank on every visit — so neither
 * can show somebody what they are about to lose. A form that cannot display
 * your preferences must not be allowed to destroy them.
 *
 * Before this, re-subscribing overwrote the cell. Picking IL+NY on the US
 * dashboard and then signing up on the California page silently cancelled the
 * Illinois and New York alerts; so did coming back to the US form months later
 * and ticking one more state.
 *
 * Removing a subscription is the unsubscribe/preferences page's job
 * (`?action=prefs` + `action:'unsubscribe'`). That page loads the current
 * selection, shows it, and writes back exactly what the subscriber confirmed —
 * it is the one surface where destructive intent is visible, so it is the one
 * surface with destructive power.
 *
 * _cleanStates dedupes, so the concatenation cannot produce repeats.
 */
function _mergeStates(existing, incoming) {
  return _cleanStates(String(existing || '') + ',' + String(incoming || ''));
}

/** Normalize a submitted preference string to "CA,NY" / "US" form. */
function _cleanStates(raw) {
  var tokens = String(raw || '').toUpperCase().split(/[^A-Z]+/);
  var out = [];
  for (var i = 0; i < tokens.length; i++) {
    var t = tokens[i];
    if (t.length === 2 && out.indexOf(t) === -1) out.push(t);
  }
  return out.join(',');
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function _isEmail(v) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v);
}

/** Same normalization the signup path applies before storing an address. */
function _normEmail(v) {
  return String(v || '').trim().toLowerCase().slice(0, 200);
}

function _listToken() {
  return PropertiesService.getScriptProperties().getProperty('LIST_TOKEN') || '';
}

/**
 * Lowercase hex HMAC-SHA256 of `value` keyed by `key`, truncated to 32 chars.
 *
 * Utilities.computeHmacSha256Signature returns a Java byte[], whose elements
 * arrive as SIGNED bytes (-128..127), so 0x80-and-up bytes come through
 * negative and need +256 before hex; bytes below 0x10 need a leading zero or
 * the digest silently loses characters and shifts. Must reproduce
 * warn_subscribers.unsubscribe_signature() exactly. Reference vector:
 *   _hmacHex('me@example.com', 'test-secret-123')
 *     === 'b71371db806cc29b7660ac1369591ea5'
 */
function _hmacHex(value, key) {
  var raw = Utilities.computeHmacSha256Signature(String(value), String(key));
  var hex = '';
  for (var i = 0; i < raw.length; i++) {
    var b = raw[i];
    if (b < 0) b += 256;
    if (b < 0x10) hex += '0';
    hex += b.toString(16);
  }
  return hex.slice(0, 32);
}

/**
 * True when `sig` is the signature this script would mint for `email`.
 *
 * Compares lengths first and then accumulates the difference over the whole
 * string, so a mismatch costs the same time wherever it falls and the loop can
 * never read past either end. Returns false when LIST_TOKEN is unset — an
 * unconfigured script must reject every link rather than accept every link.
 */
function _checkSig(email, sig) {
  var secret = _listToken();
  var given = String(sig || '');
  var addr = _normEmail(email);
  if (!secret || !addr || !given) return false;
  var want = _hmacHex(addr, secret);
  if (want.length !== given.length) return false;
  var diff = 0;
  for (var i = 0; i < want.length; i++) {
    diff |= (want.charCodeAt(i) ^ given.charCodeAt(i));
  }
  return diff === 0;
}

/** 1-based sheet row for an address, or -1. Never returns the header row. */
function _findSubscriberRow(sh, email) {
  var n = Math.max(sh.getLastRow() - 1, 0);
  if (n <= 0) return -1;
  var col = sh.getRange(2, 3, n, 1).getValues();
  for (var i = 0; i < col.length; i++) {
    if (String(col[i][0]).trim().toLowerCase() === email) return i + 2;
  }
  return -1;
}

/**
 * Split a stored states cell into { states: [...], digest: bool }.
 * Mirrors warn_subscribers._parse_prefs, including the backfill that reads a
 * BLANK cell as California (rows that predate preferences).
 */
function _splitPrefs(raw) {
  var text = String(raw || '').trim();
  if (!text) return { states: [DEFAULT_STATES], digest: false };
  var tokens = text.toUpperCase().split(/[^A-Z]+/);
  var states = [];
  var digest = false;
  for (var i = 0; i < tokens.length; i++) {
    var t = tokens[i];
    if (!t) continue;
    if (t === DIGEST_CODE) { digest = true; continue; }
    if (t.length === 2 && states.indexOf(t) === -1) states.push(t);
  }
  return { states: states, digest: digest };
}

var COUNTER_SHEET = 'pageviews';

/** Single-cell visitor counter; cell B1 holds the running total. */
function _counterSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(COUNTER_SHEET);
  if (!sh) {
    sh = ss.insertSheet(COUNTER_SHEET);
    sh.getRange('A1').setValue('pageviews');
    sh.getRange('B1').setValue(0);
  }
  return sh;
}

function _getViews() {
  return Number(_counterSheet().getRange('B1').getValue()) || 0;
}

function _bumpViews() {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = _counterSheet();
    var n = (Number(sh.getRange('B1').getValue()) || 0) + 1;
    sh.getRange('B1').setValue(n);
    return n;
  } finally {
    lock.releaseLock();
  }
}

/**
 * GET ?action=prefs&e=<email>&s=<sig> — what is this address subscribed to?
 *
 * Signature-gated: without a valid one the answer is a flat 'forbidden' that
 * says nothing about whether the address is on the list, so the endpoint can
 * never be walked to enumerate subscribers. A correctly signed address that
 * is NOT on the list gets ok:true / found:false, which is what lets the page
 * say "you're not subscribed" instead of showing an empty form.
 */
function _handlePrefs(params) {
  var email = _normEmail(params && params.e);
  if (!_isEmail(email) || !_checkSig(email, params && params.s)) {
    return _json({ ok: false, error: 'forbidden' });
  }

  var sh = _sheet();
  var row = _findSubscriberRow(sh, email);
  if (row < 0) {
    return _json({
      ok: true, email: email, states: [], digest: false, found: false,
    });
  }
  var prefs = _splitPrefs(sh.getRange(row, PREF_COL).getValue());
  return _json({
    ok: true,
    email: email,
    states: prefs.states,
    digest: prefs.digest,
    found: true,
  });
}

/**
 * POST {action:'unsubscribe', e, s, states:[...], digest} — apply the
 * visitor's confirmed selection: keep exactly the states listed plus the
 * digest flag, drop everything else.
 *
 * CRITICAL: an EMPTY selection deletes the whole row. A blank states cell is
 * read as California by the pipeline (warn_subscribers.DEFAULT_STATES, the
 * backfill for rows predating preferences), so clearing column E instead of
 * deleting the row would silently re-subscribe them to CA — the opposite of
 * what they just asked for. Never setValue('') here.
 *
 * An address that is not on the list answers ok:true / removed:true /
 * found:false: the end state they asked for already holds, and the reply
 * still reveals nothing to anyone without a valid signature.
 */
function _handleUnsubscribe(data) {
  var email = _normEmail(data.e || data.email);
  if (!_isEmail(email) || !_checkSig(email, data.s)) {
    return _json({ ok: false, error: 'forbidden' });
  }

  // states[] may arrive as an array or a string; _cleanStates handles both.
  var requested = _cleanStates(data.states);
  var kept = requested ? requested.split(',') : [];
  var digest = data.digest === true || data.digest === 'true' ||
    kept.indexOf(DIGEST_CODE) !== -1;
  var states = [];
  for (var i = 0; i < kept.length; i++) {
    if (kept[i] !== DIGEST_CODE) states.push(kept[i]);
  }
  if (digest) states.push(DIGEST_CODE);
  var cell = states.join(',');

  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = _sheet();
    var row = _findSubscriberRow(sh, email);
    if (row < 0) return _json({ ok: true, removed: true, found: false });
    if (!cell) {
      sh.deleteRow(row);
      return _json({ ok: true, removed: true, found: true });
    }
    sh.getRange(row, PREF_COL).setValue(cell);
    return _json({ ok: true, removed: false, found: true, states: cell });
  } finally {
    lock.releaseLock();
  }
}

/** Handle signups (called by the dashboard form via fetch POST). */
function doPost(e) {
  try {
    var data = {};
    if (e && e.postData && e.postData.contents) {
      try { data = JSON.parse(e.postData.contents); }
      catch (_) { data = (e && e.parameter) || {}; }
    } else {
      data = (e && e.parameter) || {};
    }

    // Unsubscribe posts carry their own signature and skip the signup path.
    if (String(data.action || '') === 'unsubscribe') {
      return _handleUnsubscribe(data);
    }

    // Honeypot: bots fill the hidden "company" field — accept silently and drop.
    if (data.company) return _json({ ok: true });

    var name = String(data.name || '').trim().slice(0, 120);
    var email = String(data.email || '').trim().toLowerCase().slice(0, 200);
    if (!_isEmail(email)) return _json({ ok: false, error: 'invalid_email' });
    var states = _cleanStates(data.states) || DEFAULT_STATES;

    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      var sh = _sheet();
      var n = Math.max(sh.getLastRow() - 1, 0);
      if (n > 0) {
        var existing = sh.getRange(2, 3, n, 1).getValues();
        for (var i = 0; i < existing.length; i++) {
          if (String(existing[i][0]).trim().toLowerCase() === email) {
            // Re-subscribing ADDS to the existing selection — it never
            // replaces it. See _mergeStates for why. A blank cell means
            // California (DEFAULT_STATES), so read it that way before merging
            // rather than letting a legacy subscriber's implicit CA vanish.
            var cell = _cleanStates(
              sh.getRange(i + 2, PREF_COL).getValue()
            ) || DEFAULT_STATES;
            var merged = _mergeStates(cell, states);
            if (merged !== cell) sh.getRange(i + 2, PREF_COL).setValue(merged);
            return _json({
              ok: true,
              duplicate: true,
              updated: merged !== cell,
              states: merged,
            });
          }
        }
      }
      sh.appendRow([
        new Date().toISOString(),
        name,
        email,
        String(data.source || 'dashboard').slice(0, 60),
        states,
      ]);
    } finally {
      lock.releaseLock();
    }
    return _json({ ok: true });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

/**
 * ?action=hit   -> increment visitor count, return { ok: true, count: N }
 * ?action=views -> read visitor count only,  return { ok: true, count: N }
 * ?action=prefs -> signed preference lookup (see _handlePrefs)
 * No token  -> public signup count: { count: N }
 * Valid token -> full list: { ok: true, count, subscribers: [{timestamp,name,email}] }
 * Bad token -> { ok: false, error: 'forbidden' }
 */
function doGet(e) {
  var params = (e && e.parameter) || {};

  // Visitor counter for the dashboard footer (no token required).
  if (params.action === 'hit') return _json({ ok: true, count: _bumpViews() });
  if (params.action === 'views') return _json({ ok: true, count: _getViews() });

  // Unsubscribe page: the per-address HMAC is the credential, not LIST_TOKEN,
  // so the shared secret never has to be shipped to a browser.
  if (params.action === 'prefs') return _handlePrefs(params);

  var token = params.token || '';
  var secret = PropertiesService.getScriptProperties().getProperty('LIST_TOKEN') || '';
  var sh = _sheet();
  var n = Math.max(sh.getLastRow() - 1, 0);

  if (!token) return _json({ count: n });
  if (token !== secret) return _json({ ok: false, error: 'forbidden' });

  var rows = n > 0 ? sh.getRange(2, 1, n, PREF_COL).getValues() : [];
  var subs = rows
    .map(function (r) {
      return {
        timestamp: r[0],
        name: r[1],
        email: String(r[2]).trim().toLowerCase(),
        // Blank for rows that predate preferences — the pipeline reads that
        // as California so existing subscribers are unaffected.
        states: String(r[PREF_COL - 1] || '').trim(),
      };
    })
    .filter(function (s) { return s.email; });
  return _json({ ok: true, count: subs.length, subscribers: subs });
}
