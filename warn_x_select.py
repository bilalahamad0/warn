"""
warn_x_select
-------------
Turn one pipeline run's new WARN notices into candidate X posts.

Pure, in the sense that matters: it reads ``state_results`` (and, only when a
state's ``new_entries`` was truncated, that state's ``warn_latest.json``) and
returns dataclasses. Nothing here talks to X, writes a ledger, or decides that
a post may be *sent* — ``warn_x`` owns all of that. Split this way so the
selection and wording can be tested exhaustively with no credentials and no
network, which is the only way the review gate is worth anything.

Four steps:

    collect_new_notices  every genuinely-new notice this run, ALL states
    group_by_company     one CompanyBatch per employer, summed across sites
    score                is this batch newsworthy, and on which arm
    compose              the post text, with X's own character weighting

WHY GROUPING IS CROSS-STATE. ``warn_publish.run`` alerts state by state, and
that is right for email — a New Jersey subscriber wants New Jersey. It is
wrong here. A company laying off in three states in one run is ONE story and
must be ONE post carrying the combined headcount; per-state posting would put
three partial numbers under the same brand name within seconds of each other.
So this runs once over the whole ``state_results`` map, after that loop.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import warn_brands
import warn_digest
import warn_monitor
import warn_names
import warn_urls

log = logging.getLogger("warn_x_select")

# ---------------------------------------------------------------------------
# Notability thresholds
# ---------------------------------------------------------------------------
#
# Combined headcount for one employer across every site in this run.
#
# Calibrated against the 60k-record national dataset, last 365 days, grouped
# by company + notice date (n=3,767 records). Grouped percentiles: p50 71,
# p75 125, p90 248, p95 388, p99 1,172. Event volume by threshold: >=100 fires
# 816 times a year (68/month — a firehose), >=250 about 250 times (~21/month),
# >=500 only 83 (7/month — too quiet to be a feed).
SIZE_THRESHOLD = 250

# A recognisable employer is news at a size an unknown LLC is not; this floor
# only keeps out the single-digit store closures that chains file constantly.
BRAND_THRESHOLD = 50

# "especially technology" — a lower bar for tech brands specifically. It has
# to be BELOW BRAND_THRESHOLD or the arm is dead code: every tech brand is a
# registered brand, so at an equal bar the brand arm already caught it and
# this branch only relabelled the verdict.
#
# Measured over the last 365 days of the national dataset, 25 adds 8 events a
# year the brand arm misses — Microsoft 42, Qualcomm 38, Western Digital 47,
# Uber 41 and 29, Panasonic 46, Illumina 28, Riot Games 26. All are news; none
# is noise. Dropping to 10 adds only 3 more.
TECH_THRESHOLD = 25

# Rite Aid filed 180 notices for 2,385 people — 13 at a time. Without a cap a
# chain unwinding its estate crowds out everything else in the feed.
MAX_POSTS_PER_COMPANY_PER_MONTH = 2

MAX_WEIGHTED_LEN = 280

# Sites listed inline in a multi-state post before it spills to a self-reply.
MAX_INLINE_STATES = 8


@dataclass
class Notice:
    """One new WARN notice, stamped with the state it came from."""

    state: str
    company: str
    employees: Optional[int]
    notice_date: Optional[str]
    effective_date: Optional[str]
    city: str = ""
    county: str = ""
    industry: str = ""
    key: str = ""          # "ST:company__effective_date__employees"
    raw: dict = field(default_factory=dict)


@dataclass
class CompanyBatch:
    """Every new notice from one employer in one run, summed."""

    key: str                       # canonical grouping key
    display: str                   # what a post prints
    notices: list
    employees: int                 # summed reported headcount (0 = none reported)
    employees_known: bool
    states: list                   # distinct state codes, most-affected first
    per_state: dict                # {"CA": 610, "TX": 380}
    sites: int
    first_effective: Optional[str]
    is_brand: bool
    tags: set
    partial: bool = False          # some sites reported no headcount

    @property
    def keys(self) -> list:
        return [n.key for n in self.notices]


@dataclass
class Verdict:
    post: bool
    arm: str                       # "size" | "brand" | "tech" | "none"
    reason: str


@dataclass
class Draft:
    """A composed post, ready for the queue."""

    text: str
    weighted_len: int
    warnings: list
    thread: list = field(default_factory=list)   # extra self-reply texts


# ---------------------------------------------------------------------------
# 1. Collect — every genuinely-new notice this run, across every state
# ---------------------------------------------------------------------------

def _int_or_none(value):
    return warn_digest.employee_count(value)


def collect_new_notices(state_results: dict, latest_for=None) -> list:
    """Flatten ``state_results`` into one list of new Notices.

    ``state_results`` is what ``warn_sources.run_all`` returns:
    ``{"ca": {"state": "CA", "diff": {...}, ...}, ...}``. Only ``diff`` matters
    here, and only its genuinely-new half.

    THE TRUNCATION. ``detect_changes`` returns ``new_entries`` capped at 50
    records (warn_monitor.py:641) while ``new_keys`` is complete, so a state
    that lands 93 new notices in one run reports 93 in ``new_count`` and hands
    over 50 records. Silently posting the 50 would understate a company's
    headcount without anything looking wrong. When the counts disagree we go
    back to that state's ``warn_latest.json`` — which ``save_latest`` has
    already written this run — and recover the missing records by key.

    ``latest_for(code) -> Path | None`` is injectable so tests need no
    warn_sources registry.
    """
    if latest_for is None:
        latest_for = _default_latest_for

    notices = []
    for code, res in (state_results or {}).items():
        state = (res.get("state") or code or "").upper()
        diff = res.get("diff") or {}
        new_count = diff.get("new_count", 0)
        if not new_count:
            continue

        entries = list(diff.get("new_entries") or [])
        # new_keys is [genuine-new…] + [amendment keys…]; the first new_count
        # entries are exactly the genuine-new set, in new_entries' own order.
        genuine_keys = list(diff.get("new_keys") or [])[:new_count]

        if len(entries) < new_count:
            recovered = _recover_truncated(
                state, entries, genuine_keys, latest_for(code)
            )
            if recovered is not None:
                entries = recovered
            else:
                log.warning(
                    f"[{state}] {new_count} new notices but only {len(entries)} "
                    "records available — company totals for this run may be "
                    "understated."
                )

        for rec in entries:
            notices.append(_to_notice(state, rec))
    return notices


def _default_latest_for(code: str):
    """Path to a state's warn_latest.json, via the source registry."""
    try:
        import warn_sources

        return warn_sources.get_source(code).paths.latest
    except Exception as e:  # noqa: BLE001 — a missing source must not stop a run
        log.debug(f"no latest path for {code}: {e}")
        return None


def _recover_truncated(state, entries, genuine_keys, latest_path):
    """Rebuild the full new-notice list from the state's freshly-written latest."""
    if not latest_path or not Path(latest_path).exists():
        return None
    try:
        records = warn_monitor.json.loads(Path(latest_path).read_text())["records"]
    except Exception as e:  # noqa: BLE001
        log.warning(f"[{state}] could not read {latest_path}: {e}")
        return None

    by_key = {}
    for rec in records:
        by_key.setdefault(warn_monitor._notice_key(rec), rec)

    rebuilt, missing = [], 0
    for key in genuine_keys:
        rec = by_key.get(key)
        if rec is None:
            missing += 1
            continue
        rebuilt.append(rec)
    if missing:
        log.warning(f"[{state}] {missing} new notice(s) not found in latest.")
    # Never return fewer than we were already given.
    return rebuilt if len(rebuilt) >= len(entries) else None


def _to_notice(state: str, rec: dict) -> Notice:
    return Notice(
        state=state,
        company=str(rec.get("company") or "").strip(),
        employees=_int_or_none(rec.get("employees")),
        notice_date=rec.get("notice_date"),
        effective_date=rec.get("effective_date"),
        city=str(rec.get("city") or "").strip(),
        county=str(rec.get("county") or "").strip(),
        industry=str(rec.get("industry") or "").strip(),
        key=f"{state}:{warn_monitor._notice_key(rec)}",
        raw=rec,
    )


# ---------------------------------------------------------------------------
# 2. Group — one batch per employer, summed across sites and states
# ---------------------------------------------------------------------------

def group_by_company(notices: list) -> list:
    """Collapse notices onto canonical employers.

    The user's requirement in one function: "always look for combined number of
    layoffs for that company with the latest announcement across all cities and
    states". Grouping is by ``warn_names.canonical``, so ``Amazon (SJC31)`` in
    California and ``Amazon-Dallas`` in Texas land in one batch.
    """
    buckets = {}
    for n in notices:
        key = warn_names.canonical(n.company)
        if not key:
            continue
        buckets.setdefault(key, []).append(n)

    batches = []
    for key, group in buckets.items():
        counted = [n for n in group if n.employees]
        employees = sum(n.employees for n in counted)
        # Only states that actually reported a number get a number. Summing
        # `n.employees or 0` across a mixed batch printed "CA 610 · TX 0" in a
        # public post — meaningless to a reader — and counted the TX sites in
        # "across 3 sites" as though the 610 covered them.
        per_state = {}
        for n in counted:
            per_state[n.state] = per_state.get(n.state, 0) + n.employees
        # `states` still lists every state the filing touches, so the hashtag
        # and the state count stay true even where the headcount is partial or
        # (Hawaii, Oklahoma) entirely absent. Only `per_state` is restricted to
        # states that published a number.
        states = sorted(
            {n.state for n in group},
            key=lambda s: (-per_state.get(s, 0), s),
        )

        effectives = sorted(
            d for d in (n.effective_date for n in group) if d
        )
        batches.append(
            CompanyBatch(
                key=key,
                display=warn_names.display_name(key, _best_raw_name(group)),
                notices=group,
                employees=employees,
                employees_known=bool(counted),
                states=states,
                per_state=per_state,
                sites=len(counted) or len(group),
                first_effective=effectives[0] if effectives else None,
                is_brand=warn_names.is_known_brand(key),
                tags=warn_names.brand_tags(key),
                partial=bool(counted) and len(counted) < len(group),
            )
        )
    # Biggest story first, so a per-run cap keeps the most newsworthy posts.
    batches.sort(key=lambda b: (-b.employees, b.key))
    return batches


# Trailing "(JW Marriott)" / "(Costco store 123)" — a venue or client label
# glued onto the filer's name.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")

# Client/venue separators the DISPLAY path must also cut. warn_names splits the
# clean forms; feeds also write "dba:" with a colon, "(dba)" in brackets,
# "dba/" with a slash, a bare "@", and "operated by" / "operation at" — each of
# which left the client's brand in the printed name.
_DISPLAY_CUT = re.compile(
    r"\bd\s*/?\s*b\s*/?\s*a\b[\s:/,-]*"
    r"|\ba\s*/?\s*k\s*/?\s*a\b[\s:/,-]*"
    r"|\s*@\s*"
    r"|\s+operat(?:ed|ing|ion|ions)\s+(?:by|at|for)\s+",
    re.IGNORECASE,
)

# " at <client>" is only cut for display when the tail actually names a
# registered brand and enough of a name survives on the left. Cutting
# unconditionally would turn the genuine hotel name "Inn at Fox Hollow" into
# "Inn", and "The Scottsdale Resort at McCormick Ranch" into something a reader
# could not identify.
_AT_SPLIT = re.compile(r"\s+\(?at\)?\s+", re.IGNORECASE)
_MIN_DISPLAY_HEAD = 8


def _display_source(raw: str) -> str:
    """The filer's own name, with any client or venue label cut off.

    THIS IS WHY IT EXISTS. ``warn_brands.resolve`` correctly refuses to call
    "Flagship Facility Services Inc. at Meta Platforms Inc." a Meta filing —
    and the post then printed that whole string verbatim, so the account
    published "Flagship Facility Services Inc. at Meta Platforms Inc. filed a
    WARN notice for 300 job cuts". Meta is named in a layoff it has nothing to
    do with. The grouping was right; only the display was wrong, which made it
    invisible to every test that checked the numbers.

    Across the national dataset that was 360 records / 296 distinct strings —
    "HCAL, LLC dba Harrah's Resort Southern California" (1,440 employees),
    "Phoenix Desert Ridege Resort & Spa (JW Marriott)" (923), "Touchstone
    Television Productions, LLC dba ABC Studios" (629) — and 28 of them clear
    the size threshold on their own, so they auto-approve.
    """
    text = warn_names.employer_substring(raw)
    text = _DISPLAY_CUT.split(text)[0]
    text = _PARENTHETICAL.sub(" ", text)
    # An UNTERMINATED "(" is the same label with a truncated feed cell —
    # "Transform SR LLC (Sidney - Sears Retail Store", "Irwin Industries, Inc.
    # (Phillips 66 Coalinga". _PARENTHETICAL needs the closing bracket, so
    # without this the client's brand survives on a technicality.
    if "(" in text:
        text = text.split("(")[0]

    for sep in ("/", " - "):
        head, _, tail = text.partition(sep)
        if tail and warn_brands.resolve(tail) and not warn_brands.resolve(head):
            if len(head.strip()) >= _MIN_DISPLAY_HEAD:
                text = head

    parts = _AT_SPLIT.split(text, maxsplit=1)
    if len(parts) == 2 and len(parts[0].strip()) >= _MIN_DISPLAY_HEAD:
        if warn_brands.resolve(parts[1]) and not warn_brands.resolve(parts[0]):
            text = parts[0]

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*[-,:/]\s*$", "", text).strip()
    return text or raw


def _best_raw_name(group: list) -> str:
    """The tidiest filer name in a group — used when no brand display exists.

    Prefers the shortest, which reliably drops the site and legal tails that
    make the long variants unreadable in a post, then breaks ties
    alphabetically so the choice is stable across runs.

    Every candidate goes through ``_display_source`` FIRST. Picking the
    shortest raw string and cleaning it afterwards is not the same thing: with
    two Flagship rows at different clients the shortest raw string was
    "Flagship Facility Services, Inc. at Google LLC", so which client got named
    in a 500-job post was an accident of string length — and 300 of those 500
    were at Meta.
    """
    names = [_display_source(n.company) for n in group if n.company]
    names = [n for n in names if n]
    if not names:
        return ""
    return sorted(names, key=lambda s: (len(s), s))[0]


# ---------------------------------------------------------------------------
# 3. Score — is this news?
# ---------------------------------------------------------------------------

def score(batch: CompanyBatch) -> Verdict:
    """Decide whether a batch is worth a post, and say which rule fired."""
    if not batch.employees_known:
        # Hawaii and Oklahoma publish no headcount at all. A registered brand
        # filing there is still news; an unknown employer with no number is a
        # post that can say nothing, so it never fires.
        if batch.is_brand:
            return Verdict(True, "brand", "known brand; state reports no headcount")
        return Verdict(False, "none", "no reported headcount")

    n = batch.employees
    if n >= SIZE_THRESHOLD:
        return Verdict(True, "size", f"{n:,} >= {SIZE_THRESHOLD}")
    if "tech" in batch.tags and n >= TECH_THRESHOLD:
        return Verdict(True, "tech", f"tech brand, {n:,} >= {TECH_THRESHOLD}")
    if batch.is_brand and n >= BRAND_THRESHOLD:
        return Verdict(True, "brand", f"known brand, {n:,} >= {BRAND_THRESHOLD}")
    return Verdict(False, "none", f"{n:,} below every threshold")


def select(state_results: dict, latest_for=None) -> list:
    """collect -> group -> score. Returns [(batch, verdict), …] for posters only."""
    notices = collect_new_notices(state_results, latest_for=latest_for)
    out = []
    for batch in group_by_company(notices):
        verdict = score(batch)
        if verdict.post:
            out.append((batch, verdict))
    return out


# ---------------------------------------------------------------------------
# 4. Compose — the post text
# ---------------------------------------------------------------------------
#
# X counts *weighted* characters, not code points: Latin text and punctuation
# weigh 1, everything else (emoji, CJK) weighs 2, and every URL counts as
# exactly 23 however long it is.
# https://docs.x.com/fundamentals/counting-characters

_URL_RE = re.compile(r"https?://\S+", re.I)
_URL_WEIGHT = 23

# The weight-1 ranges from twitter-text's default configuration.
_LIGHT_RANGES = (
    (0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037),
)


def weighted_len(text: str) -> int:
    """X's own character count for a post."""
    if not text:
        return 0
    stripped = _URL_RE.sub("", text)
    total = _URL_WEIGHT * len(_URL_RE.findall(text))
    for ch in stripped:
        cp = ord(ch)
        total += 1 if any(lo <= cp <= hi for lo, hi in _LIGHT_RANGES) else 2
    return total


def _fmt_date(iso) -> Optional[str]:
    """'2026-09-11' -> 'Sep 11, 2026'. None when the feed published no date."""
    parsed = warn_digest.parse_date(iso)
    if parsed is None:
        return None
    return parsed.strftime("%b %-d, %Y")


def _place(notice: Notice) -> str:
    """'City, County, ST' — whatever the state actually publishes.

    Coverage is uneven by design: only 39% of recent records carry a city,
    because California and New York publish county alone. Falling all the way
    back to the state name is correct, not a defect.
    """
    parts = [p for p in (notice.city, notice.county) if p]
    place = ", ".join(dict.fromkeys(parts))
    if place:
        return f"{place}, {notice.state}"
    return warn_digest.STATE_NAMES.get(notice.state, notice.state)


def _hashtag(state: str) -> str:
    name = warn_digest.STATE_NAMES.get(state, state)
    return "#" + re.sub(r"[^A-Za-z]", "", name)


def _sanitize(name: str) -> str:
    """Strip a leading @ so a company name can never become a mention.

    X blocks programmatic @mentions in normal posts (2026-02-23 anti-spam
    change), so a company filed as "@Home Depot" would make the whole post
    fail. Belt and braces: it also stops the account tagging a real handle.
    """
    return re.sub(r"@", "", name or "").strip()


def compose(batch: CompanyBatch, include_link: bool = False) -> Draft:
    """Render a batch as post text.

    Phrased as a FILING, never as an accomplished fact: a WARN notice announces
    *planned* cuts, and some are withdrawn. "filed a WARN notice for N job
    cuts" is defensible; "is cutting N jobs" is not.
    """
    display = _sanitize(batch.display)
    warnings = []
    if batch.partial:
        # Some sites in this batch reported no headcount, so the number in the
        # post covers fewer places than the filing does. True, but easy to
        # misread — a human decides whether it is worth saying.
        warnings.append("partial_headcount")
    link = f"\n\n{warn_urls.US_DASHBOARD_URL}" if include_link else ""

    if not batch.employees_known:
        warnings.append("employees_unknown")
        head = (
            f"{display} filed a WARN notice in {_place(batch.notices[0])}. "
            "Headcount not reported by the state."
        )
        tags = f"\n\n#layoffs {_hashtag(batch.states[0])}"
        return _finish(head + tags + link, warnings)

    n = f"{batch.employees:,}"
    effective = _fmt_date(batch.first_effective)

    # The template is chosen on what we can actually COUNT, not on how many
    # states the filing touches: a batch where only one state published a
    # headcount is a single-site story, and taking the multi branch rendered
    # "across 1 sites in 2 states".
    counted_states = [s for s in batch.states if batch.per_state.get(s)]
    if len(counted_states) <= 1 and batch.sites == 1:
        notice = next(
            (x for x in batch.notices if x.employees), batch.notices[0]
        )
        head = f"{display} filed a WARN notice for {n} job cuts in {_place(notice)}."
        when = (
            f"\n\nEffective {effective}."
            if effective
            else "\n\nEffective date not reported."
        )
        tail = f"\n\n#layoffs {_hashtag(batch.states[0])}"
        return _finish(head + when + tail + link, warnings)

    # Multi-site and/or multi-state.
    if len(counted_states) <= 1:
        only = (counted_states or batch.states)[0]
        head = (
            f"{display} filed WARN notices for {n} job cuts across "
            f"{batch.sites} sites in "
            f"{warn_digest.STATE_NAMES.get(only, only)}."
        )
        tail = f"\n\n#layoffs {_hashtag(only)}"
    else:
        head = (
            f"{display} filed WARN notices for {n} job cuts across "
            f"{batch.sites} sites in {len(counted_states)} states."
        )
        tail = "\n\n#layoffs"

    shown = counted_states[:MAX_INLINE_STATES]
    breakdown = " · ".join(f"{s} {batch.per_state[s]:,}" for s in shown)
    overflow = len(counted_states) - len(shown)
    if overflow > 0:
        breakdown += f" · +{overflow} more"
    when = (
        f"\n\nFirst effective date {effective}."
        if effective
        else "\n\nEffective dates not reported."
    )
    body = f"\n\n{breakdown}" if len(counted_states) > 1 else ""
    text = head + body + when + tail + link

    thread = []
    if overflow > 0:
        full = " · ".join(
            f"{s} {batch.per_state[s]:,}" for s in counted_states
        )
        thread.append(f"Full breakdown for {display}:\n\n{full}")
    return _finish(text, warnings, thread)


def _finish(text: str, warnings: list, thread=None) -> Draft:
    length = weighted_len(text)
    if length > MAX_WEIGHTED_LEN:
        # Refused, never truncated — a post cut mid-number states a wrong
        # figure. warn_x keeps these out of auto-approval.
        warnings = warnings + ["over_budget"]
    return Draft(
        text=text, weighted_len=length, warnings=warnings, thread=thread or []
    )


def build_drafts(state_results: dict, include_link: bool = False,
                 latest_for=None) -> list:
    """The whole pipeline: [(batch, verdict, draft), …], biggest story first."""
    out = []
    for batch, verdict in select(state_results, latest_for=latest_for):
        out.append((batch, verdict, compose(batch, include_link=include_link)))
    return out


def card_fields(batch: CompanyBatch) -> dict:
    """The human-readable strings a post card needs, derived once.

    Kept here rather than in warn_x because they must match the wording of the
    post itself — the card and the text are the same claim in two forms, and a
    card that says a different place or date than the post beside it is worse
    than no card.
    """
    if batch.sites == 1 and len(batch.per_state) <= 1:
        place = _place(batch.notices[0])
    elif len(batch.per_state) <= 1:
        only = (list(batch.per_state) or batch.states)[0]
        place = (f"{batch.sites} sites in "
                 f"{warn_digest.STATE_NAMES.get(only, only)}")
    else:
        shown = [s for s in batch.states if batch.per_state.get(s)]
        place = " · ".join(shown[:4]) + (
            f" +{len(shown) - 4}" if len(shown) > 4 else ""
        )
    return {
        "place": place,
        "effective": _fmt_date(batch.first_effective),
        "employees": batch.employees if batch.employees_known else None,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
