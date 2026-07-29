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
 * ── One-time setup ──────────────────────────────────────────────────────────
 * 1. Create a Google Sheet (this becomes the subscriber database).
 * 2. Extensions ▸ Apps Script. Delete the boilerplate and paste this whole file.
 * 3. Project Settings ▸ Script properties ▸ add property:
 *        LIST_TOKEN = <a long random string>
 *    Use the SAME value as the SUBSCRIBERS_TOKEN GitHub secret.
 * 4. Deploy ▸ New deployment ▸ type "Web app".
 *        Execute as: Me
 *        Who has access: Anyone
 *    Deploy, authorize, and copy the Web app URL ending in /exec.
 * 5. In GitHub: add repo variable  SIGNUP_ENDPOINT = <that /exec URL>
 *               add repo secret    SUBSCRIBERS_TOKEN = <same as LIST_TOKEN>
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
            // Re-subscribing updates preferences rather than duplicating.
            sh.getRange(i + 2, PREF_COL).setValue(states);
            return _json({ ok: true, duplicate: true, updated: true });
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
 * No token  -> public signup count: { count: N }
 * Valid token -> full list: { ok: true, count, subscribers: [{timestamp,name,email}] }
 * Bad token -> { ok: false, error: 'forbidden' }
 */
function doGet(e) {
  var params = (e && e.parameter) || {};

  // Visitor counter for the dashboard footer (no token required).
  if (params.action === 'hit') return _json({ ok: true, count: _bumpViews() });
  if (params.action === 'views') return _json({ ok: true, count: _getViews() });

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
