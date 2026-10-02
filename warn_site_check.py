"""
warn_site_check.py
------------------
Make sure the live site serves what ``main`` has committed under ``docs/``.

Committing a rebuilt ``docs/`` is not publishing it. From 2026-09-17 to
2026-10-02 one ``pages.yml`` run sat in "waiting" on the ``github-pages``
environment. The environment had no reviewers and no wait timer, and its
only rule was a branch policy that ``main`` satisfies. The run held the
``pages`` concurrency group, so every later deploy queued behind it and was
cancelled by the next. Meanwhile the pipeline kept fetching, committing and
emailing twice a day. An alert announced 36 new California notices while
both dashboards still said 2026-09-16. No step failed, so nothing went red.

This module checks the outcome rather than the steps. It compares each
dashboard's ``data.json`` in this checkout's ``docs/`` with the copy the
public site serves, using the ``last_updated`` stamp every build writes. In
CI the checkout is the tip of ``main``. By hand, ``git pull`` first. Once a
deploy lands the live file *is* the committed file, so the stamps match
exactly. A live stamp that is behind means the build never reached the site.

``--publish`` also fixes it. It cancels stuck Deploy Pages runs, dispatches
a fresh one on ``main``, and waits for the site to catch up. It retries once,
and exits non-zero if the site is still behind. ``monitor.yml`` runs it in a
job of its own right after the pipeline's commit. So the run that sends an
alert also puts the matching build on the site, or fails visibly.
``site-watchdog.yml`` runs it every two hours with ``--grace``. That is the
backstop for deploys the pipeline does not own, such as human pushes.

The pipeline still emails before it publishes, deliberately: an alert must
never wait on, or be lost to, a Pages problem. So on a healthy run the
dashboards trail the email by a few minutes. That covers the deploy, plus up
to the CDN's 10-minute cache.

Only the first few KB of each file are read, locally and over the network.
The national payload is ~14 MB and ``last_updated`` is its first key.

Usage:
    python3 warn_site_check.py                    # exit 0 in sync, 1 behind
    python3 warn_site_check.py --wait 900         # poll until in sync
    python3 warn_site_check.py --publish          # deploy + verify (needs gh, GH_TOKEN)
    python3 warn_site_check.py --publish --grace 45   # watchdog: leave young builds and runs alone
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from warn_urls import url

DOCS_DIR = Path(__file__).parent / "docs"

# (path under docs/, public URL). One per dashboard: the root (US) page and
# California are separate builds, so a check of one says nothing about the
# other. Both are rewritten by every pipeline run.
PAGES = (
    ("data.json", url("/data.json")),
    ("ca/data.json", url("/ca/data.json")),
)

# The deploy workflow, by FILE name: that is what ``gh workflow run`` and
# ``gh run list --workflow`` resolve. Events raised with a GITHUB_TOKEN start
# no runs, except workflow_dispatch and repository_dispatch. --publish uses
# workflow_dispatch, so pages.yml must keep that trigger.
PAGES_WORKFLOW = "pages.yml"

# ``last_updated`` sits within the first few hundred bytes of both payloads
# (first key of the compact national file, fourth of the indented CA one).
HEAD_BYTES = 64 * 1024

# GitHub Pages' CDN holds a file for max-age=600. Deploys have been seen to
# purge it, but that is undocumented, so a wait must outlast the TTL. Query
# strings do not vary the cache key, so they cannot be used to bypass it.
DEPLOY_WAIT_S = 15 * 60

_STAMP_RE = re.compile(r'"last_updated"\s*:\s*"([^"]+)"')


def parse_time(value) -> Optional[datetime]:
    """An ISO-8601 timestamp as an aware datetime (naive = UTC), or None."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def parse_stamp(text: str) -> Optional[datetime]:
    """The ``last_updated`` timestamp in a data.json head, or None."""
    match = _STAMP_RE.search(text or "")
    return parse_time(match.group(1)) if match else None


def local_head(rel_path: str, docs_dir: Path = DOCS_DIR) -> str:
    with open(Path(docs_dir) / rel_path, "rb") as fh:
        return fh.read(HEAD_BYTES).decode("utf-8", errors="replace")


def live_head(page_url: str, timeout: float = 30) -> str:
    """The first HEAD_BYTES of the public file, as a browser receives it.

    The request asks for gzip because browsers do, and the CDN caches each
    encoding as a separate entry. Checking the uncompressed copy could pass
    while visitors are still served a stale gzip one.
    """
    request = urllib.request.Request(
        page_url, headers={"Accept-Encoding": "gzip", "User-Agent": "warn-site-check"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read(HEAD_BYTES)
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            # A truncated gzip stream still inflates as far as it goes.
            raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, HEAD_BYTES)
    return raw.decode("utf-8", errors="replace")


def check(
    docs_dir: Path = DOCS_DIR,
    fetch: Optional[Callable[[str], str]] = None,
    grace: timedelta = timedelta(0),
    now: Optional[datetime] = None,
) -> list:
    """One result per dashboard: ``{"path", "local", "live", "ok", "reason"}``.

    ``ok`` means the live site serves this build or a newer one. Anything
    that prevents confirming that (a fetch error, a 404, an unreadable stamp)
    is *not* ok. When the answer is "can't tell", the safe response is to
    redeploy, which is idempotent. Assuming all was well is what hid the
    outage for 15 days.

    ``grace`` is for the watchdog only. A build younger than that which the
    site has not caught up with is reported ok, because the pipeline run that
    committed it is presumably still publishing it.
    """
    fetch = fetch or live_head
    now = now or datetime.now(timezone.utc)
    results = []
    for rel_path, page_url in PAGES:
        try:
            local = parse_stamp(local_head(rel_path, docs_dir))
        except OSError as exc:
            results.append(_result(rel_path, None, None, False, f"cannot read docs/{rel_path}: {exc}"))
            continue
        if local is None:
            results.append(_result(rel_path, None, None, False, f"docs/{rel_path} has no last_updated"))
            continue
        try:
            live = parse_stamp(fetch(page_url))
        except Exception as exc:  # urllib raises a zoo: HTTPError, URLError, timeouts, zlib
            results.append(_result(rel_path, local, None, False, f"cannot fetch {page_url}: {exc}"))
            continue
        if live is None:
            results.append(_result(rel_path, local, None, False, f"{page_url} has no last_updated"))
        elif live >= local:
            results.append(_result(rel_path, local, live, True, "in sync"))
        elif now - local < grace:
            results.append(_result(rel_path, local, live, True,
                                   "build is recent; its deploy may be in flight"))
        else:
            results.append(_result(rel_path, local, live, False, f"live site is {_age(local - live)} behind"))
    return results


def in_sync(results: list) -> bool:
    return all(r["ok"] for r in results)


def _result(path, local, live, ok, reason) -> dict:
    return {"path": path, "local": local, "live": live, "ok": ok, "reason": reason}


def _age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} hours"


def report(results: list) -> str:
    def stamp(value):
        return value.isoformat(timespec="seconds") if value else "—"

    return "\n".join(
        f"{'OK    ' if r['ok'] else 'BEHIND'} {r['path']:<14} build {stamp(r['local'])}  "
        f"live {stamp(r['live'])}  ({r['reason']})"
        for r in results
    )


def wait_for_sync(
    timeout_s: float,
    interval_s: float = 15,
    docs_dir: Path = DOCS_DIR,
    fetch: Optional[Callable[[str], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list:
    """Re-check until every dashboard is in sync or ``timeout_s`` elapses."""
    deadline = clock() + timeout_s
    while True:
        results = check(docs_dir, fetch)
        if in_sync(results) or clock() >= deadline:
            return results
        sleep(interval_s)


# ── Deploying ──────────────────────────────────────────────────────────────
# Through the gh CLI, which every GitHub-hosted runner has and which reads
# GH_TOKEN. The token needs `actions: write` to cancel and dispatch runs.


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


# How long a Deploy Pages run may legitimately take, measured from when a
# runner picked it up. pages.yml's deploy job has timeout-minutes: 20, which
# counts from that same moment. A run still deploying past this is a ghost.
ACTIVE_DEPLOY_MAX_AGE = timedelta(minutes=30)


def unfinished_deploys(run_gh: Callable[..., str] = gh) -> list:
    """Deploy Pages runs not yet completed: ``[{"id", "status", "created"}]``.

    ``created`` is when the run was queued. gh's run-level ``startedAt`` is
    the same instant, not when the run began deploying; see deploying_since.
    """
    runs = json.loads(run_gh("run", "list", "--workflow", PAGES_WORKFLOW, "--limit", "50",
                             "--json", "databaseId,status,createdAt") or "[]")
    return [
        {"id": r["databaseId"], "status": r.get("status"), "created": parse_time(r.get("createdAt"))}
        for r in runs if r.get("status") != "completed"
    ]


def deploying_since(run_id, run_gh: Callable[..., str] = gh) -> Optional[datetime]:
    """When a runner picked up this run: the earliest start of any of its steps.

    Two other timestamps look right and are not:

    * the run's ``startedAt`` is its creation time, so it includes hours spent
      pending. Run 36960151090 reads 03:26 and deployed at 07:20.
    * the job's ``startedAt`` is stamped when the job reaches the environment
      gate. The job held there on 2026-09-17 was "started" for 15 days with
      no runner and no steps.

    Returns None if no step has started or the lookup fails. Callers treat
    None as "do not cancel": a mistaken cancel can wedge Pages, and a real
    ghost will still be a ghost on the next pass.
    """
    try:
        jobs = json.loads(run_gh("run", "view", str(run_id), "--json", "jobs") or "{}").get("jobs") or []
    except (subprocess.CalledProcessError, OSError, ValueError, AttributeError):
        return None
    starts = [parse_time(step.get("startedAt")) for job in jobs for step in (job.get("steps") or [])]
    starts = [t for t in starts if t and t.year > 1]  # gh renders "never" as 0001-01-01
    return min(starts) if starts else None


def stuck_deploys(runs: list, run_gh: Callable[..., str] = gh,
                  now: Optional[datetime] = None) -> list:
    """The unfinished runs it is safe and useful to cancel, by id.

    * "waiting" (held at the environment gate, as on 2026-09-17) and
      "pending" (queued behind the ``pages`` group): at any age. Neither has
      reached a runner, so cancelling interrupts nothing, and the fresh
      dispatch that follows supersedes them.
    * "queued"/"requested" (waiting for a runner): once older than
      ACTIVE_DEPLOY_MAX_AGE. A younger one may be this invocation's own
      previous attempt, and cancelling it only costs it its place in line.
    * "in_progress": only once a runner has been on it longer than
      ACTIVE_DEPLOY_MAX_AGE. Cancelling actions/deploy-pages mid-flight can
      leave the Pages deployment marked in progress, because its only cleanup
      is a best-effort signal handler. Every later deploy then fails "due to
      in progress deployment", which is a worse wedge than the one being
      cleared. This is also why pages.yml keeps ``cancel-in-progress: false``.
    """
    now = now or datetime.now(timezone.utc)
    stuck = []
    for run in runs:
        status, created = run["status"], run["created"]
        if status == "in_progress":
            since = deploying_since(run["id"], run_gh)
            if since is not None and now - since > ACTIVE_DEPLOY_MAX_AGE:
                stuck.append(run["id"])
        elif status in ("queued", "requested"):
            if created is None or now - created > ACTIVE_DEPLOY_MAX_AGE:
                stuck.append(run["id"])
        else:
            stuck.append(run["id"])
    return stuck


def clear_stuck_deploys(run_gh: Callable[..., str] = gh,
                        sleep: Callable[[float], None] = time.sleep,
                        log: Callable[[str], None] = print,
                        now: Optional[datetime] = None,
                        min_age: timedelta = timedelta(0)) -> list:
    """Cancel every stuck Deploy Pages run (see stuck_deploys). Returns their ids.

    A newer run does NOT reliably displace one held at the environment gate.
    GitHub does not document whether ``cancel-in-progress`` reaches a
    "waiting" run, and a staff-acknowledged report says it does not. An
    explicit cancel from outside the ``pages`` group does work: a plain
    cancel released the 2026-09-17 run instantly.

    ``force-cancel`` is the documented fallback for a run that ignores a plain
    cancel. Before using it, the runs are classified again, because a run
    listed as "pending" may have started deploying once the run ahead of it
    was cleared. Force-cancel skips cleanup entirely, so it must never reach
    a live deploy.

    ``min_age`` limits this to runs queued at least that long ago.
    """
    now = now or datetime.now(timezone.utc)

    def listed():
        return [r for r in unfinished_deploys(run_gh)
                if min_age <= timedelta(0) or (r["created"] and now - r["created"] >= min_age)]

    stuck = stuck_deploys(listed(), run_gh, now)
    for run_id in stuck:
        log(f"Cancelling stuck Deploy Pages run {run_id}")
        _try(run_gh, log, "run", "cancel", str(run_id))
    if not stuck:
        return stuck
    sleep(20)
    for run_id in set(stuck) & set(stuck_deploys(listed(), run_gh, now)):
        log(f"Run {run_id} ignored cancel; force-cancelling")
        _try(run_gh, log, "api", "-X", "POST",
             f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/force-cancel")
    return stuck


def _try(run_gh, log, *args) -> bool:
    try:
        run_gh(*args)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        log(f"gh {' '.join(args)} failed: {str(detail).strip()}")
        return False


def _dispatch(run_gh, log) -> bool:
    return _try(run_gh, log, "workflow", "run", PAGES_WORKFLOW, "--ref", "main")


def _annotate(level: str, message: str) -> str:
    """A GitHub Actions annotation in CI (it surfaces on the run's summary
    page, not only in the log); the bare message elsewhere."""
    return f"::{level}::{message}" if os.environ.get("GITHUB_ACTIONS") else message


def publish(
    attempts: int = 2,
    wait_s: float = DEPLOY_WAIT_S,
    grace: timedelta = timedelta(0),
    docs_dir: Path = DOCS_DIR,
    fetch: Optional[Callable[[str], str]] = None,
    run_gh: Callable[..., str] = gh,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> list:
    """Get the committed build onto the live site, and prove it is there.

    Does nothing if the site already serves it. Otherwise each attempt
    clears stuck deploys, dispatches a fresh one on ``main``, and waits for
    the live stamps to catch up. Returns the final check. Errors talking to
    GitHub are logged rather than raised, so even a broken attempt ends in a
    verdict about the site, which is the thing that matters.

    With ``grace`` (watchdog mode), matching stamps are not the end of it.
    A deploy can stall without the stamps showing it, for example a human
    push that changed ``docs/`` but not ``data.json``. And a run stuck in the
    queue blocks the next deploy, whoever triggers it. So runs that are still
    stuck after ``grace`` are cleared and replaced even when the site is in sync.
    """
    results = check(docs_dir, fetch, grace=grace)
    log(report(results))
    if in_sync(results) and grace > timedelta(0):
        try:
            cleared = clear_stuck_deploys(run_gh, sleep, log, min_age=grace)
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            log(f"Could not list Deploy Pages runs: {exc}")
            cleared = []
        if cleared:
            log(_annotate("warning", f"Cleared {len(cleared)} Deploy Pages run(s) stuck for over "
                                     f"{grace}; dispatched a fresh deploy of main."))
            _dispatch(run_gh, log)
        return results
    for attempt in range(1, attempts + 1):
        if in_sync(results):
            return results
        log(f"Deploy attempt {attempt}/{attempts}")
        try:
            clear_stuck_deploys(run_gh, sleep, log)
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            log(f"Could not list Deploy Pages runs: {exc}")
        _dispatch(run_gh, log)
        results = wait_for_sync(wait_s, docs_dir=docs_dir, fetch=fetch, sleep=sleep, clock=clock)
        log(report(results))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--publish", action="store_true",
                        help="if behind, (re)deploy pages.yml and wait for the site")
    parser.add_argument("--grace", type=float, default=0, metavar="MINUTES",
                        help="leave a build younger than this alone (its deploy may be in flight)")
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                        help="poll until in sync, up to this long")
    parser.add_argument("--docs", type=Path, default=DOCS_DIR,
                        help="the build to check for (default: this checkout's docs/; git pull first)")
    args = parser.parse_args(argv)

    grace = timedelta(minutes=args.grace)
    if args.publish:
        results = publish(grace=grace, docs_dir=args.docs)
    elif args.wait:
        results = wait_for_sync(args.wait, docs_dir=args.docs)
        print(report(results))
    else:
        results = check(args.docs, grace=grace)
        print(report(results))
    if in_sync(results):
        return 0
    # Never just "re-run": re-running a whole WARN Monitor run replays its
    # pipeline from the commit it started on, whose alert ledgers predate the
    # emails it already sent, so every subscriber would get them again.
    print(_annotate("error",
                    "The live site is not serving this build (see above). To retry: "
                    "Actions ▸ Site Watchdog ▸ Run workflow, or “Re-run failed jobs” here. "
                    "Never “Re-run all jobs” on a WARN Monitor run: that re-sends its alerts."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
