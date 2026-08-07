"""
test_subscribe_gs.py
--------------------
Guard rails for automation/subscribe.gs, the Google Apps Script Web App.

Apps Script only ever runs on Google's servers, so this file does two things:

1. STATIC checks — parse the .gs source and assert the invariants a reviewer
   would otherwise have to eyeball, above all the one that silently corrupts
   the subscriber list if it regresses: a full unsubscribe must DELETE the
   row, never blank column E. A blank states cell is read as California by
   warn_subscribers._parse_prefs (the backfill for rows that predate
   preferences), so blanking would re-subscribe the person who just left.

2. BEHAVIOURAL checks — execute the real .gs through node against in-memory
   Apps Script shims (SpreadsheetApp / LockService / PropertiesService /
   ContentService / Utilities). These skip automatically when node is absent.
   The HMAC shim mirrors Utilities.computeHmacSha256Signature by handing back
   SIGNED bytes, the way a Java byte[] surfaces in Apps Script, so the hex
   encoding in _hmacHex is exercised for real rather than assumed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import warn_subscribers

REPO = Path(__file__).resolve().parent.parent
GS_PATH = REPO / "automation" / "subscribe.gs"
SRC = GS_PATH.read_text(encoding="utf-8")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _strip_comments(js):
    """Drop /* */ and // comments so prose can't satisfy (or trip) a check.

    Kept deliberately simple — subscribe.gs has no comment markers inside
    string or regex literals, which the test below asserts.
    """
    out = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", out)


# Comment-free source: the assertions below are about code, not documentation.
CODE = _strip_comments(SRC)

# Contract reference vector, shared with warn_subscribers.
REF_TOKEN = "test-secret-123"
REF_EMAIL = "me@example.com"
REF_SIG = "b71371db806cc29b7660ac1369591ea5"

HEADER = ["timestamp", "name", "email", "source", "states"]


def _row(email, states, name="Sub"):
    return ["2026-01-01T00:00:00Z", name, email, "dashboard", states]


def _body(source, name):
    """Return the {...} body of a top-level `function name(` by brace match."""
    m = re.search(r"^function\s+" + re.escape(name) + r"\s*\(", source, re.M)
    assert m, f"{name}() is missing from subscribe.gs"
    start = source.index("{", m.end())
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


# ---------------------------------------------------------------------------
# Static: the pieces exist and are wired into the entry points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn", ["_hmacHex", "_checkSig", "_handlePrefs", "_handleUnsubscribe"]
)
def test_helper_functions_defined(fn):
    assert re.search(r"^function\s+" + fn + r"\s*\(", CODE, re.M)


def test_comment_stripping_only_removed_comments():
    """Guard the guard: the checks above are worthless on mangled source."""
    for marker in (
        "function doGet",
        "function doPost",
        "createTextOutput(JSON.stringify(obj))",
        "PropertiesService.getScriptProperties",
        "/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/",   # regex literal left intact
        "split(/[^A-Z]+/)",
    ):
        assert marker in CODE, f"stripper ate {marker!r}"
    # Braces still balance, so _body() cannot silently return a partial body.
    assert CODE.count("{") == CODE.count("}")


@needs_node
def test_script_is_syntactically_valid_javascript(tmp_path):
    # Copied to .js first: `node --check` refuses an unknown extension.
    js = tmp_path / "subscribe.js"
    js.write_text(SRC, encoding="utf-8")
    proc = subprocess.run(
        [NODE, "--check", str(js)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_hmac_uses_apps_script_primitive_with_sign_and_pad_fixes():
    body = _body(CODE, "_hmacHex")
    assert "Utilities.computeHmacSha256Signature" in body
    # Signed-byte fix: Java byte[] delivers 0x80+ as negative numbers.
    assert re.search(r"if\s*\(\s*b\s*<\s*0\s*\)", body)
    # Zero padding for bytes below 0x10, or the digest silently shifts.
    assert re.search(r"b\s*<\s*0x10|b\s*<\s*16", body)
    assert "toString(16)" in body
    # Truncated to 32 hex chars, matching hexdigest()[:32] in Python.
    assert re.search(r"slice\(0,\s*32\)|substring\(0,\s*32\)", body)


def test_checksig_is_length_safe_and_fails_closed_without_a_token():
    body = _body(CODE, "_checkSig")
    # Length compared before the character loop: no read past either end.
    assert re.search(r"\.length\s*!==?\s*\w+\.length", body)
    # Difference accumulated over the whole string rather than early-returning.
    assert "^" in body and "|=" in body
    # No LIST_TOKEN -> reject everything (never accept everything).
    assert re.search(r"if\s*\(!secret", body)


def test_doget_routes_prefs_and_doPost_routes_unsubscribe():
    get_body = _body(CODE, "doGet")
    assert re.search(r"action\s*===?\s*'prefs'", get_body)
    assert "_handlePrefs" in get_body
    post_body = _body(CODE, "doPost")
    assert re.search(r"===?\s*'unsubscribe'", post_body)
    assert "_handleUnsubscribe" in post_body


def test_existing_doget_and_signup_behaviour_is_preserved():
    """The additions must not disturb the endpoints already in production."""
    get_body = _body(CODE, "doGet")
    for kept in ("'hit'", "'views'", "_bumpViews", "_getViews", "'forbidden'"):
        assert kept in get_body
    assert "token !== secret" in get_body
    post_body = _body(CODE, "doPost")
    for kept in ("data.company", "invalid_email", "appendRow", "duplicate"):
        assert kept in post_body


# ---------------------------------------------------------------------------
# Static: the safety rules
# ---------------------------------------------------------------------------


def test_empty_selection_deletes_the_row_and_never_blanks_the_cell():
    """THE critical invariant — see the module docstring."""
    body = _body(CODE, "_handleUnsubscribe")
    m = re.search(r"if\s*\(\s*!\s*cell\s*\)\s*\{", body)
    assert m, "no explicit empty-selection branch in _handleUnsubscribe"
    branch = _body("function x() " + body[m.end() - 1:], "x")
    assert "deleteRow" in branch, "empty selection must delete the row"
    assert "setValue" not in branch, (
        "empty selection must not write the states cell — a blank cell is "
        "read as California and would re-subscribe them"
    )


def test_no_code_path_blanks_the_states_column():
    """setValue('') on column E is the exact bug this endpoint must avoid."""
    assert not re.search(r"setValue\(\s*(''|\"\")\s*\)", CODE)


@pytest.mark.parametrize("fn", ["_handlePrefs", "_handleUnsubscribe"])
def test_signature_check_precedes_any_subscriber_access(fn):
    body = _body(CODE, fn)
    gate = body.index("_checkSig")
    for reader in ("_sheet()", "_findSubscriberRow", "getRange", "deleteRow"):
        idx = body.find(reader)
        if idx != -1:
            assert gate < idx, f"{fn}: {reader} runs before the signature check"
    # And the check actually short-circuits with a forbidden response.
    assert re.search(r"if\s*\(.*_checkSig[^)]*\)\s*\)\s*\{", body, re.S)
    assert "'forbidden'" in body[gate:gate + 400]


def test_forbidden_reply_leaks_nothing_about_the_address():
    """A rejected signature must not hint whether the email is on the list."""
    body = _body(CODE, "_handlePrefs")
    reject = body[: body.index("_sheet()")]
    assert "found" not in reject
    assert re.search(r"error:\s*'forbidden'", reject)


def test_lockservice_wraps_the_unsubscribe_mutation():
    body = _body(CODE, "_handleUnsubscribe")
    assert "LockService.getScriptLock" in body
    lock = body.index("LockService.getScriptLock")
    assert "waitLock" in body
    for op in ("deleteRow", "setValue"):
        assert op in body, f"{op} disappeared from _handleUnsubscribe"
        assert lock < body.index(op), f"{op} happens outside the lock"
    assert "releaseLock" in body
    # released in a finally, so an exception cannot strand the lock
    assert re.search(r"finally\s*\{\s*lock\.releaseLock\(\);?\s*\}", body)


def test_digest_sentinel_matches_the_python_side():
    m = re.search(r"var\s+DIGEST_CODE\s*=\s*'([^']+)'", CODE)
    assert m and m.group(1) == warn_subscribers.DIGEST_CODE
    m = re.search(r"var\s+DEFAULT_STATES\s*=\s*'([^']+)'", CODE)
    assert m and (m.group(1),) == warn_subscribers.DEFAULT_STATES


def test_header_documents_the_new_endpoints_and_the_redeploy_step():
    header = SRC[: SRC.index("var SHEET_NAME")]
    assert "action=prefs" in header
    assert "unsubscribe" in header
    for shape in ("removed", "found", "forbidden"):
        assert shape in header
    # The operator must publish a new version or none of this goes live.
    assert "Manage deployments" in header
    assert re.search(r"RE-DEPLOY", header)
    assert "New version" in header


# ---------------------------------------------------------------------------
# Behavioural: run the real .gs through node
# ---------------------------------------------------------------------------

HARNESS_JS = r"""
/* Apps Script emulator: runs subscribe.gs against in-memory shims. */
var fs = require('fs');
var crypto = require('crypto');

var scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
var events = [];

function Sheet(name, rows) {
  this.name = name;
  this.rows = (rows || []).map(function (r) { return r.slice(); });
}
Sheet.prototype.getLastRow = function () { return this.rows.length; };
Sheet.prototype.getLastColumn = function () {
  return this.rows.reduce(function (m, r) { return Math.max(m, r.length); }, 0);
};
Sheet.prototype.getRange = function (a, b, c, d) {
  var sh = this;
  if (typeof a === 'string') {
    var col = a.toUpperCase().charCodeAt(0) - 64;
    return sh.getRange(parseInt(a.slice(1), 10), col);
  }
  var row = a, col2 = b;
  var nRows = c === undefined ? 1 : c, nCols = d === undefined ? 1 : d;
  return {
    getValues: function () {
      events.push({ op: 'getValues', row: row, col: col2 });
      var out = [];
      for (var i = 0; i < nRows; i++) {
        var line = [];
        for (var j = 0; j < nCols; j++) {
          var r = sh.rows[row - 1 + i] || [];
          var v = r[col2 - 1 + j];
          line.push(v === undefined ? '' : v);
        }
        out.push(line);
      }
      return out;
    },
    getValue: function () { return this.getValues()[0][0]; },
    setValue: function (v) {
      events.push({ op: 'setValue', row: row, col: col2, value: v });
      while (sh.rows.length < row) sh.rows.push([]);
      var r = sh.rows[row - 1];
      while (r.length < col2) r.push('');
      r[col2 - 1] = v;
      return this;
    }
  };
};
Sheet.prototype.appendRow = function (values) {
  events.push({ op: 'appendRow', values: values.slice() });
  this.rows.push(values.slice());
};
Sheet.prototype.deleteRow = function (row) {
  events.push({ op: 'deleteRow', row: row });
  this.rows.splice(row - 1, 1);
};

var sheets = { subscribers: new Sheet('subscribers', scenario.rows || []) };

var SpreadsheetApp = {
  getActiveSpreadsheet: function () {
    return {
      getSheetByName: function (n) { return sheets[n] || null; },
      insertSheet: function (n) {
        sheets[n] = new Sheet(n, []);
        return sheets[n];
      }
    };
  }
};

var LockService = {
  getScriptLock: function () {
    return {
      waitLock: function (ms) { events.push({ op: 'waitLock', ms: ms }); },
      releaseLock: function () { events.push({ op: 'releaseLock' }); }
    };
  }
};

var PropertiesService = {
  getScriptProperties: function () {
    return {
      getProperty: function (k) {
        return k === 'LIST_TOKEN' ? (scenario.token || null) : null;
      }
    };
  }
};

var ContentService = {
  MimeType: { JSON: 'application/json' },
  createTextOutput: function (text) {
    return {
      _t: text,
      setMimeType: function () { return this; },
      getContent: function () { return this._t; }
    };
  }
};

var Utilities = {
  /* Apps Script returns a Java byte[]: signed bytes in -128..127. */
  computeHmacSha256Signature: function (value, key) {
    var mac = crypto
      .createHmac('sha256', Buffer.from(String(key), 'utf8'))
      .update(Buffer.from(String(value), 'utf8'))
      .digest();
    return Array.prototype.slice.call(mac).map(function (b) {
      return b > 127 ? b - 256 : b;
    });
  }
};

/* Direct eval in this sloppy-mode module scope hoists subscribe.gs's
   function declarations into scope, bound to the shims above. */
eval(fs.readFileSync(process.argv[2], 'utf8'));

function payload(out) {
  try { return JSON.parse(out.getContent()); }
  catch (err) { return { __unparsable: String(out && out.getContent()) }; }
}

var results = (scenario.calls || []).map(function (call) {
  if (call.kind === 'hmac') return _hmacHex(call.value, call.key);
  if (call.kind === 'checkSig') return _checkSig(call.email, call.sig);
  if (call.kind === 'get') {
    return payload(doGet({ parameter: call.params || {} }));
  }
  if (call.kind === 'post') {
    var e = { postData: { contents: JSON.stringify(call.body || {}) } };
    return payload(doPost(e));
  }
  throw new Error('unknown call kind: ' + call.kind);
});

process.stdout.write(JSON.stringify({
  results: results,
  rows: sheets.subscribers.rows,
  events: events
}));
"""


@pytest.fixture(scope="session")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("gs") / "harness.js"
    path.write_text(HARNESS_JS, encoding="utf-8")
    return path


def run_gs(harness, calls, rows=None, token=REF_TOKEN):
    """Execute subscribe.gs under node; return the parsed harness output."""
    scenario = {
        "token": token,
        "rows": [HEADER] + list(rows or []),
        "calls": calls,
    }
    proc = subprocess.run(
        [NODE, str(harness), str(GS_PATH)],
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def mutations(out):
    ops = ("setValue", "deleteRow", "appendRow")
    return [e for e in out["events"] if e["op"] in ops]


@needs_node
def test_hmac_matches_the_contract_reference_vector(harness):
    out = run_gs(
        harness, [{"kind": "hmac", "value": REF_EMAIL, "key": REF_TOKEN}]
    )
    assert out["results"][0] == REF_SIG


@needs_node
@pytest.mark.parametrize(
    "email",
    [
        "me@example.com",
        "a@b.co",
        "someone.with+tag@long-domain.example.org",
        "tëst@example.com",  # non-ASCII: both sides must sign UTF-8
    ],
)
def test_hmac_agrees_with_warn_subscribers(harness, monkeypatch, email):
    """Both halves of the contract must mint byte-identical signatures."""
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", REF_TOKEN)
    expected = warn_subscribers.unsubscribe_signature(email)
    assert len(expected) == 32
    out = run_gs(harness, [{"kind": "hmac", "value": email, "key": REF_TOKEN}])
    assert out["results"][0] == expected


@needs_node
def test_checksig_accepts_only_the_matching_address(harness):
    out = run_gs(
        harness,
        [
            {"kind": "checkSig", "email": REF_EMAIL, "sig": REF_SIG},
            {"kind": "checkSig", "email": "ME@Example.com ", "sig": REF_SIG},
            {"kind": "checkSig", "email": "other@example.com", "sig": REF_SIG},
            {"kind": "checkSig", "email": REF_EMAIL, "sig": REF_SIG[:-1] + "0"},
            {"kind": "checkSig", "email": REF_EMAIL, "sig": REF_SIG[:16]},
            {"kind": "checkSig", "email": REF_EMAIL, "sig": ""},
        ],
    )
    assert out["results"] == [True, True, False, False, False, False]


@needs_node
def test_checksig_fails_closed_when_the_script_property_is_unset(harness):
    out = run_gs(
        harness,
        [{"kind": "checkSig", "email": REF_EMAIL, "sig": REF_SIG}],
        token="",
    )
    assert out["results"][0] is False


@needs_node
def test_prefs_returns_states_and_digest_for_a_signed_address(harness):
    out = run_gs(
        harness,
        [{"kind": "get", "params": {
            "action": "prefs", "e": REF_EMAIL, "s": REF_SIG}}],
        rows=[_row(REF_EMAIL, "CA,NY,US")],
    )
    assert out["results"][0] == {
        "ok": True,
        "email": REF_EMAIL,
        "states": ["CA", "NY"],
        "digest": True,
        "found": True,
    }


@needs_node
def test_prefs_reads_a_blank_cell_as_california(harness):
    """Mirrors warn_subscribers._parse_prefs so the page shows the truth."""
    out = run_gs(
        harness,
        [{"kind": "get", "params": {
            "action": "prefs", "e": REF_EMAIL, "s": REF_SIG}}],
        rows=[_row(REF_EMAIL, "")],
    )
    got = out["results"][0]
    assert got["found"] is True
    states, digest = warn_subscribers._parse_prefs("")
    assert got["states"] == states and got["digest"] == digest


@needs_node
def test_prefs_reports_unknown_but_correctly_signed_address(harness):
    out = run_gs(
        harness,
        [{"kind": "get", "params": {
            "action": "prefs", "e": REF_EMAIL, "s": REF_SIG}}],
        rows=[_row("someone@example.com", "CA")],
    )
    assert out["results"][0] == {
        "ok": True,
        "email": REF_EMAIL,
        "states": [],
        "digest": False,
        "found": False,
    }


@needs_node
def test_prefs_rejects_bad_signatures_without_leaking_existence(harness):
    """The reply for a subscriber and a stranger must be indistinguishable."""
    calls = [
        {"kind": "get", "params": {
            "action": "prefs", "e": REF_EMAIL, "s": "nope"}},
        {"kind": "get", "params": {"action": "prefs", "e": REF_EMAIL}},
        {"kind": "get", "params": {
            "action": "prefs", "e": "stranger@example.com", "s": REF_SIG}},
    ]
    out = run_gs(harness, calls, rows=[_row(REF_EMAIL, "CA")])
    assert out["results"] == [{"ok": False, "error": "forbidden"}] * 3


@needs_node
def test_full_unsubscribe_deletes_the_row_and_spares_the_neighbours(harness):
    rows = [
        _row("above@example.com", "CA"),
        _row(REF_EMAIL, "CA,NY,US"),
        _row("below@example.com", "TX"),
    ]
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
            "states": [], "digest": False}}],
        rows=rows,
    )
    assert out["results"][0] == {"ok": True, "removed": True, "found": True}
    emails = [r[2] for r in out["rows"][1:]]
    assert emails == ["above@example.com", "below@example.com"]
    # Deleted, never blanked.
    assert [m["op"] for m in mutations(out)] == ["deleteRow"]


@needs_node
def test_full_unsubscribe_of_a_legacy_blank_cell_row_deletes_it(harness):
    """The row most at risk: blanking an already-blank cell is a no-op that
    would leave them subscribed to California forever."""
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
            "states": [], "digest": False}}],
        rows=[_row(REF_EMAIL, "")],
    )
    assert out["results"][0]["removed"] is True
    assert out["rows"][1:] == []
    assert [m["op"] for m in mutations(out)] == ["deleteRow"]


@needs_node
def test_partial_unsubscribe_rewrites_the_cell(harness):
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": "  ME@Example.com ", "s": REF_SIG,
            "states": ["ny"], "digest": True}}],
        rows=[_row(REF_EMAIL, "CA,NY,TX,US")],
    )
    assert out["results"][0] == {
        "ok": True, "removed": False, "found": True, "states": "NY,US",
    }
    assert out["rows"][1][4] == "NY,US"
    assert [m["op"] for m in mutations(out)] == ["setValue"]
    # And the pipeline reads back exactly what the visitor chose.
    states, digest = warn_subscribers._parse_prefs(out["rows"][1][4])
    assert states == ["NY"] and digest is True


@needs_node
def test_digest_only_selection_is_kept_not_deleted(harness):
    """states:[] with digest:true is NOT an empty selection."""
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
            "states": [], "digest": True}}],
        rows=[_row(REF_EMAIL, "CA,US")],
    )
    assert out["results"][0]["removed"] is False
    assert out["rows"][1][4] == "US"
    states, digest = warn_subscribers._parse_prefs("US")
    assert states == [] and digest is True


@needs_node
def test_dropping_the_digest_keeps_the_state_alerts(harness):
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
            "states": ["CA"], "digest": False}}],
        rows=[_row(REF_EMAIL, "CA,US")],
    )
    assert out["rows"][1][4] == "CA"
    states, digest = warn_subscribers._parse_prefs("CA")
    assert states == ["CA"] and digest is False


@needs_node
def test_unsubscribe_requires_a_valid_signature_and_mutates_nothing(harness):
    calls = [
        {"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": "x" * 32,
            "states": [], "digest": False}},
        {"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL,
            "states": [], "digest": False}},
        {"kind": "post", "body": {
            "action": "unsubscribe", "e": "victim@example.com", "s": REF_SIG,
            "states": [], "digest": False}},
    ]
    rows = [_row(REF_EMAIL, "CA"), _row("victim@example.com", "NY")]
    out = run_gs(harness, calls, rows=rows)
    assert out["results"] == [{"ok": False, "error": "forbidden"}] * 3
    assert mutations(out) == []
    assert [r[2] for r in out["rows"][1:]] == [REF_EMAIL, "victim@example.com"]


@needs_node
def test_unsubscribe_is_idempotent(harness):
    body = {
        "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
        "states": [], "digest": False,
    }
    out = run_gs(
        harness,
        [{"kind": "post", "body": body}, {"kind": "post", "body": body}],
        rows=[_row(REF_EMAIL, "CA")],
    )
    assert out["results"][0] == {"ok": True, "removed": True, "found": True}
    assert out["results"][1] == {"ok": True, "removed": True, "found": False}
    assert len([m for m in mutations(out) if m["op"] == "deleteRow"]) == 1


@needs_node
def test_mutation_runs_inside_the_lock(harness):
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "action": "unsubscribe", "e": REF_EMAIL, "s": REF_SIG,
            "states": [], "digest": False}}],
        rows=[_row(REF_EMAIL, "CA")],
    )
    ops = [e["op"] for e in out["events"]]
    for op in ("waitLock", "deleteRow", "releaseLock"):
        assert op in ops, f"{op} never happened: {ops}"
    assert ops.index("waitLock") < ops.index("deleteRow")
    assert ops.index("deleteRow") < ops.index("releaseLock")


@needs_node
def test_signup_and_counter_endpoints_still_work(harness):
    """Regression guard: the additions must not disturb what shipped."""
    calls = [
        {"kind": "post", "body": {
            "name": "New", "email": "New@Example.com",
            "source": "dashboard", "states": "CA,US"}},
        {"kind": "post", "body": {
            "name": "Bot", "email": "bot@example.com", "company": "spam"}},
        {"kind": "post", "body": {"email": "nope"}},
        {"kind": "get", "params": {}},
        {"kind": "get", "params": {"token": REF_TOKEN}},
        {"kind": "get", "params": {"token": "wrong"}},
    ]
    out = run_gs(harness, calls, rows=[_row(REF_EMAIL, "CA")])
    signup, honeypot, bad, public, listed, forbidden = out["results"]
    assert signup == {"ok": True}
    assert honeypot == {"ok": True}
    assert bad == {"ok": False, "error": "invalid_email"}
    # One pre-existing row plus the signup above; the honeypot added nothing.
    assert public == {"count": 2}
    assert forbidden == {"ok": False, "error": "forbidden"}
    assert listed["ok"] is True
    assert [s["email"] for s in listed["subscribers"]] == [
        REF_EMAIL,
        "new@example.com",
    ]
    assert [s["states"] for s in listed["subscribers"]] == ["CA", "CA,US"]
    # The new signup landed, lowercased, with its preferences.
    assert out["rows"][-1][2] == "new@example.com"
    assert out["rows"][-1][4] == "CA,US"
    # The honeypot row was dropped silently.
    assert "bot@example.com" not in [r[2] for r in out["rows"]]


@needs_node
def test_resubscribing_updates_preferences_in_place(harness):
    """Updates the existing row rather than duplicating the address.

    This used to assert the cell became "TX" — i.e. that re-subscribing
    REPLACED the stored selection. That was the bug: neither signup form loads
    the subscriber's current preferences, so neither can show what a replace
    would destroy. Signup adds; only the preferences page removes.
    """
    out = run_gs(
        harness,
        [{"kind": "post", "body": {
            "name": "Me", "email": REF_EMAIL, "states": "TX"}}],
        rows=[_row(REF_EMAIL, "CA")],
    )
    assert out["results"][0] == {
        "ok": True, "duplicate": True, "updated": True, "states": "CA,TX",
    }
    assert len(out["rows"]) == 2
    assert out["rows"][1][4] == "CA,TX"


@needs_node
def test_end_to_end_link_from_python_through_the_script(harness):
    """Mint a link the way warn_notify would, then spend it on the script."""
    import os
    from urllib.parse import urlparse, parse_qs

    os.environ["SUBSCRIBERS_TOKEN"] = REF_TOKEN
    try:
        url = warn_subscribers.unsubscribe_url("Someone@Example.COM")
    finally:
        os.environ.pop("SUBSCRIBERS_TOKEN", None)
    query = parse_qs(urlparse(url).query)
    email, sig = query["e"][0], query["s"][0]

    out = run_gs(
        harness,
        [
            {"kind": "get", "params": {
                "action": "prefs", "e": email, "s": sig}},
            {"kind": "post", "body": {
                "action": "unsubscribe", "e": email, "s": sig,
                "states": [], "digest": False}},
        ],
        rows=[_row("someone@example.com", "CA,US")],
    )
    prefs, unsub = out["results"]
    assert prefs["found"] is True
    assert prefs["states"] == ["CA"] and prefs["digest"] is True
    assert unsub == {"ok": True, "removed": True, "found": True}
    assert out["rows"][1:] == []


# ---------------------------------------------------------------------------
# Signup is additive — a form that cannot show your preferences must not
# destroy them
# ---------------------------------------------------------------------------


def _signup(email, states=None, source="dashboard", name="Sam"):
    body = {"name": name, "email": email, "source": source}
    if states is not None:
        body["states"] = states
    return {"kind": "post", "body": body}


def _cell(out, row=1):
    """The states cell of the Nth subscriber row (1-based, past the header)."""
    return out["rows"][row][4]


def test_signup_path_never_replaces_the_preference_cell():
    """Static guard on the regression.

    The duplicate branch used to `setValue(states)` — the raw submitted value.
    Whatever it writes now must be a merge of the stored cell with the payload.
    """
    body = _body(CODE, "doPost")
    dup = body[body.index("duplicate") - 900:body.index("duplicate") + 200]
    assert "_mergeStates" in dup, "the duplicate branch must merge, not replace"
    assert not re.search(r"setValue\(\s*states\s*\)", body), (
        "writing the submitted states verbatim discards everything the "
        "subscriber already had"
    )


@needs_node
def test_new_subscriber_is_stored_with_what_they_picked(harness):
    out = run_gs(harness, [_signup("new@example.com", "IL,NY")])
    assert out["results"][0] == {"ok": True}
    assert _cell(out) == "IL,NY"


@needs_node
def test_returning_subscriber_gains_a_state_and_keeps_the_rest(harness):
    """The reported bug: IL+NY on the US dashboard, then California."""
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", "IL,NY"]]
    out = run_gs(harness, [_signup("sam@example.com", "CA")], rows=rows)
    assert out["results"][0] == {
        "ok": True, "duplicate": True, "updated": True, "states": "IL,NY,CA",
    }
    assert _cell(out) == "IL,NY,CA"


@needs_node
def test_resubscribing_to_what_you_already_have_writes_nothing(harness):
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", "CA,NY"]]
    out = run_gs(harness, [_signup("sam@example.com", "NY")], rows=rows)
    assert out["results"][0]["updated"] is False
    assert out["results"][0]["states"] == "CA,NY"
    assert not [e for e in mutations(out) if e["op"] == "setValue"]


@needs_node
def test_a_legacy_blank_cell_keeps_its_implicit_california(harness):
    """A blank cell means California (DEFAULT_STATES). Merging onto the raw
    blank would silently drop it and leave the subscriber with only the new
    state — the same data loss in a different disguise."""
    rows = [["t", "Old", "old@example.com", "dashboard", ""]]
    out = run_gs(harness, [_signup("old@example.com", "NY")], rows=rows)
    assert out["results"][0]["states"] == "CA,NY"
    assert _cell(out) == "CA,NY"


@needs_node
def test_the_california_form_payload_preserves_other_states(harness):
    """End-to-end shape of the two-click path the restructure made plausible:
    pick IL+NY at /warn/, click through to /warn/ca/, subscribe again."""
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", "IL,NY"]]
    out = run_gs(harness, [_signup("sam@example.com", "CA", source="dashboard")],
                 rows=rows)
    assert _cell(out) == "IL,NY,CA"


@needs_node
def test_the_digest_sentinel_merges_like_any_other_code(harness):
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", "CA"]]
    out = run_gs(harness, [_signup("sam@example.com", "US")], rows=rows)
    assert _cell(out) == "CA,US"


@needs_node
def test_signup_without_a_states_field_still_means_california(harness):
    """DEFAULT_STATES fallback, for any older cached page still posting it."""
    out = run_gs(harness, [_signup("nostates@example.com")])
    assert _cell(out) == "CA"


@needs_node
@pytest.mark.parametrize("stored,submitted", [
    ("IL,NY", "CA"), ("CA", "NY"), ("CA,NY,US", "TX"), ("", "NY"), ("US", "CA"),
])
def test_no_signup_ever_shrinks_a_subscription(harness, stored, submitted):
    """The invariant, over every shape: whatever a subscriber had before a
    signup, they still have after it."""
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", stored]]
    out = run_gs(harness, [_signup("sam@example.com", submitted)], rows=rows)
    before = set((stored or "CA").split(","))
    after = set(_cell(out).split(","))
    assert before <= after, f"{sorted(before - after)} was dropped"
    assert submitted in after


@needs_node
def test_signup_still_matches_addresses_case_insensitively(harness):
    rows = [["t", "Sam", "sam@example.com", "us-dashboard", "IL"]]
    out = run_gs(harness, [_signup("SAM@Example.COM", "CA")], rows=rows)
    assert len(out["rows"]) == 2, "must update the existing row, not duplicate"
    assert _cell(out) == "IL,CA"


@needs_node
def test_signup_adds_then_the_preferences_page_removes(harness):
    """The round trip that makes additive signup safe.

    Signup can only ever grow a subscription, so something must be able to
    shrink one. That is the preferences page: it loads the current selection
    over `?action=prefs`, shows it, and writes back exactly what the subscriber
    confirmed. Destructive power lives on the one surface where the
    consequences are visible.
    """
    import os

    os.environ["SUBSCRIBERS_TOKEN"] = REF_TOKEN
    sig = warn_subscribers.unsubscribe_signature(REF_EMAIL)
    out = run_gs(
        harness,
        [
            # Subscriber already has IL,NY — they sign up on the CA page.
            {"kind": "post", "body": {"email": REF_EMAIL, "states": "CA",
                                      "source": "dashboard"}},
            # Later, from an alert email's link, they keep only NY.
            {"kind": "post", "body": {"action": "unsubscribe", "e": REF_EMAIL,
                                      "s": sig, "states": ["NY"],
                                      "digest": False}},
        ],
        rows=[_row(REF_EMAIL, "IL,NY")],
    )
    assert out["results"][0]["states"] == "IL,NY,CA"   # signup added
    assert out["results"][1]["states"] == "NY"         # prefs page narrowed
    assert out["rows"][1][4] == "NY"
