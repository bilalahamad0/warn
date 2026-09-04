# @USLayoff — automated X posting

When a pipeline run detects new WARN notices, a few of them are news: a big
employer, or a big number. This stage composes one post per company — combining
that company's headcount across **every city and state in the same run** — puts
it in a queue, and waits for a human to say yes. Once the queue has been right
for long enough, one repository variable turns the waiting off.

- **Account:** <https://x.com/USLayoff>
- **Modules:** `warn_names.py` · `warn_brands.py` · `warn_x_select.py` · `warn_x.py`
- **Hooked in at:** `warn_publish.run()` → `maybe_post_to_x()`, after the email
  loop and before the monthly digest

---

## The one idea that matters

**The review gate and the automation are the same code path.** The pipeline
never posts. It only ever *composes and enqueues*. Posting reads rows whose
status is `approved`. In review mode a human writes that word; in auto mode
`X_AUTO_POST=1` writes it. Nothing else changes — so the text you approved in
phase 1 is byte-for-byte the text auto mode sends in phase 2.
`tests/test_x.py::test_the_gate_does_not_change_a_single_byte_of_the_post`
is the guard.

---

## Phase 1 — review every post by hand (where you are now)

CI runs twice daily, stages candidates into `data/x_queue.json`, and commits
them. **No X credentials are needed, and none are in GitHub.** You review and
post from your laptop.

```bash
git pull                                  # CI staged this run's candidates
python3 warn_x.py list                    # what is waiting
python3 warn_x.py show a3f19c2b8e04       # full text + the source notices
python3 warn_x.py approve a3f19c2b8e04 7c1d9e440ab2
python3 warn_x.py post --dry-run          # exactly what would be sent
python3 warn_x.py post                    # send them
git add data/ && git commit -m "x: post 2 notices" && git push
```

Reject anything that reads wrong, and say why — the reason is the record of
what the selection rules got wrong:

```bash
python3 warn_x.py reject a3f19c2b8e04 --reason "contractor, not the brand"
python3 warn_x.py edit a3f19c2b8e04 --text "..."   # rewrite; stays pending
```

**What to look for when reviewing.** Is this actually the company named, or a
contractor working at its site? Is the combined number right? Would a stranger
reading only this post be misled?

---

## Phase 2 — hand it over

Nothing to deploy. Set repository **variables**:

| Variable | Value | Effect |
|---|---|---|
| `X_TRANSPORT` | `api` | actually send, instead of only queueing |
| `X_AUTO_POST` | `1` | the pipeline approves its own candidates |

and repository **secrets** `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`,
`X_ACCESS_SECRET`. Reverting is `X_AUTO_POST=0`. The instant stop that needs no
deploy at all is `python3 warn_x.py kill --reason "..."`, or the repository
variable `X_DISABLE_POSTING=1`.

**Auto mode still refuses to approve four shapes**, because these are where the
composed text is most likely to be wrong. They stay pending for a human forever:

- the post exceeds 280 characters (`over_budget`)
- the state published no headcount, so the post can state no number
  (`employees_unknown`)
- **some** sites reported a headcount and others did not (`partial_headcount`) —
  the number is true but covers fewer places than the filing does
- more than 6 sites in one batch — wide batches are where grouping bugs surface
- 2,000 or more employees — double the 99th percentile, worth a human look

---

## What gets posted

Two arms, either of which fires:

| Arm | Rule | Why |
|---|---|---|
| **size** | combined ≥ **250** | ~93rd percentile of grouped events; ~21 posts/month. At ≥100 it is 68/month, a firehose. |
| **brand** | in `warn_brands.py` and ≥ **50** | a recognisable name is news at a size an unknown LLC is not |
| **tech** | tagged `tech` and ≥ **25** | "especially technology". The bar has to be *below* the brand bar or the arm is dead code — every tech brand is a registered brand. Measured over the last 365 days, 25 adds 8 events the brand arm misses: Microsoft 42, Qualcomm 38, Western Digital 47, Uber 41 and 29, Panasonic 46, Illumina 28, Riot Games 26. |

Measured against the last 365 days of the national dataset, that is **≈52 posts
a month, 1.7 a day** — comfortably under the caps below, with the busiest month
at 69. Raise `BRAND_THRESHOLD` to 100 for ≈38/month if that reads too busy.

Then two dampers: at most **2 posts per company per month** (Rite Aid filed 180
notices for 2,385 people, 13 at a time), and at most **6 posts per run / 12 per
day**.

The two notices that prompted this feature:

```
Fifth Third Bank filed a WARN notice for 234 job cuts in Oakland, MI.

Effective Sep 11, 2026.

#layoffs #Michigan

https://bilalahamad0.github.io/warn/
```

```
AT&T filed a WARN notice for 138 job cuts in Cumberland County, NJ.

Effective Sep 15, 2026.

#layoffs #NewJersey

https://bilalahamad0.github.io/warn/
```

Both fire on the brand/tech arm — 234 and 138 are below the size threshold.
A company appearing in several places in one run becomes **one** post:

```
Amazon filed WARN notices for 1,240 job cuts across 4 sites in 3 states.

CA 610 · TX 380 · NJ 250

First effective date Oct 1, 2026.

#layoffs
```

Beyond 8 states the breakdown moves to a self-reply (X confirmed on
2026-02-24 that replying to your own posts still works).

### Wording rules, and why

- **"filed a WARN notice for N job cuts"**, never "is cutting N jobs". A WARN
  notice announces *planned* layoffs and some are withdrawn.
- **Never `@mention` a company.** X blocks programmatic mentions in normal
  posts (2026-02-23 anti-spam change), so a company filed as `@Home Depot`
  would fail the whole post. A leading `@` is stripped.
- **Absence is never zero.** Hawaii and Oklahoma publish no headcount;
  Arizona, Illinois, Oklahoma and Utah publish no effective date. The post says
  "not reported" rather than implying a number.
- **Refuse, never truncate.** A post cut mid-number states a wrong figure.

---

## The company grouping problem

One employer files under many spellings — `AT&T`, `AT&T CORP.`, `At & T`,
`AT&T Alabama`, `AT&T - 5001`. Getting this wrong means a wrong headline
number. Two layers:

1. **`warn_brands.py`** — a curated registry of employers the public
   recognises, matched by anchored regex with explicit negative guards. Every
   pattern was run against all 40,956 distinct company strings in the dataset
   and its matches read by hand. This is what makes `Apple` not match
   `Apple Valley Medical Center`, `Target` not match `Target Logistics`, and
   `Compass Group` not match `Compass Minerals`.
2. **`warn_names.normalize`** — conservative fallback for everyone else. It
   strips legal forms (`Inc`, `LLC`, `Corp`) but deliberately **not**
   `Group`, `Holdings`, `USA` or `Services`: stripping those merges
   `Enterprise Products` with `Enterprise Rent-A-Car`, which are different
   employers.

**The contractor rule is the one to never loosen.**
`Flagship Facility Services Inc. at Meta Platforms Inc.` is Flagship's layoff —
a janitorial contractor losing a contract — not Meta's. Posting "Meta filed a
WARN notice for N job cuts" from that row is the worst factual error this
system can make. `warn_names._SPLIT_AT` keeps the left-hand employer, and
`warn_brands.resolve` vetoes a brand that only appears after ` at `, `dba`,
`@` or an operator suffix.

---

## Costs and limits (as of 2026)

X has been **pay-per-use since 2026-02-06** — there is no free tier, and no new
Basic/Pro signups. Buy credits at <https://console.x.com>; requests are blocked
at a zero balance.

| Item | Price |
|---|---|
| `POST /2/tweets` | **$0.015** per post |
| the same post containing a URL | **$0.200** per post |

At ~20-25 posts/month that is about **$0.40/month** without the dashboard link
or **$5/month** with it. The link is on by default; `X_INCLUDE_LINK=0` drops it.

Rate limits (100 posts/15 min per user, 10,000/24h per app) are far above
anything this feed does. The binding limits are ours: 6 per run, 12 per day,
75 seconds between posts.

**Turn on the "Automated" account label** on @USLayoff and name a
human-managed parent account in the bio. X staff cite this directly as what
keeps an automated account from being limited or suspended. A factual
public-record layoff feed sits squarely inside X's permitted "informational /
news feeds" category.

---

## When something goes wrong

| Signal | What happens automatically | What you do |
|---|---|---|
| 403 `duplicate content` | treated as **success** — the notice is already public | nothing |
| 403 anything else | circuit breaker opens for 24h, batch stops | check the account for warnings before `warn_x.py resume` |
| 401 | circuit opens, batch stops, never retried | regenerate the keys |
| 429 | batch stops, deferred to the next run | nothing — the ledger makes it safe |
| 5xx | row marked `failed`, batch stops | re-approve it when you want it retried |

**`posted` is terminal.** Nothing can hand a live tweet back to the transport —
`approve` refuses a posted row and says so rather than silently doing nothing.
`failed` and `expired` rows *can* be re-approved by a human.

**A failed row never auto-retries.** odishanow20's own safety doc says exactly
this, but its code allowed nine attempts per post and its non-retryable list
matched neither `forbidden` nor `429` — the two errors the doc names. Here a
failure is terminal until a human re-approves.

```bash
python3 warn_x.py status      # caps, counters, circuit breaker, credentials
python3 warn_x.py kill --reason "403 storm"
python3 warn_x.py resume
```

---

## Files

| File | Contents |
|---|---|
| `data/x_queue.json` | every candidate and its state |
| `data/x_posted_keys.json` | notices already posted — the dedupe ledger |
| `data/x_rejected_keys.json` | notices a human said no to. Separate from the queue because rejected rows are swept after 30 days and the "no" has to outlive them — otherwise on day 31 the same candidate is re-derived and, in auto mode, posted. |
| `data/x_state.json` | daily counters, kill latch, circuit breaker |
| `data/x_outbox.json` | approved text staged for the browser transport |

All four live under `data/` **and that is load-bearing**:
`warn_publish.commit_ledgers` and monitor.yml's failure branch stage `data/`
alone. A ledger written anywhere else is lost with the CI workspace, and the
next run re-posts every notice, twice a day, forever. (odishanow20 kept its
upload history in a gitignored directory; its 2-per-day cap silently reset on
every runner.)

The ledger is written **only after a post lands**, and records **every** notice
key in the batch — a four-site Amazon post must suppress all four next run.
