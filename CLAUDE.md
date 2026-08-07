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
                      → warn_notify.py (Gmail alert if changes detected, per state)
                          ↳ warn_subscribers.py (fetch signup list → BCC subscribers)
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
- `meta.json` — ETag + file hash + timestamps for cache invalidation
- `warn_national.json` — unified multi-state dataset (records stamped with `state`), rebuilt every publish run by `warn_sources/aggregate.py`
- `digest_sent.json` — ledger of monthly-digest periods already emailed (`YYYY-MM`), written only after a successful send so a failure retries next run
- `states/<code>/` — per-state pipeline files for every non-CA source (same shapes as the top-level CA files: warn_latest, snapshot, cumulative, meta, both key ledgers, changelog)
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

**Environment** (copy `.env.example` → `.env`):
- `GH_REPO_TOKEN` — for git push in local runs (read by `warn_publish.git_commit_push`)
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL` — for email alerts
- `SIGNUP_ENDPOINT` — Apps Script `/exec` URL for the signup form (public; a CI repo *variable*)
- `SUBSCRIBERS_TOKEN` — shared secret to read the subscriber list (a CI *secret*)

## Testing

Tests use pytest with fixtures in `tests/conftest.py` (`mock_env`, `mock_data_dir`, `sample_warn_data`). The CI workflow also installs `pytest-mock` and runs `pytest -v --cov=.` before the pipeline step.
