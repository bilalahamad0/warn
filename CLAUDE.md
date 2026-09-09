# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run full pipeline (download → diff → charts → publish → git push)
python3 warn_publish.py

# Run pipeline without pushing to git
python3 warn_publish.py --no-push

# Force re-download even if ETag unchanged
python3 warn_publish.py --force

# Run individual pipeline stages
python3 warn_monitor.py          # Download + parse XLSX only
python3 warn_diff.py             # Detect changes between runs
python3 warn_charts.py           # Regenerate the 12 Plotly charts
python3 warn_history.py          # Re-parse historical PDFs (2014-2024)
python3 warn_site_us.py          # Rebuild the US dashboard (docs/ — the site root)
python3 warn_notify.py --test    # Send a test email
python3 warn_digest.py           # Preview last month's US digest (prints text)
python3 warn_digest.py --year 2026 --month 6 --html /tmp/d.html   # HTML preview
python3 warn_publish.py --digest # Force-send the monthly digest now

# X / @USLayoff auto-posting (see X_POSTING.md)
python3 warn_x.py list           # candidates awaiting review
python3 warn_x.py show <id>      # full post text + its source notices
python3 warn_x.py approve <id>   # …then `post` sends it
python3 warn_x.py post --dry-run # exactly what would be sent
python3 warn_x.py post           # stages data/x_outbox.json + cards for the browser
python3 warn_x.py status         # caps, counters, kill switch, credentials
python3 warn_x.py kill --reason "403 storm"   # instant stop, no deploy
python3 warn_publish.py --no-post-x           # skip the X stage entirely

# Run all tests
pytest -v --cov=.

# Run a single test file
pytest tests/test_monitor.py -v

# Run a single test
pytest tests/test_monitor.py::test_fix_company_name -v

# Lint
flake8 .
```

## Architecture

**Pipeline flow** (orchestrated by `warn_publish.py`):

```
state feeds (online)
  → warn_sources/     → per-state Source modules (registry in __init__.py)
       ↳ ca.py          CA = EDD XLSX, grandfathered at data/*.json
       ↳ (new states)   data/states/<code>/… via StatePaths
       ↳ engine reuses warn_monitor.py (download/parse/diff/ledgers/persist,
         all path-parameterised; CA defaults unchanged)
  → warn_diff.py      → data/diff_report.md, data/changelog.jsonl
  → warn_history.py   → data/warn_all_years.json (merged 2014-present, CA)
  → warn_sources/aggregate.py → data/warn_national.json (all states, unified
         schema with `state` field; drives the US map chart)
  → warn_datasets.py  → the California record set, derived in memory from the
         national CA slice (see "California is derived" below)
  → warn_charts.py    → docs/charts/*.html (12 self-contained Plotly divs,
         incl. 12_us_map — filterable state choropleth)
  → warn_site_us.py   → docs/index.html (US dashboard — the SITE ROOT)
                        docs/data.json (national public API, "scope": "us")
                        docs/search_index.json, docs/pages/<ST>/N.json
                        docs/us/index.html (redirect stub → /warn/)
  → warn_publish.py   → docs/ca/index.html (California dashboard)
                        docs/ca/data.json (CA public API, "scope": "ca")
                      → warn_notify.py (Gmail alert per state when it has NEW
                          notices; an amendment alone is held and clubbed into
                          that state's next alert — see below)
                          ↳ warn_subscribers.py (fetch signup list → BCC subscribers)
                      → warn_x.py (X/@USLayoff: compose + queue notable posts;
                          ↳ warn_x_select.py  cross-state grouping, scoring, wording
                          ↳ warn_names.py / warn_brands.py  company canonicalisation
                          sends only once X_AUTO_POST=1 — see X_POSTING.md)
                      → git commit + push
```

**Site URL layout** lives in `warn_urls.py`, a leaf module every other module
imports for its links (never hardcode a path). `/warn/` is the US dashboard,
`/warn/ca/` is California, `/warn/us/` is a redirect stub kept for old
bookmarks and already-mailed non-CA alerts. **`/warn/unsubscribe.html` is
frozen** — every subscriber email ever sent carries an HMAC-signed link to it
and those live in inboxes indefinitely; `tests/test_notify.py` pins the exact
URL + signature as the guard. `warn_notify.US_DASHBOARD_URL` used to be derived
as `DASHBOARD_URL + "us/"`, which was silently correct only while California
sat at the root; the two are independent constants now.

**Which site build may fail** inverted when the national dashboard took over
the root, and `warn_publish.run()` step 5 encodes it. Whatever builds the root
page must be fatal — a non-zero exit skips the full `git_commit_push` here and
the success branch of CI's commit step, leaving the last good page published.
That guard used to sit on `build_site` (California was the root); it now sits
on `build_us_site`, and California — whose failure still leaves a live, correct
front page — is the non-fatal one. The raise happens *after* notifications and
the digest so a chart hiccup never costs a subscriber a legitimate alert.
**But a skipped commit must never discard the alert ledgers**: those sends
already happened, and their ledgers were written locally — lost with a CI
workspace, the next run re-detects the same notices and re-emails every
subscriber, every 12h, until the build is fixed. So the failure path commits
`data/` alone (never `docs/`): locally `warn_publish.commit_ledgers()`
(best-effort, `"auto: alert ledgers (site build failed) [skip ci]"`, skipped
under `--no-push`); in CI `monitor.yml`'s commit step runs on pipeline failure
too (gated on the pipeline step's outcome so a totally-failed run commits
nothing) and stages only `data/` with
`"auto: alert ledgers (pipeline failed) [skip ci]"`.

**`docs/pages/` is a reserved name.** `warn_site_us._write_pages` rmtrees
`out_dir/"pages"` wholesale each build, and `out_dir` is now the site root.
Sibling directories are untouched (`tests/test_site_us.py` guards it), but
nothing else may live at that path.

**California is derived, not fetched** (`warn_datasets.py`). The live EDD feed
(`data/warn_cumulative.json`) starts 2025-01-29, while
`data/historical/ca_national_history.json` — merged into the national dataset
by `warn_sources.aggregate` — holds 71 further CA notices dated 2025-01-03 →
2025-01-28 worth 5,475 employees. The merge dedupes nothing because the sets
are disjoint, so the US dashboard counted 827 California notices for 2025 while
the California dashboard, reading only the live feed, counted 756. Both
`warn_publish._dashboard_payload` and `warn_charts.load_data` now go through
`warn_datasets.load_ca_dashboard`, which slices California out of
`warn_national.json` from `CA_COVERAGE_START` (2025-01-01) and normalises every
record onto `CA_RECORD_FIELDS`. The boundary exists because pre-2025 CA records
carry **no `industry` at all** and only 85% county coverage — the industry
chart, industry filter and county filter would silently degrade across it — and
it is a calendar-year edge so the 2025 KPI year is whole and directly
comparable. The page states its span (`_build_coverage_note`) and flags a
degraded fallback. The invariant that keeps this honest is
`tests/test_datasets.py::test_no_covered_era_record_is_dropped`: every CA record
in the national dataset on or after the boundary must survive into the derived
payload. Nothing is persisted under `data/` — `docs/ca/data.json` is the
artifact.

**Year-over-year chart reads the national dataset** (`7_yoy_bar`, via
`warn_datasets.ca_yearly_summary`). It used to read `yearly_summary` from
`warn_all_years.json`, parsed out of EDD's fiscal-year PDFs, which capture 3-5%
of actual filings — FY2019-20 rendered as 17 notices / 851 employees where
5,143 notices covering 565,385 employees were filed, flattening California's
COVID spike to nothing. The bars were greyed and captioned as a partial sample,
so the chart was not dishonest, only useless. Three consequences worth knowing:

- **Calendar years, not fiscal.** The fiscal framing existed only because the
  source PDFs were fiscal-year documents, and it was already broken: the one
  "complete" bar, labelled `FY 2025-26 (Live)`, held 19 months spanning three
  fiscal years. Everything else on the site is calendar-year.
- **This series is NOT clipped at `CA_COVERAGE_START`.** That boundary protects
  the industry and county visuals, which need fields the backfill lacks; a
  year-over-year chart needs only `notice_date` and `employees`, present on
  100% of the 16,176 historical CA records.
- **Incomplete years are hatched, not hidden.** A year is incomplete if it is
  still running, or if any month recorded zero filings — California files
  18-500 notices a month, so an empty month is missing data, never a quiet
  month. Two such years exist today: **2014** (backfill starts in July) and
  **2025** (February, March and April are absent from the EDD feed *and* the
  backfill, an upstream gap). Charted flat, 2025's 827 notices against 2024's
  1,502 reads as a 45% collapse that never happened.

**Plotly version coupling**: chart divs are generated by plotly.py 6.x, which
emits base64 `bdata` arrays that plotly.js 2.x cannot decode — both dashboard
templates must reference a plotly.js 3.x CDN build (currently 3.5.0). If
plotly.py is upgraded across a major, bump the CDN reference in
`warn_publish.py` (SITE_HTML_TEMPLATE) and `warn_site_us.py` (US_TEMPLATE).

**Multi-state expansion**: research + phased rollout plan live in
`EXPANSION_RESEARCH.md`. Adding a state = one module in `warn_sources/`
implementing `fetch()` + `parse()` (port logic from Big Local News'
Apache-2.0 warn-scraper where available — vendor it into the module, never a
runtime dependency), plus a registry entry. Failure isolation is built in:
one state erroring never blocks the others (`warn_sources.run_all`).

**Email signups:** both dashboards' forms POST to a Google Apps Script Web App
(`automation/subscribe.gs`) that stores `{timestamp, name, email, source,
states}` in a Google Sheet. `warn_subscribers.py` reads that list (via
`SUBSCRIBERS_TOKEN`). The form endpoint (`SIGNUP_ENDPOINT`) is injected into
both pages at build time; if unset, the form degrades to a "not configured"
message. See README "Email Signups" for deployment. **Re-deploy the Apps
Script after changing `subscribe.gs`** (Manage deployments ▸ edit ▸ New
version) or the new column is never written.

**Unsubscribe** (`warn_unsubscribe.py` → `docs/unsubscribe.html`, rebuilt every
run): every subscriber email carries a per-recipient link signed with
`warn_subscribers.unsubscribe_signature` (HMAC-SHA256 of the lowercased
address keyed by `SUBSCRIBERS_TOKEN`, hex, 32 chars — the Apps Script
recomputes it with its identical `LIST_TOKEN`, so a link only works for the
address it was minted for). The page GETs `?action=prefs` to show current
subscriptions and POSTs `{action:'unsubscribe', e, s, states[], digest}`.
**An empty selection deletes the sheet row** — blanking the cell would
re-subscribe them to California via `DEFAULT_STATES`. Alerts carry
`List-Unsubscribe` but deliberately *not* `List-Unsubscribe-Post`: the
landing page is a static asset that cannot serve the RFC 8058 one-click
POST (see `warn_notify._build_message`). Because links are per-recipient,
subscriber mail is sent one message per address over a single SMTP
connection rather than one BCC blast.

**Subscription preferences** (the `states` sheet column, comma-separated):
- 2-letter codes = per-notice alerts for those states, routed by
  `warn_subscribers.subscribers_for_state` → `warn_notify.send_email(...,
  state=CODE)`. An Illinois notice only reaches Illinois subscribers.
- The sentinel `US` (`warn_subscribers.DIGEST_CODE`, never a state) = the
  whole-country **monthly digest** built by `warn_digest.build_monthly_digest`
  and sent by `warn_notify.send_monthly_digest`.
- A **blank cell means California** (`DEFAULT_STATES`) — subscribers who
  signed up before preferences existed keep exactly the alerts they had.
- **Signup is additive; only the preferences page removes.** `doPost`'s
  duplicate-address branch merges (`_mergeStates`) instead of overwriting the
  cell. Neither signup form loads the subscriber's current selection — the
  California form has no state picker, and the US dashboard's picker starts
  blank on every visit — so neither can show what a replace would destroy.
  Before this, picking IL+NY at `/warn/` and then subscribing at `/warn/ca/`
  silently cancelled Illinois and New York; so did returning to the US form
  and ticking one more state. Narrowing a subscription belongs to
  `warn_unsubscribe`'s page, which GETs `?action=prefs`, shows the current
  selection, and writes back exactly what was confirmed — destructive power
  sits on the one surface where the consequence is visible. A legacy blank
  cell is read as `CA` *before* merging, so an implicit California never
  vanishes. Guarded by `tests/test_subscribe_gs.py::
  test_no_signup_ever_shrinks_a_subscription`.
- `warn_publish.maybe_send_monthly_digest` runs every pipeline run but sends
  at most once per calendar month, guarded by `data/digest_sent.json` and
  recorded only after a successful send (same discipline as the notice
  ledgers). `--digest` forces a send; `--no-digest` skips.

**US dashboard search** (`docs/search_index.json`): a compact
`ST|Company|Place|dates|Emp` row index (~3.5 MB, ~1 MB gzipped) written at
build time and fetched by the browser **only after the first search
keystroke**, so company search pages through every matching record in the
dataset — combinable with the state filter — while normal browsing still
loads nothing extra.

**Key data files** (under `data/`):
- `warn_latest.json` — current WARN records from the live XLSX
- `warn_all_years.json` — CA records parsed from EDD's fiscal-year PDFs, plus a
  `yearly_summary`. The records still feed chart 8 (multi-year monthly overlay).
  **`yearly_summary` is no longer charted** — see the year-over-year note below.
- `warn_snapshot.json` — previous run state used by `warn_diff.py` for comparison
- `notified_keys.json` — cumulative ledger of every notice key already alerted on. `warn_monitor.detect_changes` keys "new" off this (not a single prior run) so the EDD feed's version churn — it intermittently flip-flops the record count across consecutive fetches — can't re-trigger emails for the same notices. Keys are recorded only after a successful send (`warn_publish` → `warn_monitor.record_notified_keys`).
- `amended_keys.json` — cumulative ledger of every notice already reported as *amended*. `detect_changes` recognises an amendment when a filing's *anchor* (company + county + city + notice_date, via `_anchor_key`) persists across runs but its `_notice_key` changes (EDD most often revises the effective date). Without this ledger the same single amendment is re-reported as "removed/amended" on every feed swing — the exact bug that put a phantom "⚠️ 1 previously filed notice removed/amended" line in every alert email. Keys are recorded only after a successful send (`warn_publish` → `warn_monitor.record_amended_keys`). The ledger also marks the canonical (post-amendment) version so `update_cumulative` can evict the superseded line and the dashboard never shows a notice twice. `removed_count` now counts only genuine withdrawals (a whole anchor gone from the feed), never a revision.
- `pending_amendments.json` — per-state ledger of amendments detected but not yet emailed. **An amendment does not send an email of its own while there is any prospect of clubbing it** (a hold that ages out is the one exception, below). Virginia's feed re-dated its rescinded AeroFarms notice every day (the "Impact Date" column carried the CSV's render date), so the pipeline mailed "1 Virginia layoff notice amended" every morning for weeks — each true, none news. `warn_publish.alert_for_state` emails when a state has a genuinely *new* notice, or when a held amendment has aged out (below); an amendment-only run parks its amendments here (`Source.hold_amendments` → `warn_monitor.merge_pending_amendments`) and the next new-notice alert for that state carries them, collapsed to **one row per filing** (earliest `old_*` → latest `new_*`) rather than one row per day. The row's `revisions` count is rendered in the email by `warn_notify._describe_amendment` ("effective date 2026-08-18 → 2026-08-31 (revised 13 times)") — without it a row collapsed from thirteen daily re-datings reads as a single correction. The collapse key is the *filing* (`_anchor_key`), never the notice key alone: two sites of one company revised to the same date and headcount share a `_notice_key` (Arizona's LUKE Holding, Tucson and Yuma, both to `None`/0) and must stay two rows. Discipline points that differ from the other ledgers: (1) the amendment's keys go into `notified_keys.json` *and* `amended_keys.json` at hold time, not after a send — delivery is guaranteed by this file, and the ledgers must stop the revised line being re-detected, which once `warn_latest.json` carries the revision would otherwise mean surfacing it as a brand-new notice (the pre-existing failure mode whenever an amendment's email failed to send); (2) the *new*-notice keys still wait for a successful send, so a failed alert retries next run with the held amendments still attached; (3) because this file is the only thing still owing those amendments an email, a corrupt copy is moved aside as `pending_amendments.json.corrupt` for manual recovery rather than silently overwritten by the next hold, and it is the one ledger written through a temp file + `os.replace` — a torn key ledger reads back empty and the notice is simply re-alerted, while a torn pending file loses rows whose keys are already recorded, which `detect_changes` can never surface again; (4) **`detect_changes` does not cap `amendments`** the way it caps `new_entries` and `removed_entries` at 50 — nothing can rebuild a cut amendment later (there is no `warn_latest.json` recovery for amendments as there is for new notices in `warn_x_select`), and its key would already be ledgered, so a cap here would silently lose revisions forever. Held rows are emailed **as held, never re-checked against the feed of the day**: the feeds oscillate between versions across runs and the ledgers, not the latest fetch, define the canonical version (`update_cumulative` collapses to it), so a row dropped because the feed had momentarily swung back would be lost for good — its key is already ledgered and can never be re-reported. A genuine reversion is indistinguishable from a swing and is reported the way the dashboard shows it. **The wait is capped at `warn_monitor.PENDING_MAX_AGE_DAYS` (30).** "Until a new notice arrives" is unbounded for the quarter of sources that go months between filings — over the 98 runs logged to 2026-09-08, AK, CT, KS, ND, NM, RI, SD and VT recorded no new notice at all — and a held row is not merely deferred but unreachable, since its key enters both alert ledgers the moment it is held. So once any held row passes the cap, that state's whole pending ledger is emailed on its own: the one case where an amendment earns its own mail. `warn_monitor.overdue_amendments` measures age from `held_since`, stamped when the row was FIRST held and deliberately not refreshed by a later revision — otherwise a daily-churning filing would never age out. Flushing every held row rather than only the overdue one is what keeps the mail rare: Virginia's churn costs one email a month, not one a morning. A row whose `held_since` is missing or unparseable counts as overdue, erring toward delivery. Because of the cap, `alert_for_state` is called for **every registered** state on every run (`registered_sources`, not `all_sources`), not only the states whose feed changed. Every exclusion had to go, because each was a permanent-suppression path for exactly the states the cap protects: a quiet state is the one sitting on a stale hold; a source that ERRORS keeps erroring while its hold ages; and a DISABLED source may still hold rows from before it was switched off. A state with nothing held costs one absent-file check (0.1 ms for all 46). The flush email is built by `_amendments_only_diff` — the held rows and nothing else, never a copy of the run that tripped the cap. That run's `removed_count` is the danger: a truncated fetch reports every missing filing as a withdrawal (Alabama's feed has served a near-empty file ten times, six with no new notices), and before the cap such a run mailed nothing at all, so forwarding it would put "1,067 previously filed notices were withdrawn" under a subject about one amended notice. Totals likewise come from `Source.standing_summary()` — the CUMULATIVE store, always, not this run's: a truncated fetch returns a perfectly truthy summary claiming the state holds 3 filings, and `save_latest` has already overwritten `warn_latest.json` with that short file by the time the notify loop runs, so only the union still knows better. It is also the number the dashboards show. A normal new-notice alert still reports its own run's totals. The mail reads as an *Update* rather than an *Alert*, and its text part does not lead on "New notices: 0". Cleared only after the clubbed email sends. The email diff is a copy — the `state_results` diff the X stage reads keeps its `new_keys[:new_count]` contract. Absent when nothing is held. Guarded by `tests/test_sources.py::test_held_amendment_is_never_redetected_as_new_or_amended` and the policy tests in `tests/test_publish.py`.
- **Virginia nulls the effective date on rescinded rows** (`warn_sources/va.py`, `is_rescinded`). The feed marks a rescission only in the Company cell (`"AeroFarms Inc. - Rescinded"`, `"JELD-WEN-rescinded"`, `"… *notice rescinded"`) and, for the currently-rescinded notice, fills Impact Date with the CSV's render date — a new value every download, which re-keyed the same filing every run (~40 consecutive amendment-only runs, one ledger key per day). A rescinded notice has no impact date, so `effective_date` is None on those rows and the notice key is stable; the row itself stays exactly as published. The switch re-keyed three existing rows, so their undated keys were **seeded by hand into both VA ledgers** in the same change (`AeroFarms Inc. - Rescinded__None__133`, `Pyrotechnique by Grucci Inc. *notice rescinded__None__0`, `JELD-WEN-rescinded__None__138`): with both ledgers already knowing the key, `detect_changes` reports nothing and `update_cumulative` collapses the dated version out on the next run. `tests/test_source_va.py::test_seeded_ledgers_make_the_key_change_silent` pins that transition; without the seed it would still be safe, surfacing as one held amendment rather than a "new notice" alert. **Merging this branch conflicts in both VA ledgers**: `monitor.yml` rewrites them twice daily on `main`, appending the next dated AeroFarms key (`…__2026-09-08__133`) right where the seed inserts `…__None__133`. Resolve by **keeping both sides** — main's dated keys are history and the three undated keys are the seed; taking main's copy alone drops the seed and the next run reports the three rescinded rows as amendments (held, not emailed, so it is untidy rather than harmful).
- `meta.json` — ETag + file hash + timestamps for cache invalidation
- `warn_national.json` — unified multi-state dataset (records stamped with `state`), rebuilt every publish run by `warn_sources/aggregate.py`
- `digest_sent.json` — ledger of monthly-digest periods already emailed (`YYYY-MM`), written only after a successful send so a failure retries next run
- `states/<code>/` — per-state pipeline files for every non-CA source (same shapes as the top-level CA files: warn_latest, snapshot, cumulative, meta, both key ledgers, pending_amendments, changelog)
- `changelog.jsonl` — append-only log of every detected change

**GitHub Actions** — three workflows, with one deliberate coupling:
- `monitor.yml` runs the full pipeline twice daily (00:00 and 12:00 UTC).
  Automated commits use `"auto: WARN data update [skip ci]"` to prevent loops.
- `tests.yml` runs pytest on every pull request. Before it existed no PR ever
  ran the suite in CI (`monitor.yml` is schedule-only; CodeQL was the sole PR
  check). It deliberately runs no flake8 — the repo carries ~177 standing
  violations, so a lint gate would be permanently red.
- `pages.yml` deploys `docs/` to GitHub Pages (Settings ▸ Pages ▸ Source =
  **GitHub Actions**, not branch). The branch-based build it replaced wedged
  routinely (builds stuck at duration 0, deploys cancelled mid-flight), which
  could leave main updated while the live site silently served stale content.
  **The coupling:** the pipeline's `[skip ci]` commits cannot fire `pages.yml`'s
  push trigger, so it also runs on `workflow_run` after every successful
  `monitor.yml` run — renaming `monitor.yml`'s `name:` breaks that link
  silently. Manual redeploy: Actions ▸ Deploy Pages ▸ Run workflow.

**X / @USLayoff auto-posting** (`warn_x.py`, full runbook in `X_POSTING.md`).
When a run detects new notices, notable ones become posts on
[@USLayoff](https://x.com/USLayoff). The load-bearing decisions:

- **The review gate and the automation are one code path.** The pipeline never
  posts; it only enqueues into `data/x_queue.json`. Posting reads rows whose
  status is `approved` — in review mode a human writes that word, in auto mode
  `X_AUTO_POST=1` does. So the text a human approved is byte-for-byte what auto
  mode sends, pinned by `tests/test_x.py::
  test_the_gate_does_not_change_a_single_byte_of_the_post`. Phase 1 needs **no
  GitHub secrets at all**: CI stages candidates and commits them, and the
  operator reviews and posts from a local checkout.
- **Grouping is cross-state, so the step sits outside the per-state notify
  loop.** `state_results` (`warn_publish.py`, step 1) is the only object
  holding every state's diff — `monitor_result` is literally the CA entry.
  A company laying off in three states in one run is ONE story and must be ONE
  post carrying the combined headcount; posting inside the loop would put three
  partial numbers under the same brand name seconds apart.
- **`new_entries` is truncated at 50** (`warn_monitor.py`, end of
  `detect_changes`) while `new_keys` is complete (`amendments` is deliberately
  NOT truncated — see `pending_amendments.json` above). A state landing 93 new
  notices hands over 50 records, and silently posting those 50 would understate
  a company's headcount with nothing looking wrong.
  `warn_x_select.collect_new_notices` detects the mismatch and recovers the
  rest from that state's just-written `warn_latest.json`. `new_keys[:new_count]`
  is exactly the genuine-new set; `new_keys[new_count:]` are amendment keys.
- **Contractors are never merged into the client brand.** `"Flagship Facility
  Services Inc. at Meta Platforms Inc."` is Flagship's filing.
  `warn_names._SPLIT_AT` keeps the left-hand employer and `warn_brands.resolve`
  vetoes a brand that only appears after ` at ` / `dba` / `@` / an operator
  suffix. Posting "Meta filed a WARN notice for N job cuts" off that row is the
  worst factual error this system can make, and it is one regex away.
- **`warn_names.LEGAL` is deliberately narrow** — legal forms only. Adding
  `Group`, `Holdings`, `USA` or `Services` merges `Compass Group USA` into
  `compass` and `Enterprise Products` into `Enterprise Rent-A-Car`. Brand-level
  merging belongs in `warn_brands.py`, where each merge is written down and
  tested against all 40,956 distinct company strings.
- **Ledgers live under `data/`, like the alert ledgers, for the same reason.**
  `commit_ledgers` and monitor.yml's failure branch stage `data/` alone; a
  ledger written elsewhere is lost with the CI workspace and every notice is
  re-posted twice a day forever. `data/x_posted_keys.json` is written only
  after a post lands, and records **every** key in the batch.
- **The transport is the BROWSER, not the API, because the API is not free.**
  X went pay-per-use on 2026-02-06 ($0.015 a post, $0.200 with a URL) and the
  account's console balance is $0.00, so `X_TRANSPORT` defaults to `browser`:
  approved posts are staged into `data/x_outbox.json` and sent through a
  logged-in x.com session. tweepy is not in requirements.txt; the `api` path
  survives only for whoever later buys credits. CI pins `dry` — a runner has no
  browser — which means posting is a LOCAL step and "fully automated" is a cron
  job on the operator's Mac, not GitHub Actions. Programmatic `@mentions` are
  blocked in normal posts (2026-02-23), so company names are sanitised;
  self-replies still work, which is how a >8-state breakdown threads.
- **Neither the post nor the card carries a URL.** `X_INCLUDE_LINK` defaults
  to False and `warn_x_image.BRAND` is the account's name: the dashboard sits
  on a github.io address, and a raw project-hosting link under a layoff
  headline reads as a hobby page rather than a source. The link goes back in
  when there is a real domain — flip the flag and update
  `warn_urls.SITE_BASE_URL`.
- **Every post carries a generated card, and it never carries a company's
  logo.** Real logos are trademarks nobody licensed to us, and one beside a
  layoff headline implies an association nobody granted. `warn_x_image` draws
  the employer's NAME large — nominative use, the same right that lets the post
  name them at all — over the dashboard's own palette and arrow motif. Cards
  render at post time into gitignored `data/x_cards/`; only the card's *inputs*
  live on the queue row, so no PNG enters git twice a day and a card can never
  disagree with the post beside it.
- **A failed post never auto-retries** and a 403 opens a 24h circuit breaker —
  odishanow20's stated policy, which its own code did not implement.
  "Unconfigured" (no keys, no tweepy) is a distinct outcome that latches
  nothing, so a developer laptop cannot disable posting for CI.
- **`posted` is terminal, and the daily cap counts tweets not rows.** A live
  tweet cannot be un-published, so `approve` refuses a posted row. A threaded
  batch sends `1 + len(thread)` tweets, and the root is recorded the moment it
  has an id — a self-reply that fails must never demote a landed post, or a
  human re-approving it publishes the same text twice.
- **The display name is not the raw company string.** `warn_brands` refusing to
  call "Flagship Facility Services Inc. at Meta Platforms Inc." a Meta filing
  is only half the job: printing that string verbatim still named Meta in the
  post. `warn_x_select._display_source` cuts the client side (360 records in
  the dataset carried one, 28 of them big enough to auto-approve). Because only
  the *display* was wrong, every test that checked the numbers passed.

**Adding a step to `warn_publish.run()`**: eight patch stacks in
`tests/test_publish.py` enumerate every stage by name (one decorator stack, seven
`with` stacks), and `test_every_run_stage_is_mocked_in_this_file` recomputes
the seams from `run()`'s source and fails on any stack that misses one. A new
step missing from any of them executes for real in CI. Note the decorator
stack takes its mocks bottom-up, so inserting a decorator shifts the argument
list. A harness that deliberately exercises a seam opts out by naming it in
its docstring (`"X is NOT mocked here"`), as `_run_notify_loop` does for
`alert_for_state`.

**Environment** (copy `.env.example` → `.env`):
- `GH_REPO_TOKEN` — for git push in local runs (read by `warn_publish.git_commit_push`)
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL` — for email alerts
- `SIGNUP_ENDPOINT` — Apps Script `/exec` URL for the signup form (public; a CI repo *variable*)
- `SUBSCRIBERS_TOKEN` — shared secret to read the subscriber list (a CI *secret*)
- `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` — OAuth
  1.0a user context for @USLayoff (CI *secrets*). Unset is safe: the stage
  composes and queues, and sends nothing.
- `X_TRANSPORT` (`dry` | `api` | `browser`), `X_AUTO_POST`,
  `X_DISABLE_POSTING`, `X_INCLUDE_LINK` — CI repository *variables*, so the
  gate and the kill switch flip without a deploy

## Testing

Tests use pytest with fixtures in `tests/conftest.py` (`mock_env`, `mock_data_dir`, `sample_warn_data`). The CI workflow also installs `pytest-mock` and runs `pytest -v --cov=.` before the pipeline step.
