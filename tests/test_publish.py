import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import warn_publish


@patch("warn_publish.build_unsubscribe_page")
@patch("warn_publish.warn_notify.load_subscriber_records", return_value=[])
@patch("warn_publish.maybe_send_monthly_digest")
@patch("warn_publish.git_commit_push")
@patch("warn_publish.build_site")
# Both site builders are mocked: build_us_site now writes the site ROOT
# (docs/index.html, docs/data.json, docs/search_index.json, docs/pages/) and
# build_legacy_us_redirect writes docs/us/. Unmocked, this test rewrites the
# real published site every time the suite runs.
@patch("warn_publish.warn_site_us.build_legacy_us_redirect")
@patch("warn_publish.warn_site_us.build_us_site")
@patch("warn_publish.warn_charts.run")
@patch("warn_publish.warn_aggregate.build_national")
@patch("warn_publish.warn_history.run")
@patch("warn_publish.warn_diff.generate_report")
@patch("warn_publish.warn_sources.run_all")
def test_run_full_pipeline(
    mock_sources, mock_diff, mock_history, mock_national, mock_charts,
    mock_us_site, mock_redirect, mock_site, mock_push, mock_digest,
    mock_subs, mock_unsub, tmp_path
):
    """run() orchestrates every stage and honours no_push — without touching the
    real data/ directory, the network, or git.

    Every stage that does real I/O (state-source downloads, diff report,
    historical PDF fetch, national aggregation, charts, site build, git push)
    is mocked, and DATA_DIR is redirected to a tmp dir so the manifest read
    after the chart step hits a seeded file rather than data/charts_manifest.json.
    """
    mock_sources.return_value = {
        "ca": {"state": "CA", "file_changed": False,
               "diff": {"new_count": 0}, "summary": {}}
    }
    mock_charts.return_value = {}

    # run() reads charts_manifest.json after the (mocked) chart step.
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )

    with patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True)

    # Every stage ran, and push was skipped (no_push=True).
    assert mock_sources.called
    assert mock_diff.called
    assert mock_history.called
    assert mock_national.called
    assert mock_charts.called
    assert mock_site.called
    assert not mock_push.called

    # The root of the published site is the national dashboard, and the old
    # /us/ address keeps a stub so already-mailed links still land.
    assert mock_us_site.called
    assert mock_us_site.call_args.kwargs["out_dir"] == warn_publish.OUTPUT_DIR
    assert mock_redirect.called

    # The unsubscribe page is rebuilt every run — the links already mailed out
    # have to keep landing somewhere live.
    assert mock_unsub.called

    # The subscriber list is fetched exactly once per run and the digest step
    # runs (as a ledger no-op) on every run.
    assert mock_subs.call_count == 1
    assert mock_digest.called

    # The CA result doubles as the headline monitor_result passed to build_site,
    # now annotated with the per-state status map.
    monitor_result = mock_site.call_args[0][1]
    assert monitor_result["states"]["ca"]["state"] == "CA"


def test_format_number():
    assert warn_publish._format_number(1234) == "1,234"
    assert warn_publish._format_number("invalid") == "invalid"


def test_strip_county():
    assert warn_publish._strip_county("Los Angeles County") == "Los Angeles"
    assert warn_publish._strip_county("Orleans Parish") == "Orleans"
    assert warn_publish._strip_county(None) == ""


def test_fmt_human_date():
    # Must match the client JS fmtDate ('Jul 4, 2026' — no leading zero on day).
    assert warn_publish._fmt_human_date("2026-01-01") == "Jan 1, 2026"
    assert warn_publish._fmt_human_date("2026-07-04") == "Jul 4, 2026"
    assert warn_publish._fmt_human_date("2025-12-31") == "Dec 31, 2025"
    assert warn_publish._fmt_human_date("garbage") == "garbage"


# Records spanning two calendar years, used to exercise the KPI date window.
_KPI_RECORDS = [
    {"company": "Alpha", "county": "Los Angeles County", "employees": 100,
     "notice_date": "2026-02-01", "effective_date": "2026-04-02"},  # lead 60d
    {"company": "Beta", "county": "Los Angeles County", "employees": 250,
     "notice_date": "2026-03-01", "effective_date": "2026-05-30"},  # lead 90d
    {"company": "Gamma", "county": "Orange County", "employees": 400,
     "notice_date": "2025-06-01", "effective_date": "2025-07-01"},  # prior year
]


def test_compute_kpis_current_year_window():
    """A notice-date window restricts every metric to that period and strips
    the county suffix so it matches the notices table."""
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2026-01-01", "2026-12-31")
    assert k["count"] == 2
    assert k["employees_total"] == 350
    assert k["largest_company"] == "Beta"
    assert k["largest_employees"] == "250"
    assert k["top_county"] == "Los Angeles"          # suffix stripped
    assert k["top_county_employees"] == "350"
    assert k["avg_lead_days"] == "75d"               # mean of 60 and 90


def test_compute_kpis_no_window_counts_everything():
    k = warn_publish._compute_kpis(_KPI_RECORDS)
    assert k["count"] == 3
    assert k["employees_total"] == 750
    assert k["largest_company"] == "Gamma"           # 400 is the largest overall
    assert k["top_county"] == "Orange"               # Orange 400 > LA 350


def test_compute_kpis_prior_year_window():
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2025-01-01", "2025-12-31")
    assert k["count"] == 1
    assert k["employees_total"] == 400
    assert k["top_county"] == "Orange"


def test_compute_kpis_empty_window_returns_defaults():
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2030-01-01", "2030-12-31")
    assert k["count"] == 0
    assert k["employees_total"] == 0
    assert k["top_county"] == "N/A"
    assert k["largest_employees"] == "N/A"          # not "0" — matches client
    assert k["top_county_employees"] == "N/A"
    assert k["avg_lead_days"] == "N/A"


def test_compute_kpis_avg_lead_rounds_half_up():
    """Mean lead of exactly x.5 rounds up (int(x + 0.5)) to match the client's
    JS Math.round — Python's banker's round() would give 58 here."""
    recs = [
        {"company": "A", "county": "X", "employees": 10,
         "notice_date": "2026-01-01", "effective_date": "2026-02-28"},  # 58 days
        {"company": "B", "county": "X", "employees": 10,
         "notice_date": "2026-01-01", "effective_date": "2026-03-01"},  # 59 days
    ]
    assert warn_publish._compute_kpis(recs)["avg_lead_days"] == "59d"    # mean 58.5


def test_compute_kpis_largest_tiebreak_is_order_independent():
    """On an employee tie the latest notice wins, regardless of input order, so
    the server matches the client (which iterates the newest-first table)."""
    recs = [
        {"company": "Zeta Corp", "county": "X County", "employees": 500,
         "notice_date": "2026-02-01", "effective_date": "2026-03-01"},
        {"company": "Alpha Corp", "county": "X County", "employees": 500,
         "notice_date": "2026-05-01", "effective_date": "2026-06-01"},  # later
    ]
    assert warn_publish._compute_kpis(recs)["largest_company"] == "Alpha Corp"
    assert warn_publish._compute_kpis(
        list(reversed(recs))
    )["largest_company"] == "Alpha Corp"


def test_compute_kpis_top_county_tiebreak_is_order_independent():
    recs = [
        {"company": "A", "county": "Alameda County", "employees": 100,
         "notice_date": "2026-01-01", "effective_date": "2026-02-01"},
        {"company": "B", "county": "Butte County", "employees": 100,
         "notice_date": "2026-01-02", "effective_date": "2026-02-01"},
    ]
    assert warn_publish._compute_kpis(recs)["top_county"] == "Butte"
    assert warn_publish._compute_kpis(list(reversed(recs)))["top_county"] == "Butte"


# ---------------------------------------------------------------------------
# Per-state alert routing through the pipeline
# ---------------------------------------------------------------------------


class _FakeSource:
    """Stands in for a warn_sources.Source in the notify loop."""

    def __init__(self, code):
        self.code = code
        self.alerted = []

    def record_alerted(self, diff):
        self.alerted.append(diff)


def _run_notify_loop(state_results, sources, send_ok=True, tmp_path=None):
    """Drive run()'s notify loop with everything else mocked out.

    Returns the mocked notify_if_changes so callers can inspect routing.
    """
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    records = [{"email": "a@x.com", "name": "", "states": ["CA"], "digest": True}]
    with patch("warn_publish.warn_sources.run_all", return_value=state_results), \
         patch("warn_publish.warn_sources.all_sources", return_value=sources), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site"), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.build_unsubscribe_page"), \
         patch("warn_publish.git_commit_push"), \
         patch("warn_publish.maybe_send_monthly_digest"), \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=records) as mock_load, \
         patch("warn_publish.warn_notify.notify_if_changes",
               return_value=send_ok) as mock_notify, \
         patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True)
    return mock_notify, mock_load, records


def test_run_passes_state_code_and_shared_records_to_notifier(tmp_path):
    """Each state's alert is tagged with its own code and reuses one fetch."""
    state_results = {
        "ca": {"state": "CA", "diff": {"new_count": 2}, "summary": {"a": 1}},
        "il": {"state": "IL", "diff": {"new_count": 1}, "summary": {"b": 2}},
        "ny": {"state": "NY", "diff": {"new_count": 0, "amendment_count": 0},
               "summary": {}},
    }
    sources = [_FakeSource("ca"), _FakeSource("il"), _FakeSource("ny")]
    mock_notify, mock_load, records = _run_notify_loop(
        state_results, sources, tmp_path=tmp_path
    )

    # NY had no changes, so only CA and IL were notified.
    states = [c.kwargs["state"] for c in mock_notify.call_args_list]
    assert states == ["CA", "IL"]
    # One subscriber fetch for the whole run, threaded into every send.
    assert mock_load.call_count == 1
    for call in mock_notify.call_args_list:
        assert call.kwargs["records"] is records
    # Sends succeeded, so each state's alert ledger was recorded.
    assert sources[0].alerted and sources[1].alerted
    assert not sources[2].alerted


def test_run_skips_ledger_when_a_state_send_fails(tmp_path):
    state_results = {"ca": {"state": "CA", "diff": {"new_count": 2},
                            "summary": {}}}
    sources = [_FakeSource("ca")]
    _run_notify_loop(state_results, sources, send_ok=False, tmp_path=tmp_path)
    assert not sources[0].alerted


# ---------------------------------------------------------------------------
# Monthly digest ledger
# ---------------------------------------------------------------------------


@pytest.fixture
def digest_dir(tmp_path, monkeypatch):
    """Redirect the digest ledger at data/digest_sent.json into a tmp dir."""
    monkeypatch.setattr(warn_publish, "DATA_DIR", tmp_path)
    return tmp_path


def test_previous_month_is_the_just_completed_one():
    assert warn_publish._previous_month(date(2026, 7, 29)) == "2026-06"
    assert warn_publish._previous_month(date(2026, 7, 1)) == "2026-06"
    # Year boundary.
    assert warn_publish._previous_month(date(2026, 1, 3)) == "2025-12"


def test_digest_sends_once_per_month_and_again_the_next(digest_dir):
    """The pipeline runs twice daily; the ledger makes the digest monthly."""
    payload = {"subject": "s", "html": "<p>h</p>", "text": "t"}
    with patch("warn_publish._build_digest_payload", return_value=payload), \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=True) as mock_send:
        # First run of July delivers June's digest …
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is True
        # … and every later run that month is a no-op.
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is False
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is False
        assert mock_send.call_count == 1

        # A new month is a new period, so it goes out again.
        assert warn_publish.maybe_send_monthly_digest(period="2026-07") is True
        assert mock_send.call_count == 2

    ledger = json.loads((digest_dir / "digest_sent.json").read_text())
    assert ledger["sent"] == ["2026-06", "2026-07"]
    assert ledger["last_sent"] == "2026-07"


def test_failed_digest_send_leaves_ledger_untouched(digest_dir):
    """A failed send must retry next run, not be silently swallowed."""
    payload = {"subject": "s", "html": "<p>h</p>", "text": "t"}
    with patch("warn_publish._build_digest_payload", return_value=payload), \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=False):
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is False

    assert not (digest_dir / "digest_sent.json").exists()
    assert warn_publish._digest_already_sent("2026-06") is False

    # Next run succeeds and the period is finally recorded.
    with patch("warn_publish._build_digest_payload", return_value=payload), \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=True):
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is True
    assert warn_publish._digest_already_sent("2026-06") is True


def test_force_digest_resends_an_already_sent_month(digest_dir):
    """--digest is the manual-test escape hatch and ignores the ledger."""
    payload = {"subject": "s", "html": "<p>h</p>", "text": "t"}
    warn_publish._record_digest_sent("2026-06")
    with patch("warn_publish._build_digest_payload", return_value=payload), \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=True) as mock_send:
        assert warn_publish.maybe_send_monthly_digest(
            period="2026-06", force=True
        ) is True
    assert mock_send.called
    ledger = json.loads((digest_dir / "digest_sent.json").read_text())
    assert ledger["sent"] == ["2026-06"]          # recorded once, not duplicated


def test_empty_digest_payload_is_not_sent_or_recorded(digest_dir):
    with patch("warn_publish._build_digest_payload", return_value=None), \
         patch("warn_publish.warn_notify.send_monthly_digest") as mock_send:
        assert warn_publish.maybe_send_monthly_digest(period="2026-06") is False
    assert not mock_send.called
    assert warn_publish._digest_already_sent("2026-06") is False


def test_corrupt_digest_ledger_does_not_block_a_send(digest_dir):
    (digest_dir / "digest_sent.json").write_text("{not json")
    assert warn_publish._digest_already_sent("2026-06") is False
    warn_publish._record_digest_sent("2026-06")
    assert warn_publish._digest_already_sent("2026-06") is True


def test_digest_defaults_to_the_previous_month(digest_dir):
    payload = {"subject": "s", "html": "<p>h</p>", "text": "t"}
    with patch("warn_publish._build_digest_payload",
               return_value=payload) as mock_build, \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=True):
        warn_publish.maybe_send_monthly_digest()
    assert mock_build.call_args[0][0] == warn_publish._previous_month()


def test_digest_failure_is_non_fatal_to_the_run(tmp_path):
    """warn_digest blowing up must not fail a run that produced good data."""
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    with patch("warn_publish.warn_sources.run_all", return_value={}), \
         patch("warn_publish.warn_sources.all_sources", return_value=[]), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site"), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.build_unsubscribe_page"), \
         patch("warn_publish.git_commit_push"), \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=[]), \
         patch("warn_publish.maybe_send_monthly_digest",
               side_effect=ImportError("no warn_digest")), \
         patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True)      # must not raise


def test_no_digest_flag_skips_the_step(tmp_path):
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    with patch("warn_publish.warn_sources.run_all", return_value={}), \
         patch("warn_publish.warn_sources.all_sources", return_value=[]), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site"), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.build_unsubscribe_page"), \
         patch("warn_publish.git_commit_push"), \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=[]), \
         patch("warn_publish.maybe_send_monthly_digest") as mock_digest, \
         patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True, send_digest=False)
    assert not mock_digest.called


def test_build_digest_payload_passes_year_and_month(monkeypatch):
    """The seam splits a YYYY-MM period into the digest's year/month keywords."""
    import sys
    import types

    calls = []
    mod = types.ModuleType("warn_digest")

    def build(year, month, national_file=None):
        calls.append((year, month))
        return {"subject": "s"}

    mod.build_monthly_digest = build
    monkeypatch.setitem(sys.modules, "warn_digest", mod)

    assert warn_publish._build_digest_payload("2026-06") == {"subject": "s"}
    assert calls == [(2026, 6)]


def test_build_digest_payload_matches_the_real_digest_signature():
    """Guards the seam against drift in warn_digest.build_monthly_digest."""
    import inspect

    import warn_digest

    params = inspect.signature(warn_digest.build_monthly_digest).parameters
    assert "year" in params and "month" in params


# ---------------------------------------------------------------------------
# Unsubscribe page
# ---------------------------------------------------------------------------


def test_build_unsubscribe_page_delegates_to_warn_unsubscribe(monkeypatch):
    """The seam calls warn_unsubscribe.build_unsubscribe_page() and nothing else."""
    import sys
    import types

    calls = []
    mod = types.ModuleType("warn_unsubscribe")
    mod.build_unsubscribe_page = lambda: calls.append(True)
    monkeypatch.setitem(sys.modules, "warn_unsubscribe", mod)

    warn_publish.build_unsubscribe_page()
    assert calls == [True]


def _run_with_unsubscribe(tmp_path, **kw):
    """Drive run() with every stage mocked except the unsubscribe-page step."""
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    with patch("warn_publish.warn_sources.run_all", return_value={}), \
         patch("warn_publish.warn_sources.all_sources", return_value=[]), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site"), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.git_commit_push") as mock_push, \
         patch("warn_publish.maybe_send_monthly_digest") as mock_digest, \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=[]), \
         patch("warn_publish.build_unsubscribe_page", **kw) as mock_unsub, \
         patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True)
    return mock_unsub, mock_digest, mock_push


def test_unsubscribe_page_is_built_every_run(tmp_path):
    mock_unsub, _, _ = _run_with_unsubscribe(tmp_path)
    assert mock_unsub.call_count == 1


def test_unsubscribe_page_failure_is_non_fatal(tmp_path):
    """A missing/broken warn_unsubscribe must not fail a run with good data."""
    mock_unsub, mock_digest, _ = _run_with_unsubscribe(
        tmp_path, side_effect=ImportError("no warn_unsubscribe")
    )
    assert mock_unsub.called
    # The run carried on past the failure — the digest step still ran.
    assert mock_digest.called


def _staged_paths(monkeypatch) -> list:
    """Run git_commit_push with git stubbed out; return the `git add` argv."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        # Empty `git status --porcelain` → "nothing to commit", so the stub
        # never has to fake a commit or a push.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(warn_publish.subprocess, "run", fake_run)
    assert warn_publish.git_commit_push() is True
    add_call = next(c for c in calls if c[:2] == ["git", "add"])
    return add_call


def test_git_add_stages_the_unsubscribe_page(tmp_path, monkeypatch):
    page = tmp_path / "unsubscribe.html"
    page.write_text("<html></html>")
    monkeypatch.setattr(warn_publish, "UNSUBSCRIBE_HTML", page)
    assert "docs/unsubscribe.html" in _staged_paths(monkeypatch)


def test_git_add_omits_the_unsubscribe_page_when_it_was_not_built(
    tmp_path, monkeypatch
):
    """`git add` fails wholesale on a pathspec matching nothing — so a page
    that was never built must not be named, or nothing else gets staged."""
    monkeypatch.setattr(warn_publish, "UNSUBSCRIBE_HTML", tmp_path / "absent.html")
    staged = _staged_paths(monkeypatch)
    assert "docs/unsubscribe.html" not in staged
    assert "docs/" in staged            # the rest is still staged as before


# ---------------------------------------------------------------------------
# Ledger persistence on a root-site build failure
# ---------------------------------------------------------------------------
#
# run() deliberately sends notifications and the digest BEFORE raising on a
# root-build failure. The ledgers recording those sends are written locally —
# so the failure path must commit data/ (and only data/) or a fresh CI
# workspace discards them and every subscriber is re-emailed next run.


class _GitRecorder:
    """Stands in for subprocess.run, recording every argv.

    ``git diff --cached --quiet`` reports staged changes (exit 1) by default so
    the ledger-commit path runs end to end; ``staged_changes=False`` gives the
    nothing-to-commit case. Everything else succeeds with empty output.
    """

    def __init__(self, staged_changes=True):
        self.calls = []
        self.staged_changes = staged_changes

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if list(args[:3]) == ["git", "diff", "--cached"]:
            rc = 1 if self.staged_changes else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _run_with_root_failure(tmp_path, no_push):
    """Drive run() with build_us_site raising and every other stage mocked.

    Asserts the RuntimeError propagates (the run must still exit non-zero so
    the last good page stays published). Git is NOT mocked here — stub
    subprocess.run before calling if the test lets the ledger path run.
    """
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    with patch("warn_publish.warn_sources.run_all", return_value={}), \
         patch("warn_publish.warn_sources.all_sources", return_value=[]), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site",
               side_effect=ValueError("chart hiccup")), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.build_unsubscribe_page"), \
         patch("warn_publish.maybe_send_monthly_digest") as mock_digest, \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=[]), \
         patch("warn_publish.DATA_DIR", tmp_path):
        with pytest.raises(RuntimeError, match="site build failed"):
            warn_publish.run(no_push=no_push)
    return mock_digest


def test_root_failure_commits_ledgers_only_and_still_raises(
    tmp_path, monkeypatch
):
    """Local run, root build fails: data/ is committed and pushed, docs/ is
    never staged, and the RuntimeError still propagates."""
    rec = _GitRecorder()
    monkeypatch.setattr(warn_publish.subprocess, "run", rec)
    mock_digest = _run_with_root_failure(tmp_path, no_push=False)

    # The sends went out before the raise, exactly as before.
    assert mock_digest.called

    # Only data/ was ever staged — docs/ appears in no git argv at all.
    assert ["git", "add", "data/"] in rec.calls
    assert not any("docs/" in arg for call in rec.calls for arg in call)

    # Committed with the distinct ledger message, [skip ci] included, and the
    # pathspec restricting the commit to data/ …
    commit = next(c for c in rec.calls if c[:2] == ["git", "commit"])
    msg = commit[commit.index("-m") + 1]
    assert msg == "auto: alert ledgers (site build failed) [skip ci]"
    assert "[skip ci]" in msg
    assert commit[-2:] == ["--", "data/"]

    # … and pushed.
    assert ["git", "push", "origin", "main"] in rec.calls


def test_root_failure_with_no_push_runs_no_git_at_all(tmp_path, monkeypatch):
    """--no-push means the caller owns git: in CI, monitor.yml's failure
    branch commits data/ itself, so the in-process path must stay silent."""
    rec = _GitRecorder()
    monkeypatch.setattr(warn_publish.subprocess, "run", rec)
    _run_with_root_failure(tmp_path, no_push=True)
    assert rec.calls == []


def test_ledger_commit_failure_never_masks_the_root_raise(tmp_path):
    """A broken git must not turn the deliberate RuntimeError into an OSError
    (the helper asserts the RuntimeError still propagates)."""
    with patch("warn_publish.commit_ledgers",
               side_effect=OSError("no git binary")) as mock_ledgers:
        _run_with_root_failure(tmp_path, no_push=False)
    assert mock_ledgers.called


def test_commit_ledgers_is_a_noop_without_data_changes(monkeypatch):
    rec = _GitRecorder(staged_changes=False)
    monkeypatch.setattr(warn_publish.subprocess, "run", rec)
    assert warn_publish.commit_ledgers() is False
    assert ["git", "add", "data/"] in rec.calls
    assert not any(c[:2] == ["git", "commit"] for c in rec.calls)
    assert not any(c[:2] == ["git", "push"] for c in rec.calls)


def test_commit_ledgers_swallows_git_errors(monkeypatch):
    """Best-effort by contract: it runs just ahead of run()'s raise."""
    def boom(*args, **kwargs):
        raise OSError("git missing")

    monkeypatch.setattr(warn_publish.subprocess, "run", boom)
    assert warn_publish.commit_ledgers() is False       # must not raise


def test_success_path_uses_full_commit_not_the_ledger_path(tmp_path):
    """With a healthy root build and no_push=False, the normal full commit
    runs and the failure-path ledger commit does not."""
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )
    with patch("warn_publish.warn_sources.run_all", return_value={}), \
         patch("warn_publish.warn_sources.all_sources", return_value=[]), \
         patch("warn_publish.warn_diff.generate_report"), \
         patch("warn_publish.warn_history.run"), \
         patch("warn_publish.warn_aggregate.build_national"), \
         patch("warn_publish.warn_charts.run"), \
         patch("warn_publish.warn_site_us.build_us_site"), \
         patch("warn_publish.warn_site_us.build_legacy_us_redirect"), \
         patch("warn_publish.build_site"), \
         patch("warn_publish.build_unsubscribe_page"), \
         patch("warn_publish.maybe_send_monthly_digest"), \
         patch("warn_publish.warn_notify.load_subscriber_records",
               return_value=[]), \
         patch("warn_publish.git_commit_push") as mock_push, \
         patch("warn_publish.commit_ledgers") as mock_ledgers, \
         patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=False)
    assert mock_push.called
    assert not mock_ledgers.called


def test_maybe_send_digest_threads_records_to_the_notifier(digest_dir):
    records = [{"email": "d@x.com", "name": "", "states": [], "digest": True}]
    payload = {"subject": "s", "html": "<p>h</p>", "text": "t"}
    with patch("warn_publish._build_digest_payload", return_value=payload), \
         patch("warn_publish.warn_notify.send_monthly_digest",
               return_value=True) as mock_send:
        warn_publish.maybe_send_monthly_digest(records=records, period="2026-06")
    assert mock_send.call_args.kwargs["records"] is records
