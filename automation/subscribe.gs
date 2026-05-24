/**
 * subscribe.gs — Google Apps Script Web App backing the dashboard signup form.
 *
 * It stores {timestamp, name, email, source} rows in a Google Sheet and lets the
 * WARN pipeline read the list (with a shared token) so subscribers can be emailed.
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

function _sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(['timestamp', 'name', 'email', 'source']);
  }
  return sh;
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function _isEmail(v) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v);
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

    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      var sh = _sheet();
      var n = Math.max(sh.getLastRow() - 1, 0);
      if (n > 0) {
        var existing = sh.getRange(2, 3, n, 1).getValues();
        for (var i = 0; i < existing.length; i++) {
          if (String(existing[i][0]).trim().toLowerCase() === email) {
            return _json({ ok: true, duplicate: true });
          }
        }
      }
      sh.appendRow([
        new Date().toISOString(),
        name,
        email,
        String(data.source || 'dashboard').slice(0, 60),
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
 * No token  -> public signup count: { count: N }
 * Valid token -> full list: { ok: true, count, subscribers: [{timestamp,name,email}] }
 * Bad token -> { ok: false, error: 'forbidden' }
 */
function doGet(e) {
  var token = (e && e.parameter && e.parameter.token) || '';
  var secret = PropertiesService.getScriptProperties().getProperty('LIST_TOKEN') || '';
  var sh = _sheet();
  var n = Math.max(sh.getLastRow() - 1, 0);

  if (!token) return _json({ count: n });
  if (token !== secret) return _json({ ok: false, error: 'forbidden' });

  var rows = n > 0 ? sh.getRange(2, 1, n, 3).getValues() : [];
  var subs = rows
    .map(function (r) {
      return { timestamp: r[0], name: r[1], email: String(r[2]).trim().toLowerCase() };
    })
    .filter(function (s) { return s.email; });
  return _json({ ok: true, count: subs.length, subscribers: subs });
}
