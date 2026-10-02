"""Tests for warn_site_check — is the live site serving the committed build?

From 2026-09-17 to 2026-10-02 the public dashboards froze at 2026-09-16 while
the pipeline kept committing new builds and emailing alerts about them: one
GitHub Pages deploy sat in "waiting" for 15 days and every later deploy queued
behind it. Nothing failed, so nothing noticed. These tests pin the check that
now notices, the repair that clears a stuck deploy without ever interrupting a
live one, and the workflow wiring both depend on.
"""

import gzip
import http.server
import json
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import warn_site_check
import warn_urls


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
BUILD = "2026-10-02T03:24:31.804781+00:00"
OLD_BUILD = "2026-09-16T16:18:19.532152+00:00"


@pytest.fixture
def docs(tmp_path):
    """A docs/ tree in the two shapes the builds really write: compact
    national JSON (stamp first) and indented California JSON."""
    (tmp_path / "ca").mkdir()
    (tmp_path / "data.json").write_text(
        json.dumps({"last_updated": BUILD, "states_live": 47, "records": []}))
    (tmp_path / "ca" / "data.json").write_text(
        json.dumps({"scope": "ca", "state": "CA", "coverage_start": "2025-01-01",
                    "last_updated": BUILD, "records": []}, indent=2))
    return tmp_path


def serving(stamp_by_path):
    """A fake fetch: the live site serving these stamps, keyed by docs path."""
    by_url = {page_url: stamp_by_path[rel] for rel, page_url in warn_site_check.PAGES}

    def fetch(page_url):
        stamp = by_url[page_url]
        if isinstance(stamp, Exception):
            raise stamp
        return json.dumps({"last_updated": stamp, "records": []})

    return fetch


def everywhere(stamp):
    return serving({rel: stamp for rel, _ in warn_site_check.PAGES})


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


# ── check(): is the live site behind? ──────────────────────────────────────


def test_both_dashboards_are_checked():
    """The US root and California are separate builds: a check of one says
    nothing about the other, and both froze in the outage."""
    assert dict(warn_site_check.PAGES) == {
        "data.json": warn_urls.url("/data.json"),
        "ca/data.json": warn_urls.url("/ca/data.json"),
    }


def test_the_outage_is_reported_as_behind(docs):
    """The exact state of 2026-10-02: built Oct 2, live Sep 16."""
    results = warn_site_check.check(docs, everywhere(OLD_BUILD), now=NOW)
    assert [r["ok"] for r in results] == [False, False]
    assert all("15.5 days behind" in r["reason"] for r in results)


def test_in_sync_when_live_serves_the_build(docs):
    assert warn_site_check.in_sync(warn_site_check.check(docs, everywhere(BUILD), now=NOW))


def test_a_newer_live_build_is_in_sync(docs):
    """A checkout a newer deploy has already overtaken is success, not a
    mismatch."""
    newer = "2026-10-02T15:00:00+00:00"
    assert warn_site_check.in_sync(warn_site_check.check(docs, everywhere(newer), now=NOW))


def test_one_stale_dashboard_fails_the_whole_check(docs):
    """Both dashboards must be current. A US page that is current must not
    vouch for a California page that is not."""
    fetch = serving({"data.json": BUILD, "ca/data.json": OLD_BUILD})
    results = warn_site_check.check(docs, fetch, now=NOW)
    assert [r["ok"] for r in results] == [True, False]
    assert not warn_site_check.in_sync(results)


def test_a_fresh_build_gets_grace_while_its_deploy_runs(docs):
    """The watchdog must not call the minutes between a pipeline's commit and
    its own publish job landing an outage."""
    built = datetime.fromisoformat(BUILD)
    young = warn_site_check.check(docs, everywhere(OLD_BUILD),
                                  grace=timedelta(minutes=45), now=built + timedelta(minutes=10))
    assert warn_site_check.in_sync(young)
    old = warn_site_check.check(docs, everywhere(OLD_BUILD),
                                grace=timedelta(minutes=45), now=built + timedelta(minutes=50))
    assert not any(r["ok"] for r in old)


@pytest.mark.parametrize("live", [
    OSError("connection reset"),
    "<html>404 Not Found</html>",
    '{"last_updated": "not a date"}',
])
def test_cannot_confirm_is_not_ok(docs, live):
    """"Can't tell" must redeploy, never assume all is well. Assuming all
    was well is what hid the outage for 15 days."""
    fetch = (serving({rel: live for rel, _ in warn_site_check.PAGES})
             if isinstance(live, Exception)
             else lambda _url: live)
    assert not any(r["ok"] for r in warn_site_check.check(docs, fetch, now=NOW))


def test_missing_build_file_is_not_ok(docs):
    (docs / "ca" / "data.json").unlink()
    results = warn_site_check.check(docs, everywhere(BUILD), now=NOW)
    assert [r["ok"] for r in results] == [True, False]
    assert "cannot read" in results[1]["reason"]


def test_stamp_is_read_from_the_head_only(docs):
    head = warn_site_check.local_head("data.json", docs)
    assert warn_site_check.parse_stamp(head) == datetime.fromisoformat(BUILD)


def test_real_committed_payloads_carry_the_stamp_up_front():
    """The check reads 64 KB of a ~14 MB file. A builder that moves
    ``last_updated`` below the records must fail here, not in production."""
    for rel, _ in warn_site_check.PAGES:
        path = warn_site_check.DOCS_DIR / rel
        if not path.exists() or path.stat().st_size == 0:
            pytest.skip(f"docs/{rel} not built in this checkout")
        assert warn_site_check.parse_stamp(warn_site_check.local_head(rel)), rel


def test_naive_stamps_are_read_as_utc():
    assert warn_site_check.parse_stamp('{"last_updated": "2026-10-02T03:24:31"}') == \
        datetime(2026, 10, 2, 3, 24, 31, tzinfo=timezone.utc)


# ── live_head(): what a browser is served ──────────────────────────────────


@pytest.fixture
def cdn():
    """A local stand-in for the Pages CDN, which caches each Accept-Encoding
    as its own entry. Here the gzip entry (what browsers get) is fresh and the
    identity entry is stale, so a check that forgets gzip reads the wrong copy.
    The body is padded past HEAD_BYTES even when compressed, so the truncated-
    gzip path is the one exercised."""
    seen = {}
    fresh = json.dumps({"scope": "ca", "state": "CA", "coverage_start": "2025-01-01",
                        "last_updated": BUILD,
                        "records": [{"n": i, "pad": f"{i:x}" * 8} for i in range(60000)]},
                       indent=2).encode()
    stale = fresh.replace(BUILD.encode(), OLD_BUILD.encode())

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["accept_encoding"] = self.headers.get("Accept-Encoding", "")
            if "gzip" in seen["accept_encoding"]:
                body = gzip.compress(fresh)
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
            else:
                body = stale
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # The client hangs up after HEAD_BYTES, by design; don't print the
    # resulting BrokenPipe tracebacks.
    server.handle_error = lambda *_args: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert len(gzip.compress(fresh)) > warn_site_check.HEAD_BYTES
    yield f"http://127.0.0.1:{server.server_address[1]}/warn/ca/data.json", seen
    server.shutdown()


def test_live_head_reads_the_copy_browsers_get(cdn):
    page_url, seen = cdn
    head = warn_site_check.live_head(page_url, timeout=10)
    assert "gzip" in seen["accept_encoding"]
    assert len(head) <= warn_site_check.HEAD_BYTES
    assert warn_site_check.parse_stamp(head) == datetime.fromisoformat(BUILD)


# ── wait_for_sync() ────────────────────────────────────────────────────────


def test_wait_polls_until_the_deploy_lands(docs):
    """The CDN can lag the deploy by a poll or two."""
    clock = FakeClock()
    polls = len(warn_site_check.PAGES)
    calls = iter([OLD_BUILD] * (2 * polls) + [BUILD] * polls)
    results = warn_site_check.wait_for_sync(
        600, interval_s=15, docs_dir=docs,
        fetch=lambda _url: json.dumps({"last_updated": next(calls)}),
        sleep=clock.sleep, clock=clock)
    assert warn_site_check.in_sync(results)
    assert clock.t == 30  # two waits, in sync on the third poll


def test_wait_gives_up_at_the_deadline(docs):
    clock = FakeClock()
    results = warn_site_check.wait_for_sync(60, interval_s=15, docs_dir=docs,
                                            fetch=everywhere(OLD_BUILD),
                                            sleep=clock.sleep, clock=clock)
    assert not any(r["ok"] for r in results)
    assert clock.t == 60


def test_a_deploy_wait_outlasts_the_cdn_cache():
    """Pages' CDN serves a copy for up to max-age=600. Deploys have been seen
    to purge it, but that is undocumented, so a wait must not depend on it."""
    assert warn_site_check.DEPLOY_WAIT_S > 600


# ── Which runs are stuck ───────────────────────────────────────────────────


def _run(run_id, status, created_minutes_ago):
    return {"id": run_id, "status": status,
            "created": NOW - timedelta(minutes=created_minutes_ago)}


def _jobs(*step_minutes_ago):
    """A ``gh run view --json jobs`` payload whose steps started this long
    before NOW. No arguments = a job with no steps (never reached a runner)."""
    steps = [{"name": f"s{i}", "startedAt": (NOW - timedelta(minutes=m)).isoformat()}
             for i, m in enumerate(step_minutes_ago)]
    return json.dumps({"jobs": [{"name": "deploy", "startedAt": "2026-09-17T02:43:17Z",
                                 "steps": steps}]})


def _gh_jobs(payload):
    def run_gh(*args):
        assert args[:2] == ("run", "view") and args[-2:] == ("--json", "jobs"), args
        if isinstance(payload, Exception):
            raise payload
        return payload
    return run_gh


@pytest.mark.parametrize("status", ["waiting", "pending"])
def test_a_run_held_before_its_runner_is_stuck_at_any_age(status):
    """"waiting" is the 2026-09-17 run: held at the environment gate for 15
    days. Neither status has reached a runner, so cancelling interrupts
    nothing, and the fresh dispatch replaces it."""
    assert warn_site_check.stuck_deploys([_run(1, status, 0)], _gh_jobs("{}"), now=NOW) == [1]


@pytest.mark.parametrize("status", ["queued", "requested"])
def test_a_run_waiting_for_a_runner_is_spared_while_young(status):
    """A young one may be this invocation's own previous attempt; cancelling
    it would only cost it its place in line."""
    runs = [_run(1, status, 16), _run(2, status, 31)]
    assert warn_site_check.stuck_deploys(runs, _gh_jobs("{}"), now=NOW) == [2]


def test_a_deploy_that_just_started_after_hours_pending_is_spared():
    """Run 36960151090, verbatim: created 03:26, pending ~4 h behind the stuck
    run, deploying from 07:20. Its run-level timestamps say it is 4 h old; its
    steps say seconds. Cancelling deploy-pages mid-flight can wedge Pages for
    every later deploy, so the steps are what count."""
    run = _run(36960151090, "in_progress", 235)
    assert warn_site_check.stuck_deploys([run], _gh_jobs(_jobs(0.2, 0.1)), now=NOW) == []


def test_a_deploy_past_its_job_timeout_is_a_ghost():
    run = _run(1, "in_progress", 240)
    assert warn_site_check.stuck_deploys([run], _gh_jobs(_jobs(31, 30.5)), now=NOW) == [1]


@pytest.mark.parametrize("payload", [
    _jobs(),                                           # no step has started yet
    subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
    "not json",
    json.dumps({"jobs": [{"steps": [{"startedAt": "0001-01-01T00:00:00Z"}]}]}),
])
def test_an_in_progress_run_of_unknown_age_is_spared(payload):
    """A mistaken cancel can wedge Pages; a real ghost will still be a ghost
    on the next pass."""
    run = _run(1, "in_progress", 240)
    assert warn_site_check.stuck_deploys([run], _gh_jobs(payload), now=NOW) == []


# ── publish(): deploy, clear what is stuck, prove it landed ────────────────


class FakeGitHub:
    """The gh CLI as publish() sees it: Deploy Pages runs (status, minutes
    since created, minutes since a runner picked it up), and a site that
    serves the build once a dispatched deploy is allowed to land."""

    def __init__(self, runs=(), deploys_land=True, cancel_works=True, on_cancel=None,
                 live=OLD_BUILD):
        self.runs = {run_id: list(rest) for run_id, *rest in runs}
        self.deploys_land = deploys_land
        self.cancel_works = cancel_works
        self.on_cancel = on_cancel or (lambda gh, run_id: None)
        self.calls = []
        self.live = dict.fromkeys((rel for rel, _ in warn_site_check.PAGES), live)

    def __call__(self, *args):
        self.calls.append(args)
        now = datetime.now(timezone.utc)
        if args[:2] == ("run", "list"):
            return json.dumps([
                {"databaseId": i, "status": status,
                 "createdAt": (now - timedelta(minutes=created)).isoformat()}
                for i, (status, created, *_) in self.runs.items()])
        if args[:2] == ("run", "view"):
            _status, _created, *runner = self.runs[int(args[2])]
            steps = ([{"startedAt": (now - timedelta(minutes=runner[0])).isoformat()}]
                     if runner else [])
            return json.dumps({"jobs": [{"steps": steps}]})
        if args[:2] == ("run", "cancel"):
            if self.cancel_works:
                self.runs[int(args[2])][0] = "completed"
            self.on_cancel(self, int(args[2]))
            return ""
        if args[:3] == ("api", "-X", "POST") and args[3].endswith("/force-cancel"):
            self.runs[int(args[3].split("/")[-2])][0] = "completed"
            return ""
        if args[:2] == ("workflow", "run"):
            if self.deploys_land:
                self.live = dict.fromkeys(self.live, BUILD)
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    def fetch(self, page_url):
        rel = {u: r for r, u in warn_site_check.PAGES}[page_url]
        return json.dumps({"last_updated": self.live[rel]})

    def dispatches(self):
        return [c for c in self.calls if c[:2] == ("workflow", "run")]

    def cancels(self):
        return [c for c in self.calls if c[:2] == ("run", "cancel")]

    def force_cancels(self):
        return [c for c in self.calls if c[0] == "api" and c[-1].endswith("/force-cancel")]


def _publish(docs, github, **kw):
    clock = FakeClock()
    return warn_site_check.publish(docs_dir=docs, fetch=github.fetch, run_gh=github,
                                   sleep=clock.sleep, clock=clock, log=lambda _m: None, **kw)


def test_publish_does_nothing_when_the_site_is_current(docs):
    github = FakeGitHub(live=BUILD)
    assert warn_site_check.in_sync(_publish(docs, github))
    assert github.calls == []


def test_publish_dispatches_pages_on_main_and_waits_for_it(docs):
    github = FakeGitHub()
    assert warn_site_check.in_sync(_publish(docs, github))
    assert github.dispatches() == [("workflow", "run", "pages.yml", "--ref", "main")]


def test_publish_redeploys_when_only_california_is_behind(docs):
    github = FakeGitHub(live=BUILD)
    github.live["ca/data.json"] = OLD_BUILD
    assert warn_site_check.in_sync(_publish(docs, github))
    assert len(github.dispatches()) == 1


def test_publish_clears_the_run_that_caused_the_outage(docs):
    """The 2026-09-17 run, "waiting" at the gate and holding the deploy
    queue, must be cancelled before the fresh deploy is queued."""
    github = FakeGitHub(runs=[(35175481498, "waiting", 22000), (36960151090, "pending", 235),
                              (35121221198, "completed", 23000)])
    assert warn_site_check.in_sync(_publish(docs, github))
    assert github.cancels() == [("run", "cancel", "35175481498"), ("run", "cancel", "36960151090")]
    assert max(github.calls.index(c) for c in github.cancels()) < \
        github.calls.index(github.dispatches()[0])


def test_publish_never_touches_a_live_deploy(docs):
    github = FakeGitHub(runs=[(7, "in_progress", 235, 0.2)])
    _publish(docs, github)
    assert github.cancels() == [] and github.force_cancels() == []


def test_a_run_that_ignores_cancel_is_force_cancelled(docs):
    github = FakeGitHub(runs=[(35175481498, "waiting", 22000)], cancel_works=False)
    _publish(docs, github)
    assert github.force_cancels() == [
        ("api", "-X", "POST", "repos/{owner}/{repo}/actions/runs/35175481498/force-cancel")]


def test_force_cancel_spares_a_run_that_started_deploying_meanwhile(docs):
    """Pending run P sits behind stuck holder H. P's plain cancel fails (an
    API 502), H's succeeds, so P starts deploying. Force-cancel skips all
    cleanup, so P must not get it."""
    def on_cancel(github, run_id):
        if run_id == 2:                       # H released: P picks up a runner
            github.runs[1] = ["in_progress", 235, 0.1]

    github = FakeGitHub(runs=[(1, "pending", 235), (2, "waiting", 22000)], on_cancel=on_cancel)

    real = github.__call__

    def flaky(*args):
        if args == ("run", "cancel", "1"):
            github.calls.append(args)
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 502")
        return real(*args)

    clock = FakeClock()
    warn_site_check.publish(docs_dir=docs, fetch=github.fetch, run_gh=flaky,
                            sleep=clock.sleep, clock=clock, log=lambda _m: None)
    assert ("api", "-X", "POST", "repos/{owner}/{repo}/actions/runs/1/force-cancel") \
        not in github.calls


def test_publish_retries_once_then_reports_failure(docs):
    """A site that never catches up must end in a non-ok verdict. That is
    what turns the publish job red instead of green-and-stale."""
    github = FakeGitHub(deploys_land=False)
    assert not warn_site_check.in_sync(_publish(docs, github))
    assert len(github.dispatches()) == 2


def test_attempt_two_does_not_cancel_attempt_ones_queued_run(docs):
    """Runners are congested: attempt 1's deploy is still "queued" when
    attempt 2 starts ~15 min later. Cancelling it only loses its place."""
    github = FakeGitHub(deploys_land=False)
    real = github.__call__

    def dispatch_queues_a_run(*args):
        out = real(*args)
        if args[:2] == ("workflow", "run"):
            github.runs[100 + len(github.runs)] = ["queued", 15]
        return out

    clock = FakeClock()
    warn_site_check.publish(docs_dir=docs, fetch=github.fetch, run_gh=dispatch_queues_a_run,
                            sleep=clock.sleep, clock=clock, log=lambda _m: None)
    assert github.cancels() == []


def test_gh_failures_still_end_in_a_verdict(docs):
    """A broken gh (no token, API 500) is logged; the verdict comes from the
    site, so a failure here never masquerades as success or crashes early."""
    def broken(*args):
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 500")

    clock = FakeClock()
    results = warn_site_check.publish(docs_dir=docs, fetch=everywhere(OLD_BUILD), run_gh=broken,
                                      sleep=clock.sleep, clock=clock, log=lambda _m: None)
    assert not warn_site_check.in_sync(results)


# ── Watchdog mode (grace) ──────────────────────────────────────────────────


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin publish()'s clock to ``young`` minutes after BUILD."""
    def freeze(minutes_after_build):
        at = datetime.fromisoformat(BUILD) + timedelta(minutes=minutes_after_build)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return at

        monkeypatch.setattr(warn_site_check, "datetime", Frozen)
    return freeze


def test_watchdog_leaves_a_young_build_to_its_pipeline_run(docs, frozen_now):
    """site-watchdog.yml must not race monitor.yml's own publish job."""
    frozen_now(10)
    github = FakeGitHub()
    assert warn_site_check.in_sync(_publish(docs, github, grace=timedelta(minutes=45)))
    assert github.dispatches() == [] and github.cancels() == []


def test_watchdog_clears_a_stuck_run_the_stamps_cannot_see(docs):
    """A human push that changed docs/ but not data.json, its deploy stuck
    "waiting" for 2 h: the stamps match, the queue is still blocked."""
    github = FakeGitHub(runs=[(9, "waiting", 120), (10, "pending", 5)], live=BUILD)
    assert warn_site_check.in_sync(_publish(docs, github, grace=timedelta(minutes=45)))
    assert github.cancels() == [("run", "cancel", "9")]       # not the 5-minute-old one
    assert len(github.dispatches()) == 1


def test_the_pipeline_publish_does_not_scan_the_queue_when_in_sync(docs):
    """Without grace (monitor.yml), a just-queued run is not stuck: leave it."""
    github = FakeGitHub(runs=[(10, "pending", 1)], live=BUILD)
    _publish(docs, github)
    assert github.calls == []


# ── The CLI ────────────────────────────────────────────────────────────────


def test_cli_exit_codes(docs, monkeypatch, capsys):
    """The exit code is what monitor.yml's publish job and site-watchdog.yml
    turn red on: 0 = the live site serves this build, 1 = it does not."""
    monkeypatch.setattr(warn_site_check, "live_head", everywhere(OLD_BUILD))
    assert warn_site_check.main(["--docs", str(docs)]) == 1
    assert "BEHIND" in capsys.readouterr().out

    monkeypatch.setattr(warn_site_check, "live_head", everywhere(BUILD))
    assert warn_site_check.main(["--docs", str(docs)]) == 0


def test_cli_fails_when_only_california_is_stale(docs, monkeypatch):
    monkeypatch.setattr(warn_site_check, "live_head",
                        serving({"data.json": BUILD, "ca/data.json": OLD_BUILD}))
    assert warn_site_check.main(["--docs", str(docs)]) == 1


def test_failure_never_advises_rerunning_the_whole_monitor_run(docs, monkeypatch, capsys):
    """Re-running a whole WARN Monitor run replays its pipeline from the
    commit it started on, whose ledgers predate the alerts it already sent:
    every subscriber would get them again."""
    monkeypatch.setattr(warn_site_check, "live_head", everywhere(OLD_BUILD))
    warn_site_check.main(["--docs", str(docs)])
    out = capsys.readouterr().out
    assert "Re-run failed jobs" in out
    assert "Never “Re-run all jobs”" in out


def test_watchdog_grace_is_minutes_and_reaches_publish(docs, monkeypatch):
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return [{"ok": True}]

    monkeypatch.setattr(warn_site_check, "publish", spy)
    assert warn_site_check.main(["--publish", "--grace", "45", "--docs", str(docs)]) == 0
    assert seen["grace"] == timedelta(minutes=45)


# ── The workflows ──────────────────────────────────────────────────────────
# No YAML parser in requirements.txt, so these read the workflows as text,
# with comment lines dropped: the explanations quote the settings they
# replaced, and must neither satisfy nor trip a check.

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"


def _workflow(name):
    lines = (WORKFLOWS / name).read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _block(text, header, indent):
    """The lines under ``header`` (at ``indent`` spaces), up to the next line
    at that indent or less."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(" " * indent + header) and not line[indent].isspace())
    out = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


def test_pages_never_cancels_a_deploy_in_progress():
    """Flipping this to true looks like the fix for the 2026-09-17 outage,
    and it is not. True is not documented to reach a run waiting on the
    environment gate, which is the run that was stuck. And cancelling
    deploy-pages mid-flight can leave Pages "in progress", which fails every
    later deploy. Clearing stuck runs is warn_site_check's job, from outside
    this group."""
    block = _block(_workflow("pages.yml"), "concurrency:", 0)
    assert re.search(r"^\s+group:\s*['\"]?pages['\"]?\s*$", block, re.M)
    assert re.search(r"^\s+cancel-in-progress:\s*false\s*$", block, re.M)


def test_pages_can_still_be_dispatched():
    """--publish dispatches pages.yml by file name. Events raised with a
    GITHUB_TOKEN start no runs, except workflow_dispatch and
    repository_dispatch."""
    assert warn_site_check.PAGES_WORKFLOW == "pages.yml"
    assert (WORKFLOWS / "pages.yml").exists()
    assert re.search(r"^\s+workflow_dispatch:", _workflow("pages.yml"), re.M)


def test_the_pipeline_run_publishes_and_verifies_its_own_build():
    """The run that emails an alert must also put that build on the site, or
    go red. It does that in a job of its own, so "Re-run failed jobs" retries
    only the publish, never the alerts. The job checks out the branch tip,
    because github.sha predates the run's own push and would always read as
    "in sync"."""
    job = _block(_workflow("monitor.yml"), "publish:", 2)
    assert re.search(r"^\s+needs:\s*\[?\s*update-dashboard\s*\]?\s*$", job, re.M)
    assert re.search(r"^\s+ref:\s*\$\{\{\s*github\.ref_name\s*\}\}", job, re.M)
    assert re.search(r"^\s+actions:\s*write", job, re.M)
    assert "GH_TOKEN: ${{ github.token }}" in job
    assert re.search(r"run:\s*python3 warn_site_check\.py --publish\s*$", job, re.M)
    assert "continue-on-error" not in job and not re.search(r"^\s+if:", job, re.M)


def test_a_watchdog_runs_outside_the_deploy_queue():
    """A check sharing the `pages` group would wait behind the very run it
    has to clear."""
    text = _workflow("site-watchdog.yml")
    assert re.search(r"^\s*schedule:\s*\n\s*-\s*cron:", text, re.M)
    assert re.search(r"run:\s*python3 warn_site_check\.py --publish --grace \d+", text)
    assert re.search(r"^\s+actions:\s*write", text, re.M)
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert not re.search(r"^\s*group:\s*['\"]?pages['\"]?\s*$", text, re.M)


def test_watchdog_grace_outlasts_a_pipeline_publish():
    """The watchdog's grace must cover the pipeline's own publish job (every
    attempt's full wait), or the two race on the same build."""
    grace = int(re.search(r"--grace (\d+)", _workflow("site-watchdog.yml")).group(1))
    assert grace * 60 > 2 * warn_site_check.DEPLOY_WAIT_S
