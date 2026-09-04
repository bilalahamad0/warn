"""
warn_x
------
The @USLayoff posting stage: queue, review gate, safety, transports, CLI.

WHAT THIS IS FOR. When a pipeline run detects new WARN notices, a handful of
them are news — a big employer, or a big number. This composes one post per
company (combined across every city and state in that run) and stages it. A
human approves it. Then it goes out.

THE GATE AND THE AUTOMATION ARE ONE CODE PATH. The pipeline never posts
directly; it only ever *enqueues* into ``data/x_queue.json``. Posting reads
rows whose status is ``approved``. In review mode a human writes that word; in
auto mode ``X_AUTO_POST=1`` writes it. Nothing else differs — the bytes a
person reviewed in phase 1 are byte-for-byte what auto mode sends in phase 2,
so flipping the gate cannot change what a post says.

TRANSPORTS. ``dry`` composes and stages, sends nothing (the default, and what
CI runs until the account is wired up). ``api`` posts through tweepy with
OAuth 1.0a. ``browser`` stages approved text into ``data/x_outbox.json`` for a
logged-in browser session to post, then ``mark-posted`` records the result —
the ledger discipline is identical, only the hands differ.

LEDGERS. ``data/x_posted_keys.json`` records a notice only AFTER a post lands.
Same rule, and the same helpers, as ``notified_keys.json``: a failed send
retries next run instead of being silently swallowed, and a successful one can
never be repeated. ``data/x_rejected_keys.json`` is its mirror for "no" — the
queue row that carried a rejection is swept after SWEEP_AFTER_DAYS, the ledger
is not. Both must live under ``data/`` — ``warn_publish.commit_ledgers`` and
monitor.yml's failure branch stage ``data/`` alone, and a ledger written
anywhere else is lost with the CI workspace, after which every notice is
re-posted twice a day forever. (odishanow20 kept its upload history in a
gitignored dir and its 2/day cap silently reset on every runner.)
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:  # noqa: BLE001
    pass

import warn_monitor
import warn_x_select

log = logging.getLogger("warn_x")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

QUEUE_NAME = "x_queue.json"
POSTED_NAME = "x_posted_keys.json"
REJECTED_NAME = "x_rejected_keys.json"
STATE_NAME = "x_state.json"
OUTBOX_NAME = "x_outbox.json"
CARDS_DIR = "x_cards"

QUEUE_VERSION = 1

# ---------------------------------------------------------------------------
# Safety limits — ported from odishanow20's TWITTER_SAFETY_GUIDELINES.md
# ---------------------------------------------------------------------------
MAX_POSTS_PER_RUN = 6
MAX_POSTS_PER_DAY = 12
MIN_SECONDS_BETWEEN_POSTS = 75    # the 60-90s spacing that fixed their 403s
CIRCUIT_OPEN_HOURS_ON_403 = 24    # their rule: wait 24-48h, never auto-retry
QUEUE_TTL_DAYS = 7                # an unreviewed draft goes stale
SWEEP_AFTER_DAYS = 30             # terminal rows are dropped from the file

# Even in auto mode these stay pending for a human. Auto-approval is a
# default, not an override — every one of these is a case where the composed
# text is most likely to be wrong or the grouping most likely to have merged
# two employers.
AUTO_APPROVE_BLOCKERS = ("over_budget", "employees_unknown", "partial_headcount")
AUTO_APPROVE_MAX_SITES = 6
AUTO_APPROVE_MAX_EMPLOYEES = 2000   # p99 grouped is 1,172

STATUSES = ("pending", "approved", "rejected", "posted", "failed", "expired")

# Which statuses a row may be moved OUT of. ``posted`` is in neither list and
# never will be: a tweet that is public cannot be un-published, so nothing may
# hand a posted row back to the transport. `list --status all` prints posted
# ids and `approve <id>` used to take them, which re-sent live text verbatim.
# The rest are recoverable — a transport failure and an aged-out draft are both
# things a human may legitimately re-approve.
APPROVABLE_FROM = ("pending", "failed", "expired")
REJECTABLE_FROM = ("pending", "approved", "failed", "expired")


# ---------------------------------------------------------------------------
# Paths and config — all resolved at CALL time
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    """Resolve a data file at call time so tests can repoint DATA_DIR."""
    return DATA_DIR / name


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _x_config() -> tuple:
    """OAuth 1.0a credentials, read from the environment at call time.

    Never captured into module globals at import, so .env, CI-exported vars
    and test monkeypatching are all honoured (the warn_notify._smtp_config
    pattern).
    """
    return (
        _env("X_API_KEY"),
        _env("X_API_SECRET"),
        _env("X_ACCESS_TOKEN"),
        _env("X_ACCESS_SECRET"),
    )


def transport() -> str:
    """``browser`` (default) | ``dry`` | ``api``.

    BROWSER IS THE DEFAULT because the API is not free. X went pay-per-use on
    2026-02-06 — $0.015 a post, $0.200 with a URL — and this account's console
    balance is $0.00. The browser path posts through a logged-in x.com session
    and costs nothing, so ``api`` stays only for whoever later decides to buy
    credits; tweepy is not even in requirements.txt.

    ``dry`` composes and queues without staging anything, which is what CI
    wants: a GitHub runner has no browser and no session to post from.
    """
    value = _env("X_TRANSPORT", "browser").lower()
    return value if value in ("dry", "api", "browser") else "browser"


def auto_post_enabled() -> bool:
    """The review gate. False (manual approval) until X_AUTO_POST=1."""
    return _flag("X_AUTO_POST", False)


def include_link() -> bool:
    """Whether posts carry the dashboard link.

    Costs real money on the API transport: X charges $0.015 for a post and
    $0.200 for a post containing a URL (pay-per-use, since 2026-02-06). At
    this feed's volume that is roughly $0.40 vs $5 a month — worth it for a
    link back to the source, but set X_INCLUDE_LINK=0 to drop it.
    """
    return _flag("X_INCLUDE_LINK", True)


# ---------------------------------------------------------------------------
# Ledger — the same machinery as notified_keys.json
# ---------------------------------------------------------------------------

def load_posted_keys() -> set:
    return warn_monitor._load_keys_file(_path(POSTED_NAME))


def record_posted_keys(keys) -> None:
    """Mark notices as posted. Called only after a post actually lands."""
    warn_monitor._record_keys(keys, _path(POSTED_NAME), "posted")


def load_rejected_keys() -> set:
    return warn_monitor._load_keys_file(_path(REJECTED_NAME))


def record_rejected_keys(keys) -> None:
    """Remember a "no" for longer than the queue row that carried it.

    ``_expire_stale`` sweeps rejected rows after SWEEP_AFTER_DAYS, and
    ``enqueue``'s only memory of a rejection was that row still being in the
    file — so on day 31 the same candidate_id was re-derived from the same
    notices and, with X_AUTO_POST=1, posted the text a human had refused.
    """
    warn_monitor._record_keys(keys, _path(REJECTED_NAME), "rejected")


# ---------------------------------------------------------------------------
# Run state — counters, kill switch, circuit breaker
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_state() -> dict:
    path = _path(STATE_NAME)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning(f"Unreadable {path.name} ({e}) — starting fresh.")
    return {
        "day": "", "posts_today": 0, "last_post_at": None, "total_posted": 0,
        "disabled": False, "disabled_reason": "", "circuit_open_until": None,
        "per_company_month": {},
    }


def _save_state(state: dict) -> None:
    path = _path(STATE_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _roll_day(state: dict) -> dict:
    today = _now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["posts_today"] = 0
    return state


def posting_disabled() -> tuple:
    """``(disabled, reason)`` — the kill switch, env or latched."""
    if _flag("X_DISABLE_POSTING", False):
        return True, "X_DISABLE_POSTING=1"
    state = _load_state()
    if state.get("disabled"):
        return True, state.get("disabled_reason") or "latched by warn_x kill"
    until = state.get("circuit_open_until")
    if until:
        try:
            if datetime.fromisoformat(until) > _now():
                return True, f"circuit breaker open until {until}"
        except ValueError:
            pass
    return False, ""


def _open_circuit(reason: str, hours: int = CIRCUIT_OPEN_HOURS_ON_403) -> None:
    state = _load_state()
    state["circuit_open_until"] = (_now() + timedelta(hours=hours)).isoformat()
    state["disabled_reason"] = reason
    _save_state(state)
    log.error(f"X circuit breaker OPEN for {hours}h: {reason}")


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def _load_queue() -> dict:
    path = _path(QUEUE_NAME)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.setdefault("candidates", [])
            return data
        except Exception as e:  # noqa: BLE001
            log.warning(f"Unreadable {path.name} ({e}) — starting a new queue.")
    return {"version": QUEUE_VERSION, "updated_at": None, "candidates": []}


def _save_queue(queue: dict) -> None:
    queue["version"] = QUEUE_VERSION
    queue["updated_at"] = _now().isoformat()
    path = _path(QUEUE_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2) + "\n")


def candidate_id(canonical_key: str, keys) -> str:
    """Stable id for a batch, so a re-run upserts instead of duplicating.

    Derived from the company plus the exact set of notices, which means a
    second run that re-derives the same batch produces the same id, while a
    batch that gained a site produces a new one.
    """
    payload = canonical_key + "|" + "|".join(sorted(keys))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _expire_stale(queue: dict) -> int:
    """Age out unreviewed drafts and sweep long-dead rows. Returns n expired."""
    now = _now()
    ttl = timedelta(days=QUEUE_TTL_DAYS)
    sweep = timedelta(days=SWEEP_AFTER_DAYS)
    expired, kept = 0, []
    for row in queue.get("candidates", []):
        try:
            created = datetime.fromisoformat(row.get("created_at"))
        except (TypeError, ValueError):
            created = now
        if row.get("status") == "pending" and now - created > ttl:
            row["status"] = "expired"
            expired += 1
        terminal = row.get("status") in ("posted", "rejected", "expired")
        if terminal and now - created > sweep:
            continue
        kept.append(row)
    queue["candidates"] = kept
    return expired


def _blocks_auto_approval(row: dict) -> str:
    """Why this row must stay pending even in auto mode ('' if nothing does)."""
    for w in AUTO_APPROVE_BLOCKERS:
        if w in row.get("warnings", []):
            return w
    if row.get("sites", 0) > AUTO_APPROVE_MAX_SITES:
        return f"sites>{AUTO_APPROVE_MAX_SITES}"
    if row.get("employees", 0) >= AUTO_APPROVE_MAX_EMPLOYEES:
        return f"employees>={AUTO_APPROVE_MAX_EMPLOYEES}"
    return ""


def _month_key() -> str:
    return _now().strftime("%Y-%m")


def _company_posts_this_month(canonical_key: str) -> int:
    state = _load_state()
    return state.get("per_company_month", {}).get(_month_key(), {}).get(
        canonical_key, 0
    )


def enqueue(drafts, auto_approve: bool = False) -> dict:
    """Upsert composed drafts into the queue.

    ``drafts`` is ``[(batch, verdict, draft), …]`` from
    ``warn_x_select.build_drafts``. Rows already past ``pending`` are never
    touched, so a re-run cannot resurrect something a human rejected or
    re-send something already posted. The two key ledgers carry that same
    memory past the row itself, which ``_expire_stale`` eventually sweeps.
    """
    queue = _load_queue()
    expired = _expire_stale(queue)
    by_id = {row["id"]: row for row in queue["candidates"]}
    posted = load_posted_keys()
    rejected = load_rejected_keys()

    added, approved, skipped = 0, 0, 0
    for batch, verdict, draft in drafts:
        keys = batch.keys
        # Every notice already posted -> nothing new to say.
        if keys and all(k in posted for k in keys):
            skipped += 1
            continue
        # Every notice already refused -> the human's "no" still stands, even
        # after the rejected row was swept out of the queue file.
        if keys and all(k in rejected for k in keys):
            skipped += 1
            continue
        rid = candidate_id(batch.key, keys)
        if rid in by_id:
            continue
        if _company_posts_this_month(batch.key) >= \
                warn_x_select.MAX_POSTS_PER_COMPANY_PER_MONTH:
            log.info(
                f"Skipping {batch.display}: already posted "
                f"{warn_x_select.MAX_POSTS_PER_COMPANY_PER_MONTH}x this month."
            )
            skipped += 1
            continue

        row = {
            "id": rid,
            "created_at": _now().isoformat(),
            "status": "pending",
            "company": batch.key,
            "display": batch.display,
            "employees": batch.employees,
            "states": batch.states,
            "per_state": batch.per_state,
            "sites": batch.sites,
            # Card inputs, stored rather than the rendered PNG: the card is
            # regenerated at post time so no binary lands in git twice a day,
            # and it can never drift from the row it illustrates.
            "card": warn_x_select.card_fields(batch),
            "arm": verdict.arm,
            "reason": verdict.reason,
            "text": draft.text,
            "thread": draft.thread,
            "weighted_len": draft.weighted_len,
            "warnings": draft.warnings,
            "keys": keys,
            "approved_by": None,
            "approved_at": None,
            "rejected_reason": None,
            "tweet_id": None,
            "posted_at": None,
            "attempts": 0,
            "last_error": None,
        }
        if auto_approve:
            blocker = _blocks_auto_approval(row)
            if blocker:
                log.info(f"{batch.display}: auto-approval withheld ({blocker}).")
            else:
                row["status"] = "approved"
                row["approved_by"] = "auto"
                row["approved_at"] = _now().isoformat()
                approved += 1
        queue["candidates"].append(row)
        by_id[rid] = row
        added += 1

    _save_queue(queue)
    return {
        "added": added, "auto_approved": approved,
        "skipped": skipped, "expired": expired,
        "pending": sum(1 for r in queue["candidates"] if r["status"] == "pending"),
    }


def _set_status(ids, status: str, allowed_from, **fields) -> tuple:
    """Move matching rows to ``status``. Returns ``(moved_rows, refused)``.

    ``refused`` is ``[(id, status), …]`` for rows a caller named whose current
    status is not in ``allowed_from``. They are returned rather than skipped in
    silence: a status command that reports nothing is how a human comes away
    believing a row is queued when it never moved.
    """
    queue = _load_queue()
    wanted = set(ids or [])
    moved, refused = [], []
    for row in queue["candidates"]:
        named = row["id"] in wanted or (not wanted and row["status"] == "pending")
        if not named:
            continue
        if row["status"] not in allowed_from:
            refused.append((row["id"], row["status"]))
            continue
        row["status"] = status
        row.update(fields)
        moved.append(row)
    _save_queue(queue)
    return moved, refused


def approve(ids, by: str = "manual") -> tuple:
    """Approve rows for posting. Returns ``(moved, refused)``.

    Only pending/failed/expired rows may move; ``posted`` is terminal. Without
    that guard `list --status all` printed posted ids and `approve <id>` took
    them, handing text that is already public straight back to the transport.
    """
    moved, refused = _set_status(
        ids, "approved", APPROVABLE_FROM,
        approved_by=by, approved_at=_now().isoformat(),
    )
    return len(moved), refused


def reject(ids, reason: str = "") -> tuple:
    """Reject rows and remember the refusal. Returns ``(moved, refused)``.

    ``posted`` is terminal here too — rejecting a live tweet cannot unsend it,
    and writing its notices to the rejected ledger would then suppress the one
    story we have already told.
    """
    moved, refused = _set_status(
        ids, "rejected", REJECTABLE_FROM, rejected_reason=reason
    )
    record_rejected_keys([k for row in moved for k in row.get("keys", [])])
    return len(moved), refused


def edit_text(row_id: str, text: str) -> dict:
    """Replace a pending row's text, re-validating length."""
    queue = _load_queue()
    for row in queue["candidates"]:
        if row["id"] == row_id:
            if row["status"] != "pending":
                raise ValueError(f"{row_id} is {row['status']}, not pending")
            length = warn_x_select.weighted_len(text)
            row["text"] = text
            row["weighted_len"] = length
            warnings = [w for w in row.get("warnings", []) if w != "over_budget"]
            if length > warn_x_select.MAX_WEIGHTED_LEN:
                warnings.append("over_budget")
            row["warnings"] = warnings
            _save_queue(queue)
            return row
    raise KeyError(row_id)


def rows(status: str = None) -> list:
    queue = _load_queue()
    if status in (None, "all"):
        return queue["candidates"]
    return [r for r in queue["candidates"] if r["status"] == status]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class PostError(Exception):
    """A post failed. ``kind`` drives the caller's circuit-breaker decision."""

    def __init__(self, message, kind="transient"):
        super().__init__(message)
        # unconfigured  no keys / no tweepy — skip quietly, latch NOTHING
        # credentials   X rejected the keys we have (401) — latch, needs a human
        # duplicate     already public; treat as success
        # policy        blocked by X's automation rules
        # restricted    403 of any other kind — latch for 24h
        # rate          429; defer to the next run
        # transient     5xx and everything else
        self.kind = kind


def _post_via_api(text: str, in_reply_to: str = None) -> str:
    """POST /2/tweets through tweepy. Returns the tweet id.

    v2 only. X retired v1.1 ``statuses/update`` in 2023, so odishanow20's
    try-v1.1-then-fall-back pattern always throws on the first leg and pays a
    wasted round trip per post.
    """
    # Credentials first, THEN the import. Reversing these makes an unconfigured
    # machine raise ModuleNotFoundError out of the transport instead of the
    # PostError the caller knows how to absorb — and the pipeline has to
    # complete a data run on a laptop with neither tweepy nor X keys, exactly
    # as it does today with no Gmail credentials.
    api_key, api_secret, token, secret = _x_config()
    if not all([api_key, api_secret, token, secret]):
        raise PostError(
            "X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET not "
            "set — skipping the post. Add them to .env to enable posting.",
            kind="unconfigured",
        )

    try:
        import tweepy
    except ImportError as e:
        raise PostError(
            f"tweepy is not installed ({e}) — pip install -r requirements.txt",
            kind="unconfigured",
        ) from e

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=token,
        access_token_secret=secret,
        # Deliberately False: with it True tweepy sleeps until
        # x-rate-limit-reset, which inside a GitHub Actions run blocks the job
        # for up to 15 minutes and cascades into every later step.
        wait_on_rate_limit=False,
    )
    kwargs = {"text": text}
    if in_reply_to:
        kwargs["in_reply_to_tweet_id"] = in_reply_to
    try:
        resp = client.create_tweet(**kwargs)
    except tweepy.Forbidden as e:
        raise PostError(str(e), kind=_classify_403(str(e))) from e
    except tweepy.Unauthorized as e:
        raise PostError(str(e), kind="credentials") from e
    except tweepy.TooManyRequests as e:
        raise PostError(str(e), kind="rate") from e
    except Exception as e:  # noqa: BLE001
        raise PostError(str(e), kind="transient") from e

    data = getattr(resp, "data", None) or {}
    tweet_id = data.get("id")
    if not tweet_id:
        raise PostError("create_tweet returned no id", kind="transient")
    return str(tweet_id)


def _classify_403(message: str) -> str:
    """Sort a 403 body into an action.

    v2 returns problem+json with a ``detail`` string and, unlike v1.1, NO
    numeric code — there is no code 187 to match on, so this reads the text.
    """
    low = (message or "").lower()
    if "duplicate content" in low:
        return "duplicate"
    if "not been mentioned" in low or "otherwise engaged" in low:
        return "policy"
    return "restricted"


def _post_via_browser(row: dict) -> None:
    """Stage an approved post for a logged-in browser session to send.

    The pipeline cannot drive a browser, so this writes the exact text to
    ``data/x_outbox.json`` and stops. Whoever (or whatever) posts it then runs
    ``warn_x.py mark-posted <id> --tweet-id <id>``, which is what writes the
    ledger — so a post that never actually went out is never recorded as sent.
    """
    path = _path(OUTBOX_NAME)
    outbox = {"updated_at": _now().isoformat(), "posts": []}
    if path.exists():
        try:
            outbox = json.loads(path.read_text())
            outbox.setdefault("posts", [])
        except Exception:  # noqa: BLE001
            pass
    if not any(p.get("id") == row["id"] for p in outbox["posts"]):
        outbox["posts"].append(
            {
                "id": row["id"],
                "display": row["display"],
                "text": row["text"],
                "thread": row.get("thread", []),
                "image": _render_card(row),
                "staged_at": _now().isoformat(),
            }
        )
    outbox["updated_at"] = _now().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outbox, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _render_card(row: dict):
    """Render this row's post card, returning its path as a string, or None.

    Generated here rather than at enqueue time so the PNG never enters git:
    CI stages the queue twice a day, and committing a ~100 KB image per
    candidate would add tens of megabytes a year to a repo whose whole point
    is small, reviewable diffs.
    """
    card = row.get("card") or {}
    try:
        import warn_x_image
    except ImportError as e:  # noqa: BLE001
        log.warning(f"No card for {row['id']} ({e}) — text-only post.")
        return None
    out = _path(CARDS_DIR) / f"{row['id']}.png"
    path = warn_x_image.build_card(
        display=row.get("display", ""),
        employees=card.get("employees"),
        place=card.get("place", ""),
        effective=card.get("effective"),
        states=row.get("states"),
        per_state=row.get("per_state"),
        out_path=out,
    )
    return str(path) if path else None


def _stamp_outbox(row_id: str, tweet_id: str) -> None:
    """Mark the browser outbox entry for a row that is now live.

    ``x_outbox.json`` is the whole instruction set a browser session works
    from. It was append-only and nothing ever stamped it, so a session that
    re-read the file after ``mark-posted`` had no way to tell a staged post
    from a sent one and posted every entry again.
    """
    path = _path(OUTBOX_NAME)
    if not path.exists():
        return
    try:
        outbox = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning(f"Unreadable {path.name} ({e}) — {row_id} left unstamped.")
        return
    stamped = False
    for entry in outbox.get("posts", []):
        if entry.get("id") == row_id and not entry.get("posted_at"):
            entry["posted_at"] = _now().isoformat()
            entry["tweet_id"] = tweet_id
            stamped = True
    if stamped:
        outbox["updated_at"] = _now().isoformat()
        path.write_text(json.dumps(outbox, indent=2) + "\n")


def _bump_tweet_count(n: int = 1) -> None:
    """Charge ``n`` tweets to the day's budget without touching the queue.

    The cap counts TWEETS; thread self-replies are tweets. See _mark_posted.
    """
    state = _roll_day(_load_state())
    state["posts_today"] = state.get("posts_today", 0) + n
    state["total_posted"] = state.get("total_posted", 0) + n
    state["last_post_at"] = _now().isoformat()
    _save_state(state)


def _mark_posted(row_id: str, tweet_id: str, keys, tweets: int = 1) -> None:
    """Record a landed post: queue row, run state, and the dedupe ledger.

    ``tweets`` is how many tweets actually went on the wire, because the daily
    cap is a cap on tweets and this used to add exactly one per ROW. A row
    whose company filed in more than warn_x_select.MAX_INLINE_STATES states
    sends 1 + len(thread) of them, so six threaded rows put 12 tweets out
    against a MAX_POSTS_PER_DAY of 12 that the counter read as 6 — and the run
    happily kept going. ``per_company_month`` still counts one per row: that
    limit is about how often one employer is a story, not about volume.
    """
    queue = _load_queue()
    for row in queue["candidates"]:
        if row["id"] == row_id:
            row["status"] = "posted"
            row["tweet_id"] = tweet_id
            row["posted_at"] = _now().isoformat()
            company = row.get("company")
            break
    else:
        company = None
    _save_queue(queue)
    _stamp_outbox(row_id, tweet_id)

    state = _roll_day(_load_state())
    state["posts_today"] = state.get("posts_today", 0) + tweets
    state["total_posted"] = state.get("total_posted", 0) + tweets
    state["last_post_at"] = _now().isoformat()
    if company:
        month = state.setdefault("per_company_month", {}).setdefault(_month_key(), {})
        month[company] = month.get(company, 0) + 1
    _save_state(state)

    # LAST, and only now: the notice is public, so it must never post again.
    record_posted_keys(keys)


def post_approved(limit: int = None, dry_run: bool = False,
                  force: bool = False) -> dict:
    """Send approved rows, oldest first, under every safety gate.

    Returns a summary. Never raises for an expected failure — a run must
    complete its data work whether or not X cooperates.
    """
    result = {"posted": 0, "failed": 0, "skipped": 0, "stopped": None,
              "tweet_ids": []}

    disabled, reason = posting_disabled()
    if disabled:
        log.warning(f"X posting disabled: {reason}")
        result["stopped"] = f"disabled: {reason}"
        return result

    state = _roll_day(_load_state())
    remaining_today = MAX_POSTS_PER_DAY - state.get("posts_today", 0)
    cap = MAX_POSTS_PER_RUN if not force else max(MAX_POSTS_PER_RUN, limit or 0)
    if limit:
        cap = min(cap, limit)
    cap = min(cap, max(remaining_today, 0))
    if cap <= 0:
        log.warning(
            f"Daily cap reached ({state.get('posts_today')}/{MAX_POSTS_PER_DAY})."
        )
        result["stopped"] = "daily cap"
        return result

    approved = sorted(
        (r for r in rows("approved")), key=lambda r: r.get("created_at") or ""
    )
    if not approved:
        return result

    mode = transport()
    posted_any = False
    for row in approved[:cap]:
        if dry_run:
            print(f"--- [{row['id']}] {row['display']} "
                  f"({row['weighted_len']}/280)\n{row['text']}\n")
            result["skipped"] += 1
            continue

        # Re-checked per post: the kill switch must be honourable mid-batch.
        disabled, reason = posting_disabled()
        if disabled:
            log.warning(f"Stopping batch — posting disabled: {reason}")
            result["stopped"] = f"disabled: {reason}"
            break

        # `cap` above is a row count; the budget is spent in tweets. Charge
        # the whole row — root plus every self-reply — against the live
        # counter before sending any of it, or six threaded rows walk 12
        # tweets past a 12/day cap that thought it had spent 6. The `spent`
        # test lets a row through when nothing has gone out today, so a row
        # costing more than the entire cap makes progress instead of wedging
        # the queue for good.
        spent = _roll_day(_load_state()).get("posts_today", 0)
        cost = 1 + len(row.get("thread", []))
        if mode == "api" and spent and spent + cost > MAX_POSTS_PER_DAY:
            log.warning(
                f"Daily cap: {spent}/{MAX_POSTS_PER_DAY} spent and this row "
                f"costs {cost} tweet(s) — stopping the batch."
            )
            result["stopped"] = "daily cap"
            break

        if posted_any and mode == "api":
            time.sleep(MIN_SECONDS_BETWEEN_POSTS)

        try:
            if mode == "browser":
                _post_via_browser(row)
                log.info(
                    f"Staged {row['display']} to {OUTBOX_NAME} for the browser "
                    "session; run `warn_x.py mark-posted` once it is live."
                )
                result["skipped"] += 1
                continue
            if mode == "dry":
                log.info(f"[dry] would post: {row['display']}")
                result["skipped"] += 1
                continue

            tweet_id = _post_via_api(row["text"])
        except PostError as e:
            posted_any = True    # a real attempt was made; keep the spacing
            if e.kind == "duplicate":
                # Already public. Recording it is correct — the goal is that
                # the notice is out there, not that we were the ones to send it.
                log.warning(
                    f"{row['display']}: duplicate content — treating as posted."
                )
                _mark_posted(row["id"], "duplicate", row.get("keys", []))
                result["posted"] += 1
                continue
            if e.kind == "unconfigured":
                # Not an error, just an un-set-up machine. Leave the row
                # approved so it goes out on the first run that IS configured,
                # and latch nothing — a laptop without keys must not disable
                # posting for the pipeline that does have them.
                log.warning(str(e))
                result["skipped"] += 1
                result["stopped"] = "unconfigured"
                break
            _fail_row(row["id"], str(e))
            result["failed"] += 1
            if e.kind in ("restricted", "credentials"):
                _open_circuit(f"{e.kind}: {e}")
                result["stopped"] = e.kind
                break
            if e.kind in ("rate", "policy"):
                log.error(f"Stopping batch ({e.kind}): {e}")
                result["stopped"] = e.kind
                break
            log.error(f"{row['display']}: {e}")
            result["stopped"] = "transient"
            break

        # The root is public the moment it has an id, so it is recorded before
        # anything else can fail. A thread self-reply that raised used to jump
        # to the except block above, which _fail_row()d the row and wrote no
        # ledger — a human re-approving it then sent the live root a second
        # time. Nothing below may demote a landed post.
        _mark_posted(row["id"], tweet_id, row.get("keys", []))
        result["posted"] += 1
        result["tweet_ids"].append(tweet_id)
        posted_any = True

        for extra in row.get("thread", []):
            try:
                time.sleep(MIN_SECONDS_BETWEEN_POSTS)
                reply_id = _post_via_api(extra, in_reply_to=tweet_id)
            except PostError as e:
                log.error(f"thread reply failed (root {tweet_id} is live): {e}")
                break
            _bump_tweet_count()
            result["tweet_ids"].append(reply_id)
            log.info(f"Threaded reply {reply_id}")

    return result


def _fail_row(row_id: str, error: str) -> None:
    """Mark a row failed. It NEVER auto-retries — a human re-approves it."""
    queue = _load_queue()
    for row in queue["candidates"]:
        if row["id"] == row_id:
            row["status"] = "failed"
            row["attempts"] = row.get("attempts", 0) + 1
            row["last_error"] = error[:500]
    _save_queue(queue)


def mark_posted(row_id: str, tweet_id: str) -> dict:
    """Record a post sent outside the API path (the browser transport)."""
    for row in rows("all"):
        if row["id"] == row_id:
            _mark_posted(row_id, tweet_id, row.get("keys", []))
            return row
    raise KeyError(row_id)


# ---------------------------------------------------------------------------
# The pipeline entry point
# ---------------------------------------------------------------------------

def run_stage(state_results: dict, force: bool = False) -> dict:
    """Compose and stage this run's candidates; post if auto mode is on.

    Called by ``warn_publish.maybe_post_to_x``. Returns a summary dict; the
    caller swallows exceptions, so nothing here may be load-bearing for the
    rest of the pipeline.
    """
    drafts = warn_x_select.build_drafts(
        state_results, include_link=include_link()
    )
    auto = auto_post_enabled()
    summary = enqueue(drafts, auto_approve=auto)
    log.info(
        f"X queue: {summary['added']} new candidate(s), "
        f"{summary['auto_approved']} auto-approved, {summary['pending']} pending."
    )
    if auto or force:
        summary["post"] = post_approved(force=force)
    else:
        summary["post"] = {"skipped": "review gate on (X_AUTO_POST unset)"}
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_row(row: dict, full: bool = False) -> str:
    head = (
        f"[{row['id']}] {row['status']:<9} {row['display']} · "
        f"{row['employees']:,} · {'/'.join(row['states'])} · "
        f"{row['arm']} · {row['weighted_len']}/280"
    )
    if row.get("warnings"):
        head += f" · ⚠ {','.join(row['warnings'])}"
    if not full:
        return head
    body = "\n".join("    " + line for line in row["text"].splitlines())
    extra = ""
    if row.get("thread"):
        extra = "\n  thread:\n" + "\n".join(
            "    " + line for t in row["thread"] for line in t.splitlines()
        )
    return f"{head}\n  reason: {row['reason']}\n{body}{extra}"


def _report_refused(refused, verb: str) -> int:
    """Print the rows a status command would not move; non-zero if any."""
    if not refused:
        return 0
    for rid, status in refused:
        print(f"Refused to {verb} [{rid}]: it is {status}.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="@USLayoff post queue — review gate and transport."
    )
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="show queued candidates")
    p_list.add_argument("--status", default="pending",
                        choices=list(STATUSES) + ["all"])

    p_show = sub.add_parser("show", help="full detail for one candidate")
    p_show.add_argument("id")

    p_ok = sub.add_parser("approve", help="approve candidates for posting")
    p_ok.add_argument("ids", nargs="*")
    p_ok.add_argument("--all", action="store_true", help="approve every pending row")

    p_no = sub.add_parser("reject", help="reject candidates")
    p_no.add_argument("ids", nargs="+")
    p_no.add_argument("--reason", default="")

    p_ed = sub.add_parser("edit", help="rewrite a pending candidate's text")
    p_ed.add_argument("id")
    p_ed.add_argument("--text", required=True)

    p_post = sub.add_parser("post", help="send approved candidates")
    p_post.add_argument("--dry-run", action="store_true")
    p_post.add_argument("--limit", type=int)

    p_mark = sub.add_parser("mark-posted", help="record a post sent by hand/browser")
    p_mark.add_argument("id")
    p_mark.add_argument("--tweet-id", required=True)

    sub.add_parser("status", help="caps, counters, kill switch")

    p_kill = sub.add_parser("kill", help="latch posting off")
    p_kill.add_argument("--reason", default="manual kill")
    sub.add_parser("resume", help="clear the kill latch and circuit breaker")

    args = parser.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "list":
        found = rows(args.status)
        if not found:
            print(f"No {args.status} candidates.")
            return 0
        for row in found:
            print(_fmt_row(row))
        return 0

    if cmd == "show":
        for row in rows("all"):
            if row["id"] == args.id:
                print(_fmt_row(row, full=True))
                print("\n  notices:")
                for k in row.get("keys", []):
                    print(f"    {k}")
                return 0
        print(f"No candidate {args.id}", file=sys.stderr)
        return 1

    if cmd == "approve":
        if not args.ids and not args.all:
            print("Give ids, or --all for every pending row.", file=sys.stderr)
            return 2
        n, refused = approve(args.ids if args.ids else [])
        print(f"Approved {n} candidate(s).")
        return _report_refused(refused, "approve")

    if cmd == "reject":
        n, refused = reject(args.ids, reason=args.reason)
        print(f"Rejected {n} candidate(s).")
        return _report_refused(refused, "reject")

    if cmd == "edit":
        try:
            row = edit_text(args.id, args.text)
        except (KeyError, ValueError) as e:
            print(f"Cannot edit: {e}", file=sys.stderr)
            return 1
        print(_fmt_row(row, full=True))
        return 0

    if cmd == "post":
        res = post_approved(limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0

    if cmd == "mark-posted":
        try:
            mark_posted(args.id, args.tweet_id)
        except KeyError:
            print(f"No candidate {args.id}", file=sys.stderr)
            return 1
        print(f"Recorded {args.id} as posted ({args.tweet_id}).")
        return 0

    if cmd == "status":
        state = _load_state()
        disabled, reason = posting_disabled()
        counts = {s: len(rows(s)) for s in STATUSES}
        print(json.dumps({
            "transport": transport(),
            "auto_post": auto_post_enabled(),
            "include_link": include_link(),
            "credentials_set": all(_x_config()),
            "disabled": disabled, "disabled_reason": reason,
            "posts_today": state.get("posts_today", 0),
            "max_per_day": MAX_POSTS_PER_DAY,
            "total_posted": state.get("total_posted", 0),
            "queue": counts,
        }, indent=2))
        return 0

    if cmd == "kill":
        state = _load_state()
        state["disabled"] = True
        state["disabled_reason"] = args.reason
        _save_state(state)
        print(f"Posting latched OFF: {args.reason}")
        return 0

    if cmd == "resume":
        state = _load_state()
        state["disabled"] = False
        state["disabled_reason"] = ""
        state["circuit_open_until"] = None
        _save_state(state)
        print("Posting re-enabled.")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
