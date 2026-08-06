# Expanding the WARN Tracker from California to All US States — Research Report

*Researched 2026-07-20. Combines a multi-agent web research sweep (claims adversarially verified 3-0 unless noted), firsthand inspection of the Big Local News codebases, and ~35 live endpoint probes run today.*

> **STATUS (2026-07-23): IMPLEMENTED.** All phases of the rollout below are
> complete: 47 scraper modules in `warn_sources/` (46 states + DC live;
> TX fully implemented but disabled behind an AWS WAF challenge — flip
> `enabled=True` once a sanctioned fetch proxy is configured). AR, WY keep
> filings confidential by statute; NH/PR/territories publish no list. The
> US dashboard is the site root, `docs/` (built by `warn_site_us.py`);
> California moved to `docs/ca/` and now derives its records from the national
> dataset so the two pages cannot report different California numbers.
> Per-state details live in each module's docstring and its
> `tests/test_source_<code>.py`.

---

## 1. Executive summary

- **There is no national WARN feed.** The US DOL explicitly does *not* collect or publish WARN notices centrally — its [layoffs contact page](https://www.dol.gov/agencies/eta/layoffs/contact) is only a routing directory of per-state Rapid Response coordinators (verified 3-0). Every state publishes (or doesn't) on its own terms.
- **~46 of 56 jurisdictions publish something; formats are chaotic.** XLSX (CA, NJ, TX, KY, MT, RI…), CSV (VA), HTML tables (≈20 states), PDF-only (ID, NM, ND, MN, NV), interactive web apps (FL, GA, IL, WA, the six "America's JobLink" states), and a Tableau dashboard (NY since 4/1/2025). **Arkansas and Wyoming make WARN filings confidential by statute; PR and the territories publish nothing; New Hampshire has no public list.**
- **Do not write 50 scrapers from scratch.** Stanford's Big Local News (BLN) maintains [`warn-scraper`](https://github.com/biglocalnews/warn-scraper) (Apache-2.0, pip-installable), with working scrapers for **43 jurisdictions (42 states + DC)**, plus [`warn-transformer`](https://github.com/biglocalnews/warn-transformer) which normalizes all of them into one schema, and `warn-github-flow`, a GitHub Actions ETL that runs the whole thing daily (all verified 3-0). The recommended architecture is: **use `warn-scraper` as a library for acquisition, keep this repo's diff/ledger/alert/dashboard pipeline as the product layer**, and hand-write only the ~5 gap-state scrapers that BLN lacks but that do publish (NC, MA, MN, NV, WV).
- **Anti-bot walls are the single biggest operational risk.** BLN's own production pipeline passes a **Zyte API key** (commercial scraping proxy) into `warn-scraper` (verified 3-0). My probes today confirmed why: TX blocks even direct `.xlsx` downloads behind an Akamai challenge (HTTP 202), MN sits behind a ShieldSquare/Radware wall, and mass.gov, detr.nv.gov, dws.arkansas.gov, nhes.nh.gov — and dol.gov itself — return 403 to non-browser clients.
- **Buying is a real option but a bad fit.** [layoffdata.com](https://layoffdata.com/api-docs/) sells a REST API + CSV (49 states, ~82k notices, 13 fields/record, subscription-gated). Useful as a validation benchmark; as a primary source it would make the platform a paid reseller of someone else's scraping with no control over freshness.

---

## 2. How WARN publication actually works

Employers file WARN notices with **state** dislocated-worker units. DOL's ETA maintains only a contact directory covering all 50 states + DC + PR + USVI (verified 3-0 against [dol.gov](https://www.dol.gov/agencies/eta/layoffs/contact)). Consequences for this platform:

1. Fifty-plus independent sources, each with its own format, cadence, URL-stability, and legal regime.
2. "What counts as a WARN event" differs by state: **mini-WARN states** (lower thresholds than the federal 100-employee / 50-affected rules) publish far more notices per capita — CA (CalWARN, 75+ employees), NY (50+ employees / 25 affected, 90-day notice), NJ (90-day notice + mandatory severance since 2023), IL (75+), MD (Economic Stabilization Act, mandatory since Oct 2020), DE (2019), HI, IA, ME, TN, VT, WI, NH (RSA 275-F: 25+ if ⅓ of workforce, else 250+), and — new — **WA (SB 5525, effective July 27, 2025: 50+ FTEs → mandatory ESD notice)**. Some states also publish *non-WARN* voluntary layoff reports mixed into the same feed (NV explicitly labels "WARN and Non-WARN"; several JobLink states include sub-threshold events).
3. Date semantics differ: some states publish *notice received* date, some *dated* date, some only *effective/layoff start* date (WA, NJ, GA lack a true notice date; KY's feed effectively swaps notice/effective).

---

## 3. Master source table — all 56 jurisdictions

**Legend** — `BLN`: covered by warn-scraper today. Format/access notes marked ⚡ were verified by live probes today (2026-07-20); the rest come from BLN source docs and targeted searches.

### High-volume states (start here)

| State | Source | Format | Access mechanics | BLN |
|---|---|---|---|---|
| CA | EDD `warn_report1.xlsx` | XLSX | ⚡ direct download, ETag + Last-Modified honored (current pipeline) | ✅ |
| NY | [WARN dashboard](https://dol.ny.gov/warn-dashboard) (since 4/1/2025) + [legacy pages](https://dol.ny.gov/legacy-warn-notices) per year | Tableau + HTML archive | ⚡ old `/warn-notices` 301s to legacy; dashboard is Tableau Public (BLN scrapes the workbook); historical XLSX in BLN's public bucket | ✅ |
| TX | TWC [`warn-act-listings-<YYYY>-twc.xlsx`](https://www.twc.texas.gov/data-reports/warn-notice) per year | XLSX | ⚡ **Akamai bot challenge (HTTP 202) even on the static XLSX** — needs Zyte/browser-grade fetch | ✅ |
| FL | [reactwarn.floridajobs.org](https://reactwarn.floridajobs.org/WarnList/Records?year=2026) | ASP.NET web app (HTML) | ⚡ plain GET works (200); old floridajobs.org listing pages are dead (404) | ✅ |
| IL | IWD [public export API](https://apps.illinoisworknet.com/iebs/api/public/export?...) | JSON/CSV API | ⚡ extremely slow (>20 s timeouts are normal); plan long timeouts + retries | ✅ |
| OH | JFS [current notices](https://jfs.ohio.gov/job-workforce-services/job-programs-and-services/submit-a-warn-notice/current-public-notices-of-layoffs-and-closures) | HTML + PDF archive | ⚡ URL churned twice since 2023; old paths 404 | ✅ |
| PA | [pa.gov WARN notices](https://www.pa.gov/agencies/dli/programs-services/workforce-development-home/warn-requirements/warn-notices) | HTML (huge single page) | ⚡ 200, ~840 KB page, ETag present | ✅ |
| MI | [michigan.gov LEO](https://www.michigan.gov/leo/bureaus-agencies/wd/data-public-notices/warn-notices) | HTML | ⚡ 200; pre-11/2025 archive is a 10 MB zip in BLN's bucket | ✅ |
| WA | ESD [SearchWARN.aspx](https://fortress.wa.gov/esd/file/warn/Public/SearchWARN.aspx) | ASP.NET search app | ⚡ 200; form-post pagination; no notice date field (only layoff start) | ✅ |
| NJ | [`WARN_Notice_Archive.xlsx`](https://www.nj.gov/labor/assets/PDFs/WARN/WARN_Notice_Archive.xlsx) | XLSX (full archive, one file) | ⚡ direct download, ETag + Last-Modified — **the easiest big state**; updated day-of-probe | ✅ |
| GA | GDOL [search list](https://www.dol.state.ga.us/public/es/warn/searchwarns/list) + TCSG [public view](https://www.tcsg.edu/warn-public-view/) | HTML / WP admin-ajax | ⚡ TCSG 200; two overlapping sources need dedup | ✅ |

### Remaining BLN-covered states

| State | Source | Format | Notes | BLN |
|---|---|---|---|---|
| AK | jobs.alaska.gov | HTML | | ✅ |
| AL | madeinalabama.com / workforce.alabama.gov | HTML | source moved once already | ✅ |
| AZ | azjobconnection.gov | JobLink app | shared "America's JobLink" platform module | ✅ |
| CO | cdle.colorado.gov | HTML → Google Sheet | state publishes via a Google Spreadsheet | ✅ |
| CT | dolpublicdocumentlibrary.ct.gov | JSON API (paged) | clean specialized endpoint | ✅ |
| DC | does.dc.gov, per-year page | HTML | year-templated URL | ✅ |
| DE | joblink.delaware.gov | JobLink app | | ✅ |
| HI | labor.hawaii.gov | HTML (news posts) | messy; BLN resorts to cache tricks | ✅ |
| IA | workforce.iowa.gov | HTML + XLSX | | ✅ |
| ID | labor.idaho.gov | **PDF** | pdfplumber territory | ✅ |
| IN | in.gov/dwd | HTML | | ✅ |
| KS | kansasworks.com | JobLink app | | ✅ |
| KY | kcc.ky.gov | XLSX | feed's notice/effective semantics swapped | ✅ |
| LA | laworks.net | HTML + per-year PDF | | ✅ |
| MD | dllr.state.md.us | HTML | | ✅ |
| ME | joblink.maine.gov | JobLink app | | ✅ |
| MO | jobs.mo.gov | HTML per year | | ✅ |
| MS | mdes.ms.gov | HTML/PDF | | ✅ |
| MT | wsd.dli.mt.gov | XLSX | | ✅ |
| ND | jobsnd.com | **PDF** ("2015 to present" single PDF) | | ✅ |
| NE | dol.nebraska.gov | HTML per year | | ✅ |
| NM | dws.state.nm.us | **PDF** | | ✅ |
| OK | employoklahoma.gov / okjobmatch | JobLink app | | ✅ |
| OR | ccwd.hecc.oregon.gov | XLSX via `/Layoff/WARN/Download` | ⚡ endpoint answers 200 but returns HTML shell first; needs the app's download flow | ✅ |
| RI | dlt.ri.gov | XLSX | | ✅ |
| SC | scworks.org | HTML + PDF | | ✅ |
| SD | dlr.sd.gov | HTML | | ✅ |
| TN | tn.gov | HTML + PDF | historical CSV in BLN bucket | ✅ |
| UT | jobs.utah.gov | HTML | | ✅ |
| VA | [virginiaworks.gov](https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/) | HTML (CSV formerly) | ⚡ old vec.virginia.gov CSV is 404; new agency site works | ✅ |
| VT | vermontjoblink.com | JobLink app | | ✅ |
| WI | dwd.wisconsin.gov | HTML per year | | ✅ |

### Gap states — no BLN scraper (custom work needed)

| State | Publishes? | Source | Format | Access |
|---|---|---|---|---|
| **NC** | ✅ yes, well | [Workforce WARN reports](https://www.commerce.nc.gov/data-tools-reports/labor-market-data-tools/workforce-warn-reports) + per-year summary list + [LEAD Analytics dashboard](https://analytics.nccommerce.com/NC-WARN-Reports/) | HTML tables + dashboard | ⚡ commerce.nc.gov reachable; old deep link 404'd (URL churn) |
| **MA** | ✅ weekly (Fridays) | [mass.gov WARN updates](https://www.mass.gov/info-details/worker-adjustment-and-retraining-notification-act-warn) | weekly report (HTML/downloads), by region | ⚡ mass.gov 403s plain curl (Akamai) — needs browser-grade fetch |
| **MN** | ✅ monthly-ish | [DEED dislocated-worker reports](https://mn.gov/deed/programs-services/dislocated-worker-program/reports/), e.g. `plant-closing-mass-layoff-warn-october-2025.pdf` | **PDF** | ⚡ ShieldSquare/Radware bot wall on mn.gov |
| **NV** | ✅ yearly master files | DETR, e.g. [`WARN_and_Non_WARN_Master_w_Logo.pdf`](https://detr.nv.gov/content/media/WARN_and_Non_WARN_Master_w_Logo.pdf) | **PDF** (WARN + non-WARN mixed, labeled) | ⚡ detr.nv.gov 403s plain curl |
| **WV** | ✅ | [WARN Listing](https://workforcewv.org/job-seeker/layoffs-downsizing/warn-listing/) | HTML | reachable |
| **AR** | ❌ **confidential by statute** (Ark. Code § 11-10-314) | — | — | show as "no public data" |
| **WY** | ❌ **confidential by statute** (Wyo. Stat. § 9-2-2607) | — | — | show as "no public data" |
| **NH** | ❌ no public list found | employers report to NHES (masslayoffcoordinator@); has its own RSA 275-F act but no published listing | — | mark "no public list"; re-check periodically |
| **PR** | ❌ none found | federal WARN applies; DOL directory lists a coordinator; no public listing | — | — |
| AS / GU / MP / VI | ❌ | nothing found; BLN also lists them as unscraped | — | — |

---

## 4. Prior art & aggregators (build-vs-buy)

| Option | Coverage | Freshness | Cost / license | Verdict |
|---|---|---|---|---|
| **BLN `warn-scraper` + `warn-transformer`** ([Layoff Watch](https://biglocalnews.org/content/tools/layoff-watch.html)) | 43 jurisdictions | their `warn-github-flow` runs daily; you can run the scrapers yourself at any cadence | **Apache-2.0, pip-installable** | **Use as acquisition library.** Verified 3-0: per-state module pattern `scrape(data_dir, cache_dir) → Path`, shared `Cache`/`utils`, matrix of exactly 43 source slugs, Zyte key for hard states. |
| BLN consolidated dataset (biglocalnews.org platform) | same 43, standardized + deduped | daily | free w/ account; API via `bln` client | Good cross-check; adds platform dependency. Public GCS bucket (`bln-data-public/warn-layoffs/`) holds **historical backfills only** (NY, TX, OR, OH, GA, KY, IA, TN, MI ⚡). |
| **layoffdata.com** ("WARN Database") | claims 49 states, ~82k notices, 1988→present (per Dewey listing; unverified) | site claims weekly+ | **paid subscription** (API + CSV tiers) | Benchmark / gap-checker, not a foundation. 13 fields incl. county, industry, union ⚡ (API docs). |
| Cleveland Fed WARN dataset (openICPSR 155161) | state×month **aggregate counts** only | academic-cadence | free | Not notice-level → unusable for alerts; fine for chart sanity checks. (Verification incomplete — session limits.) |
| WARN Firehose / LayoffAlert / WARNTracker / Layoff Lookout / kadoa | commercial/hobby trackers, similar scope to this project | varies | closed | Competitive landscape, not sources. |
| US DOL | none — routing directory only | — | — | Verified 3-0: no national database exists. |

**Recommendation: hybrid build.** `pip install warn-scraper` for the 43 covered jurisdictions (pin the version; vendor-patch if a state breaks before upstream fixes it), write 5 custom scrapers (NC, MA, MN, NV, WV), keep CA on your existing battle-tested EDD module, and contribute the 5 new scrapers upstream — that's how the project is designed to grow (verified 3-0 from their contributor docs).

---

## 5. Unified schema

BLN's transformer targets a lowest-common-denominator 10-field schema (`hash_id, postal_code, company, location, notice_date, effective_date, jobs, is_temporary, is_closure, is_amendment`). Your CA schema is richer (county, city, industry, address, layoff_type). Recommended superset — keep rich fields nullable, never fabricate:

```
state            2-letter code                        REQUIRED
company          str                                  REQUIRED
notice_date      ISO date | null                      # "received by state" where distinguishable
effective_date   ISO date | null
employees        int | null                           # affected count as reported
layoff_type      enum: closure|layoff|unknown         # + is_temporary bool|null
county / city / address / industry    str|null        # state-dependent granularity
notice_id        str|null                             # state-native ID where one exists (FL, IL, NY)
source_url       str                                  # provenance, per record
first_seen / last_seen   ISO timestamps               # your pipeline's own observation ledger
is_amendment     bool                                 # from your anchor-key logic, generalized
raw              dict                                 # untouched source row for audit
```

Normalization landmines (from BLN's 44 transformer field-maps, extracted today):

- **Column chaos is total**: "Employees Affected" / "Emp #" / "Workforce Affected" / "# of Workers" / "TOTAL_LAYOFF_NUMBER" / "Number toEmployees Affected" (sic, DC) all mean `employees`. Treat BLN's per-state `fields` dicts as the authoritative crosswalk.
- **Date formats**: at least 10 distinct (`%m/%d/%Y`, `%m/%d/%y`, `%b %d, %Y`, `%Y-%m-%d %H:%M:%S`, "March 2024", bare "2024"…). IN alone needs 6 candidate formats; CT 4. Parse with per-state format lists + a shared fallback; store unparseable originals in `raw`.
- **Missing/odd semantics**: NJ, GA, MI, WA publish no true notice date; KY's is swapped; WA's "Layoff Start Date" doubles as both. SD/UT/NE/SC/HI/AZ-style JobLink feeds lack effective dates. Never synthesize one from the other.
- **Counts**: ranges ("50-99"), "unknown", zeros, and per-site vs per-company totals (IL reports per location). BLN guard-rails: `maximum_jobs=10000` sanity cap, manual `jobs_corrections` dicts — adopt both.
- **Taxonomy**: map state wording onto `closure|layoff` + `is_temporary`, keep original in `raw` (CA's "Layoff Permanent" already fits).
- **Amendments**: your CA anchor-key design (company+county+city+notice_date surviving while notice_key changes) generalizes; BLN handles amendments per-transformer with an `is_amendment` flag. Keep per-state `notified_keys` / `amended_keys` ledgers — feed flip-flops like EDD's exist elsewhere (FL and IL apps re-order and re-total rows routinely).

---

## 6. Recommended architecture for this repo

```
warn_sources/                    # NEW package
    __init__.py                  # REGISTRY: {"ca": CaSource, "nj": NjSource, ...}
    base.py                      # Source ABC: fetch() -> raw file, parse() -> DataFrame
                                 #   + per-source: cadence, url, needs_browser, enabled
    ca.py                        # today's warn_monitor fetch/parse, unchanged behavior
    bln.py                       # wraps warn-scraper Runner for the 43 covered slugs,
                                 #   then maps BLN CSVs -> unified schema via crosswalk
    nc.py, ma.py, mn.py, nv.py, wv.py   # custom gap scrapers (contribute upstream)
data/
    states/<st>/warn_latest.json, snapshot.json, notified_keys.json, amended_keys.json
    warn_national.json           # concatenated unified dataset (drives charts + data.json)
```

- **Keep the product layer you already have.** `warn_diff`, the ledgers, `warn_notify`, `warn_charts`, `warn_publish` all operate per-state then aggregate; your dedup/amendment logic is *ahead* of most trackers.
- **Failure isolation**: one state failing must never block the run. Wrap each source in try/except, emit a per-state status block into `meta.json`, and surface "last successful fetch per state" on the dashboard (stale-source badges). In Actions, either loop in-process with isolation (simplest, keeps one commit per run) or a `matrix: state` job with `fail-fast: false` (BLN's production pattern, verified 3-0) writing per-state artifacts merged by a final job.
- **Cadence tiers**: keep twice-daily for direct-file states (CA, NJ, TX, KY, MT, RI, CT, VA); daily is plenty for HTML/PDF states (many update weekly — MA literally publishes Fridays; MN monthly; NV a few times a year). A `cadence` attribute per source + a `--tier` flag on `warn_publish` keeps Actions minutes and ban-risk down.
- **Hard-state fetching**: for TX/MN/MA/NV (bot-walled ⚡), route `fetch()` through a pluggable fetcher: plain `requests` → retry with browser headers → Zyte/ScrapingBee-style API (env `ZYTE_API_KEY`, exactly BLN's production approach, verified 3-0) → else mark stale, don't fail the run.
- **PDF states** (ID, NM, ND, MN, NV): `pdfplumber` (BLN's choice); snapshot every parsed PDF into `data/states/<st>/archive/` because states silently replace files.
- **Politeness**: 1 req/s per host, identify with a real UA + contact email, honor ETag/Last-Modified where offered (CA, NJ, PA do ⚡), cache raw downloads before parsing (BLN's `Cache` pattern).
- **Expect URL churn as steady-state**: OH moved twice, FL/VA/AL/NY all moved within ~18 months. A weekly "source health" CI step that fails soft (dashboard badge + email to you, not subscribers) beats discovering silence weeks later. Your ETag-cache + `--force` design already fits.
- **Product surfaces**: add `state` to `data.json`, a state filter + US choropleth to the dashboard, and per-state subscription preferences (extend the signup sheet with a states column; BCC logic filters by subscriber's states).

---

## 7. Phased rollout

| Phase | States | Rationale |
|---|---|---|
| **0** | Refactor CA behind the `Source` interface; introduce `data/states/`; national concat; dashboard state dimension | zero-new-data risk, unblocks everything |
| **1** | NJ, TX, NY, KY, MT, RI, OR, CT (+ CA) | file/API sources with clean parsing; TX is the Zyte guinea pig; NY needs the Tableau-dashboard path (post-4/2025) |
| **2** | FL, IL, OH, PA, MI, WA, GA, WI, MO, IN, MD, CO, IA, VA | big HTML/app states via `warn-scraper`; highest volume payoff |
| **3** | JobLink six (AZ, DE, KS, ME, OK, VT) + AK, AL, HI, LA, MS, NE, NM, ND, SC, SD, TN, UT, ID, DC | long tail via `warn-scraper`; PDF states last within the phase |
| **4** | Custom: NC, MA, MN, NV, WV (contribute upstream) | the five publishers BLN lacks |
| **5** | Historical backfill: BLN public bucket (NY/TX/OR/OH/GA/KY/IA/TN/MI ⚡) + BLN consolidated dataset; extends your 2014-present CA history nationally | depth after breadth |
| — | AR, WY, NH, PR, territories | mark "no public data" on the dashboard with the statutory citation — honesty is a feature |

**Ordering logic**: (notice volume × parse reliability) first; per-phase gate = two clean weeks of diffs with no phantom "new notice" alerts (your CA flip-flop war informs the acceptance bar).

---

## 8. Open questions / caveats

- Claims marked "verified 3-0" survived the adversarial verification workflow; the Cleveland Fed, layoffdata-coverage-depth, and BLN-email-alert claims did **not** finish verification (session limits) and carry ordinary web-source confidence.
- Mini-WARN thresholds above are for context, not legal advice; NH's exact post-2021 thresholds showed conflicting sources (statute merged text vs NHES postcard) and don't affect scraping either way.
- NY's Tableau dashboard export mechanics and OR's download flow need hands-on testing in Phase 1 (BLN's `ny.py`/`or.py` are the working references).
- Re-probe AR/WY/NH/PR yearly — publication policies change (MD only began mandatory publication in late 2020; WA's entire regime is a 2025 creation).

### Key sources
[DOL ETA layoffs/contact](https://www.dol.gov/agencies/eta/layoffs/contact) · [warn-scraper](https://github.com/biglocalnews/warn-scraper) · [warn-scraper docs](https://warn-scraper.readthedocs.io/en/latest/) · [warn-transformer](https://github.com/biglocalnews/warn-transformer) · [warn-github-flow](https://github.com/biglocalnews/warn-github-flow) · [BLN Layoff Watch](https://biglocalnews.org/content/tools/layoff-watch.html) · [layoffdata.com API docs](https://layoffdata.com/api-docs/) · [NY DOL WARN](https://dol.ny.gov/warn-notices) · [TWC WARN](https://www.twc.texas.gov/data-reports/warn-notice) · [FL REACT](https://reactwarn.floridajobs.org/WarnList/Records?year=2026) · [NC Commerce WARN reports](https://www.commerce.nc.gov/data-tools-reports/labor-market-data-tools/workforce-warn-reports) · [mass.gov WARN](https://www.mass.gov/info-details/worker-adjustment-and-retraining-notification-act-warn) · [MN DEED reports](https://mn.gov/deed/programs-services/dislocated-worker-program/reports/) · [NV DETR](https://detr.nv.gov/Page/WARN) · [WorkForce WV WARN listing](https://workforcewv.org/job-seeker/layoffs-downsizing/warn-listing/) · [WA SB 5525 (Holland & Knight)](https://www.hklaw.com/en/insights/publications/2025/07/washingtons-mini-warn-act-goes-into-effect-on-july-27-2025) · [Arkansas Times on AR confidentiality](https://arktimes.com/arkansas-blog/2020/04/02/state-secret-in-arkansas-data-on-mass-layoffs) · [NH RSA 275-F](https://gc.nh.gov/rsa/html/XXIII/275-F/275-F-mrg.htm)
