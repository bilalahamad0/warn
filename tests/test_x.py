"""Tests for warn_x — the @USLayoff queue, review gate, safety and ledgers.

Nothing here touches the network. The transport is patched at
``warn_x._post_via_api``, the way tests/test_notify.py patches
``warn_notify.smtplib.SMTP_SSL``, so every path below exercises the real queue
state machine and the real ledger writes.
"""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import warn_x
import warn_x_select
from warn_x_select import Notice


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every X_* var.

    monitor.yml exports X secrets into its pytest step while tests.yml exports
    none, and a developer laptop has a .env — without this the same test can
    behave three different ways.
    """
    for key in (
        "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET",
        "X_TRANSPORT", "X_AUTO_POST", "X_DISABLE_POSTING", "X_INCLUDE_LINK",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect every warn_x data file into a tmp dir."""
    monkeypatch.setattr(warn_x, "DATA_DIR", tmp_path)
    return tmp_path


def notice(state="CA", company="Widget Corp", employees=400,
           effective="2026-10-01", key=None, **kw):
    return Notice(
        state=state, company=company, employees=employees,
        notice_date=None, effective_date=effective,
        key=key or f"{state}:{company}__{effective}__{employees}", **kw
    )


def drafts_for(notices, include_link=False):
    """The real select+compose path, so tests exercise production wording."""
    out = []
    for batch in warn_x_select.group_by_company(notices):
        verdict = warn_x_select.score(batch)
        if verdict.post:
            out.append(
                (batch, verdict, warn_x_select.compose(batch, include_link))
            )
    return out


# ---------------------------------------------------------------------------
# Queue: enqueue, identity, upsert
# ---------------------------------------------------------------------------

def test_enqueue_stages_without_posting(data_dir):
    """The default path composes and queues. It must never send."""
    with patch("warn_x._post_via_api") as mock_post:
        summary = warn_x.enqueue(drafts_for([notice()]))
    assert summary["added"] == 1
    assert summary["auto_approved"] == 0
    assert not mock_post.called
    assert warn_x.rows("pending")[0]["status"] == "pending"


def test_rerun_upserts_rather_than_duplicating(data_dir):
    """Feed churn re-derives the same batch twice a day; it must not re-queue.

    The id is sha1(canonical + the exact notice keys), so an identical batch
    collapses onto the existing row.
    """
    batch = drafts_for([notice()])
    warn_x.enqueue(batch)
    second = warn_x.enqueue(drafts_for([notice()]))
    assert second["added"] == 0
    assert len(warn_x.rows("all")) == 1


def test_a_new_site_makes_a_new_candidate(data_dir):
    """A company that gains a location is a different, larger story."""
    warn_x.enqueue(drafts_for([notice()]))
    grown = drafts_for([notice(), notice(state="TX", key="TX:b")])
    assert warn_x.enqueue(grown)["added"] == 1


def test_already_posted_notices_are_not_requeued(data_dir):
    """The dedupe ledger is what stops a re-post, not the queue's memory."""
    only = notice()
    warn_x.record_posted_keys([only.key])
    assert warn_x.enqueue(drafts_for([only]))["added"] == 0


# ---------------------------------------------------------------------------
# The review gate
# ---------------------------------------------------------------------------

def test_auto_approve_off_by_default(data_dir):
    warn_x.enqueue(drafts_for([notice()]))
    assert warn_x.rows("approved") == []


def test_auto_approve_marks_rows_approved(data_dir):
    summary = warn_x.enqueue(drafts_for([notice()]), auto_approve=True)
    assert summary["auto_approved"] == 1
    assert warn_x.rows("approved")[0]["approved_by"] == "auto"


@pytest.mark.parametrize(
    "notices,blocker",
    [
        # A batch spanning many sites is where a grouping bug shows up.
        ([notice(state=s, key=f"{s}:x", employees=60)
          for s in ("CA", "TX", "NY", "FL", "OH", "GA", "NC")], "sites"),
        # Twice the p99 grouped event — a human should see it first.
        ([notice(employees=5000)], "employees"),
    ],
)
def test_auto_approval_is_withheld_from_the_risky_cases(data_dir, notices, blocker):
    """Auto mode is a default, not an override.

    These stay pending even with X_AUTO_POST=1, because they are exactly the
    shapes where the composed text is most likely to be wrong.
    """
    warn_x.enqueue(drafts_for(notices), auto_approve=True)
    assert warn_x.rows("approved") == []
    assert len(warn_x.rows("pending")) == 1


def test_approve_and_reject_move_rows(data_dir):
    warn_x.enqueue(drafts_for([notice()]))
    rid = warn_x.rows("pending")[0]["id"]
    assert warn_x.approve([rid]) == (1, [])
    assert warn_x.rows("approved")[0]["id"] == rid
    assert warn_x.reject([rid], reason="contractor") == (1, [])
    assert warn_x.rows("rejected")[0]["rejected_reason"] == "contractor"


def test_a_rejected_row_is_never_resurrected(data_dir):
    """The whole point of the gate: 'no' has to stick across runs."""
    warn_x.enqueue(drafts_for([notice()]))
    warn_x.reject([warn_x.rows("pending")[0]["id"]], reason="no")
    warn_x.enqueue(drafts_for([notice()]), auto_approve=True)
    assert [r["status"] for r in warn_x.rows("all")] == ["rejected"]


def test_edit_revalidates_length(data_dir):
    warn_x.enqueue(drafts_for([notice()]))
    rid = warn_x.rows("pending")[0]["id"]
    row = warn_x.edit_text(rid, "x" * 300)
    assert row["weighted_len"] == 300
    assert "over_budget" in row["warnings"]
    row = warn_x.edit_text(rid, "short and true")
    assert "over_budget" not in row["warnings"]


def test_edit_refuses_a_row_that_has_left_pending(data_dir):
    warn_x.enqueue(drafts_for([notice()]))
    rid = warn_x.rows("pending")[0]["id"]
    warn_x.approve([rid])
    with pytest.raises(ValueError):
        warn_x.edit_text(rid, "too late")


# ---------------------------------------------------------------------------
# Posting: the ledger discipline
# ---------------------------------------------------------------------------

def _approved(data_dir, n=notice):
    warn_x.enqueue(drafts_for([n()]))
    warn_x.approve([warn_x.rows("pending")[0]["id"]])


def test_a_successful_post_records_every_key(data_dir, monkeypatch):
    """A multi-site post covers several notices; all must be suppressed next run."""
    monkeypatch.setenv("X_TRANSPORT", "api")
    notices = [notice(), notice(state="TX", key="TX:b", employees=300)]
    warn_x.enqueue(drafts_for(notices))
    warn_x.approve([warn_x.rows("pending")[0]["id"]])
    with patch("warn_x._post_via_api", return_value="1234567890"):
        result = warn_x.post_approved()
    assert result["posted"] == 1
    assert warn_x.load_posted_keys() == {n.key for n in notices}
    assert warn_x.rows("posted")[0]["tweet_id"] == "1234567890"


def test_a_failed_post_leaves_the_ledger_untouched(data_dir, monkeypatch):
    """A failed send must retry next run, never be silently swallowed."""
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    with patch("warn_x._post_via_api",
               side_effect=warn_x.PostError("boom", kind="transient")):
        result = warn_x.post_approved()
    assert result["failed"] == 1
    assert warn_x.load_posted_keys() == set()
    assert not (data_dir / warn_x.POSTED_NAME).exists()
    assert warn_x.rows("failed")[0]["last_error"].startswith("boom")


def test_a_failed_row_never_auto_retries(data_dir, monkeypatch):
    """odishanow20's own guidance, which its code did not implement.

    There, Stage(attempts=3) around _with_retry(attempts=3) allowed nine
    attempts, and the non-retryable token list matched neither 'forbidden' nor
    '429' — the two errors its safety doc says must never be retried.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    with patch("warn_x._post_via_api",
               side_effect=warn_x.PostError("boom", kind="transient")) as post:
        warn_x.post_approved()
        warn_x.post_approved()          # a second run must not pick it up
    assert post.call_count == 1


def test_duplicate_content_counts_as_posted(data_dir, monkeypatch):
    """403 'duplicate content' means the notice is already public.

    v2 returns problem+json with a detail string and no numeric code — there is
    no v1.1 code 187 to match, so classification reads the text.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    err = warn_x.PostError(
        "403 Forbidden: You are not allowed to create a Tweet with "
        "duplicate content.",
        kind="duplicate",
    )
    with patch("warn_x._post_via_api", side_effect=err):
        result = warn_x.post_approved()
    assert result["posted"] == 1
    assert warn_x.rows("posted")[0]["tweet_id"] == "duplicate"
    assert warn_x.load_posted_keys()


@pytest.mark.parametrize("body,kind", [
    ("You are not allowed to create a Tweet with duplicate content.", "duplicate"),
    ("Reply to this conversation is not allowed because you have not been "
     "mentioned or otherwise engaged by the author", "policy"),
    ("Your account is temporarily restricted", "restricted"),
])
def test_403_bodies_are_classified(body, kind):
    assert warn_x._classify_403(body) == kind


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------

def test_missing_credentials_never_raise(data_dir, monkeypatch):
    """A machine with no X keys must still complete a full data run.

    And it must latch NOTHING: an unconfigured laptop running the pipeline
    cannot be allowed to open a 24h circuit breaker that then blocks the CI
    run which does have credentials. The row stays approved and goes out on
    the first configured run.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    result = warn_x.post_approved()          # no keys set by _clean_env
    assert result["posted"] == 0
    assert result["stopped"] == "unconfigured"
    assert warn_x.load_posted_keys() == set()
    assert warn_x.rows("approved")           # still queued, not failed
    assert warn_x.posting_disabled()[0] is False


def test_kill_switch_env_blocks_everything(data_dir, monkeypatch):
    monkeypatch.setenv("X_TRANSPORT", "api")
    monkeypatch.setenv("X_DISABLE_POSTING", "1")
    _approved(data_dir)
    with patch("warn_x._post_via_api") as post:
        result = warn_x.post_approved()
    assert not post.called
    assert "disabled" in result["stopped"]


def test_kill_latch_survives_in_the_state_file(data_dir):
    warn_x.main(["kill", "--reason", "403 storm"])
    disabled, reason = warn_x.posting_disabled()
    assert disabled and "403 storm" in reason
    warn_x.main(["resume"])
    assert warn_x.posting_disabled()[0] is False


def test_a_403_opens_the_circuit_breaker(data_dir, monkeypatch):
    """Their rule: on a 403, stop for 24-48h. Never hammer a restricted account."""
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    with patch("warn_x._post_via_api",
               side_effect=warn_x.PostError("403", kind="restricted")):
        warn_x.post_approved()
    assert warn_x.posting_disabled()[0] is True
    assert "circuit breaker" in warn_x.posting_disabled()[1]


def test_rate_limit_stops_the_batch_without_sleeping(data_dir, monkeypatch):
    """429 defers to the next run — the ledger makes that safe.

    Sleeping until x-rate-limit-reset inside a GitHub Actions run would block
    the job for up to 15 minutes and cascade into every later step.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    warn_x.enqueue(drafts_for([notice(), notice(state="TX", company="Other Co",
                                                key="TX:o")]))
    warn_x.approve([r["id"] for r in warn_x.rows("pending")])
    with patch("warn_x._post_via_api",
               side_effect=warn_x.PostError("429", kind="rate")) as post, \
         patch("warn_x.time.sleep") as sleep:
        result = warn_x.post_approved()
    assert post.call_count == 1              # stopped after the first failure
    assert result["stopped"] == "rate"
    assert not sleep.called


def test_daily_cap_is_enforced(data_dir, monkeypatch):
    monkeypatch.setenv("X_TRANSPORT", "api")
    state = warn_x._load_state()
    state["day"] = warn_x._now().strftime("%Y-%m-%d")
    state["posts_today"] = warn_x.MAX_POSTS_PER_DAY
    warn_x._save_state(state)
    _approved(data_dir)
    with patch("warn_x._post_via_api") as post:
        result = warn_x.post_approved()
    assert not post.called
    assert result["stopped"] == "daily cap"


def test_posts_are_spaced_apart(data_dir, monkeypatch):
    """60-90s spacing is what actually fixed odishanow20's 403s."""
    monkeypatch.setenv("X_TRANSPORT", "api")
    warn_x.enqueue(drafts_for([
        notice(), notice(state="TX", company="Other Co", key="TX:o"),
    ]))
    warn_x.approve([r["id"] for r in warn_x.rows("pending")])
    with patch("warn_x._post_via_api", return_value="1"), \
         patch("warn_x.time.sleep") as sleep:
        warn_x.post_approved()
    sleep.assert_called_once_with(warn_x.MIN_SECONDS_BETWEEN_POSTS)


def test_a_company_cannot_flood_the_month(data_dir):
    """Rite Aid filed 180 notices for 2,385 people, 13 at a time."""
    state = warn_x._load_state()
    state["per_company_month"] = {
        warn_x._month_key(): {
            warn_x_select.warn_names.canonical("Widget Corp"):
                warn_x_select.MAX_POSTS_PER_COMPANY_PER_MONTH
        }
    }
    warn_x._save_state(state)
    assert warn_x.enqueue(drafts_for([notice()]))["added"] == 0


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

def test_dry_transport_sends_nothing(data_dir, monkeypatch):
    """Explicit, because the DEFAULT is no longer dry.

    Without setting it this test would exercise the browser path and still
    pass every assertion below — which is exactly how a test stops testing
    what its name says.
    """
    monkeypatch.setenv("X_TRANSPORT", "dry")
    _approved(data_dir)
    with patch("warn_x._post_via_api") as post:
        result = warn_x.post_approved()
    assert not post.called
    assert result["posted"] == 0
    assert warn_x.load_posted_keys() == set()
    assert not (data_dir / warn_x.OUTBOX_NAME).exists()


def test_browser_is_the_default_transport(monkeypatch):
    """The API costs money and this account has a $0.00 balance."""
    monkeypatch.delenv("X_TRANSPORT", raising=False)
    assert warn_x.transport() == "browser"


def test_an_unknown_transport_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("X_TRANSPORT", "carrier-pigeon")
    assert warn_x.transport() == "browser"


def test_browser_transport_stages_an_outbox(data_dir, monkeypatch):
    """The browser path cannot confirm delivery, so it must not write a ledger.

    Only mark-posted does that — a post that never went out is never recorded
    as sent.
    """
    monkeypatch.setenv("X_TRANSPORT", "browser")
    _approved(data_dir)
    warn_x.post_approved()
    outbox = json.loads((data_dir / warn_x.OUTBOX_NAME).read_text())
    assert len(outbox["posts"]) == 1
    assert warn_x.load_posted_keys() == set()
    assert warn_x.rows("approved")                       # still awaiting proof

    rid = outbox["posts"][0]["id"]
    warn_x.mark_posted(rid, "1999")
    assert warn_x.rows("posted")[0]["tweet_id"] == "1999"
    assert warn_x.load_posted_keys()


# ---------------------------------------------------------------------------
# The pipeline entry point
# ---------------------------------------------------------------------------

def _state_results():
    return {
        "mi": {"state": "MI", "diff": {
            "new_count": 1,
            "new_keys": ["Fifth Third Bank__2026-09-11__234"],
            "new_entries": [{
                "company": "Fifth Third Bank", "employees": 234,
                "notice_date": None, "effective_date": "2026-09-11",
                "county": "Oakland", "city": "",
            }],
        }},
    }


def test_run_stage_queues_but_does_not_post_by_default(data_dir):
    with patch("warn_x._post_via_api") as post:
        summary = warn_x.run_stage(_state_results())
    assert summary["added"] == 1
    assert not post.called
    assert "review gate" in str(summary["post"]["skipped"])


def test_run_stage_posts_once_the_gate_is_flipped(data_dir, monkeypatch):
    """Flipping X_AUTO_POST must change WHO approves, and nothing else."""
    monkeypatch.setenv("X_AUTO_POST", "1")
    monkeypatch.setenv("X_TRANSPORT", "api")
    with patch("warn_x._post_via_api", return_value="42") as post:
        summary = warn_x.run_stage(_state_results())
    assert summary["post"]["posted"] == 1
    text = post.call_args.args[0] if post.call_args.args else \
        post.call_args.kwargs["text"]
    assert "Fifth Third Bank" in text and "234" in text


def test_the_gate_does_not_change_a_single_byte_of_the_post(data_dir, monkeypatch):
    """The invariant that makes the phase-1 review meaningful.

    Whatever a human reads and approves in review mode is byte-for-byte what
    auto mode sends. If this ever fails, the review gate has been testing
    different copy than the account publishes.
    """
    warn_x.run_stage(_state_results())
    reviewed = warn_x.rows("pending")[0]["text"]

    monkeypatch.setattr(warn_x, "DATA_DIR", data_dir / "auto")
    (data_dir / "auto").mkdir()
    monkeypatch.setenv("X_AUTO_POST", "1")
    monkeypatch.setenv("X_TRANSPORT", "api")
    with patch("warn_x._post_via_api", return_value="42") as post:
        warn_x.run_stage(_state_results())
    sent = post.call_args.args[0] if post.call_args.args else \
        post.call_args.kwargs["text"]
    assert sent == reviewed


# ---------------------------------------------------------------------------
# Threads — the cases that let a live tweet go unrecorded
# ---------------------------------------------------------------------------

def _threaded_row(data_dir):
    """One approved row whose batch is wide enough to need a self-reply."""
    notices = [
        notice(state=s, company="Widespread Co", employees=100 + i,
               key=f"{s}:widespread")
        for i, s in enumerate(
            ["TX", "CA", "FL", "NY", "OH", "GA", "NC", "VA", "MD", "AZ"]
        )
    ]
    warn_x.enqueue(drafts_for(notices))
    row = warn_x.rows("pending")[0]
    assert row["thread"], "expected an overflow self-reply"
    warn_x.approve([row["id"]])
    return row


def test_a_live_root_is_recorded_even_when_its_reply_fails(data_dir, monkeypatch):
    """The root tweet is PUBLIC the moment it has an id.

    A failing self-reply used to jump to the except block, which marked the row
    failed and wrote no ledger — so a human re-approving it sent the live root
    a second time. Only X's own duplicate-content window stopped it.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    row = _threaded_row(data_dir)

    calls = []

    def fake(text, in_reply_to=None):
        calls.append(in_reply_to)
        if in_reply_to:
            raise warn_x.PostError("reply blew up", kind="transient")
        return "111"

    with patch("warn_x._post_via_api", side_effect=fake), \
         patch("warn_x.time.sleep"):
        result = warn_x.post_approved()

    assert result["posted"] == 1
    assert warn_x.rows("posted")[0]["tweet_id"] == "111"
    assert warn_x.load_posted_keys() == set(row["keys"])
    assert calls == [None, "111"]


def test_the_daily_cap_counts_tweets_not_rows(data_dir, monkeypatch):
    """A threaded row sends 1 + len(thread) tweets.

    Counting one per ROW let six threaded rows put twelve tweets on the wire
    against a twelve-per-day cap — and the next run started from six.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    _threaded_row(data_dir)
    with patch("warn_x._post_via_api", return_value="1"), \
         patch("warn_x.time.sleep"):
        warn_x.post_approved()
    assert warn_x._load_state()["posts_today"] == 2      # root + one reply


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------

def test_a_posted_row_can_never_be_re_approved(data_dir, monkeypatch):
    """`list --status all` prints posted ids and `approve <id>` took them.

    A tweet that is public cannot be un-published, so nothing may hand a
    posted row back to the transport.
    """
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    with patch("warn_x._post_via_api", return_value="1"):
        warn_x.post_approved()
    rid = warn_x.rows("posted")[0]["id"]

    moved, refused = warn_x.approve([rid])
    assert moved == 0
    assert refused, "the CLI must say what it refused, not silently no-op"
    assert warn_x.rows("approved") == []
    assert warn_x.rows("posted")[0]["id"] == rid

    with patch("warn_x._post_via_api") as post:
        warn_x.post_approved()
    assert not post.called


def test_a_failed_row_may_be_re_approved_by_a_human(data_dir, monkeypatch):
    """Terminal means posted. A transport failure is recoverable."""
    monkeypatch.setenv("X_TRANSPORT", "api")
    _approved(data_dir)
    with patch("warn_x._post_via_api",
               side_effect=warn_x.PostError("5xx", kind="transient")):
        warn_x.post_approved()
    rid = warn_x.rows("failed")[0]["id"]
    moved, refused = warn_x.approve([rid])
    assert (moved, refused) == (1, [])
    assert warn_x.rows("approved")[0]["id"] == rid


def test_a_rejection_outlives_the_queue_row_that_carried_it(data_dir):
    """_expire_stale sweeps rejected rows after 30 days, and enqueue's only
    memory of "no" was the row still being in the file — so on day 31 the same
    candidate came back and, in auto mode, posted text a human had refused."""
    warn_x.enqueue(drafts_for([notice()]))
    warn_x.reject([warn_x.rows("pending")[0]["id"]], reason="contractor")
    assert warn_x.load_rejected_keys()

    # Age the row past the sweep and drop it, as a later run would.
    queue = warn_x._load_queue()
    old = (warn_x._now() - timedelta(days=warn_x.SWEEP_AFTER_DAYS + 1))
    queue["candidates"][0]["created_at"] = old.isoformat()
    warn_x._save_queue(queue)
    warn_x._expire_stale(queue)
    warn_x._save_queue(queue)
    assert warn_x.rows("all") == []

    assert warn_x.enqueue(drafts_for([notice()]), auto_approve=True)["added"] == 0


def test_the_browser_outbox_is_marked_once_a_post_is_live(data_dir, monkeypatch):
    """x_outbox.json is the whole instruction set a browser session works from.

    Leaving a sent entry in it, indistinguishable from a fresh one, means any
    consumer that re-reads the file posts everything in it again.
    """
    monkeypatch.setenv("X_TRANSPORT", "browser")
    _approved(data_dir)
    warn_x.post_approved()
    rid = json.loads((data_dir / warn_x.OUTBOX_NAME).read_text())["posts"][0]["id"]

    warn_x.mark_posted(rid, "2001")
    entry = json.loads((data_dir / warn_x.OUTBOX_NAME).read_text())["posts"][0]
    assert entry.get("tweet_id") == "2001"
    assert entry.get("posted_at")


# ---------------------------------------------------------------------------
# Post cards
# ---------------------------------------------------------------------------

def test_the_browser_outbox_carries_a_rendered_card(data_dir, monkeypatch):
    """The image is what stops a thumb; the outbox is what the browser reads."""
    monkeypatch.setenv("X_TRANSPORT", "browser")
    _approved(data_dir)
    warn_x.post_approved()
    post = json.loads((data_dir / warn_x.OUTBOX_NAME).read_text())["posts"][0]
    assert post["image"], "no card rendered"
    card = Path(post["image"])
    assert card.exists() and card.suffix == ".png"
    assert card.stat().st_size > 5_000


def test_a_card_is_regenerated_not_committed(data_dir, monkeypatch):
    """Only the card's INPUTS live on the queue row.

    CI stages the queue twice a day; committing a ~100 KB PNG per candidate
    would add tens of megabytes a year to a repo whose diffs are meant to stay
    readable. The row carries the strings, the PNG is made at post time.
    """
    warn_x.enqueue(drafts_for([notice()]))
    row = warn_x.rows("pending")[0]
    assert set(row["card"]) == {"place", "effective", "employees"}
    assert "image" not in row
    assert not (data_dir / warn_x.CARDS_DIR).exists()


def test_a_card_failure_never_blocks_a_post(data_dir, monkeypatch):
    """The text is the story; the card is packaging."""
    monkeypatch.setenv("X_TRANSPORT", "browser")
    _approved(data_dir)
    with patch("warn_x_image.build_card", return_value=None):
        warn_x.post_approved()
    post = json.loads((data_dir / warn_x.OUTBOX_NAME).read_text())["posts"][0]
    assert post["image"] is None
    assert post["text"]


def test_the_card_says_the_same_thing_as_the_post(data_dir):
    """A card naming a different place or date than the text beside it is
    worse than no card at all."""
    warn_x.enqueue(drafts_for([
        notice(state="MI", company="Fifth Third Bank", employees=234,
               effective="2026-09-11", county="Oakland"),
    ]))
    row = warn_x.rows("pending")[0]
    assert row["card"]["effective"] == "Sep 11, 2026"
    assert row["card"]["effective"] in row["text"]
    assert row["card"]["employees"] == 234
    assert "Oakland" in row["card"]["place"]
