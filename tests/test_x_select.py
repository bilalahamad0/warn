"""Tests for warn_x_select — collecting, grouping, scoring and wording.

No network, no credentials, no data/ access: every test builds its own records.
Assertions are on behaviour (does AT&T group with At & T?) rather than on the
literal canonical key, so the brand registry can grow without churning tests.
"""

import json

import pytest

import warn_x_select as sel
from warn_x_select import Notice


def rec(company="Widget Corp", employees=400, effective="2026-10-01",
        notice_date=None, city="", county="", **kw):
    """A record in the unified schema, as a state source emits it."""
    return {
        "company": company, "employees": employees,
        "notice_date": notice_date, "effective_date": effective,
        "city": city, "county": county, "industry": "", **kw
    }


def notice(company="Widget Corp", state="CA", employees=400,
           effective="2026-10-01", **kw):
    return Notice(
        state=state, company=company, employees=employees, notice_date=None,
        effective_date=effective, key=f"{state}:{company}__{effective}",
        **kw
    )


# ---------------------------------------------------------------------------
# Character counting — X's rules, not Python's len()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("hello", 5),
    ("a\nb", 3),                                   # newline weighs 1
    ("https://bilalahamad0.github.io/warn/", 23),  # any URL is exactly 23
    ("https://x.co/a", 23),                        # …however short
    ("📉", 2),                                      # emoji weigh 2
    ("A · B", 5),                                  # U+00B7 is a light char
])
def test_weighted_len_follows_x_counting(text, expected):
    assert sel.weighted_len(text) == expected


def test_weighted_len_of_a_url_is_fixed_not_measured():
    """t.co rewrites every URL, so a long one costs no more than a short one."""
    short = sel.weighted_len("see https://x.co/a")
    long = sel.weighted_len("see https://example.com/" + "z" * 200)
    assert short == long


# ---------------------------------------------------------------------------
# Collect — across every state, and the 50-record truncation
# ---------------------------------------------------------------------------

def test_collect_spans_every_state():
    results = {
        "mi": {"state": "MI", "diff": {"new_count": 1, "new_keys": ["k1"],
                                       "new_entries": [rec("Fifth Third Bank")]}},
        "nj": {"state": "NJ", "diff": {"new_count": 1, "new_keys": ["k2"],
                                       "new_entries": [rec("AT&T")]}},
        "ca": {"state": "CA", "diff": {"new_count": 0}},
    }
    got = sel.collect_new_notices(results)
    assert {n.state for n in got} == {"MI", "NJ"}


def test_notices_carry_their_state():
    """detect_changes knows nothing about states; the loop key is the only source."""
    results = {"nj": {"state": "NJ", "diff": {
        "new_count": 1, "new_keys": ["k"], "new_entries": [rec("AT&T")]}}}
    assert sel.collect_new_notices(results)[0].state == "NJ"
    assert sel.collect_new_notices(results)[0].key.startswith("NJ:")


def test_truncated_new_entries_are_recovered_from_latest(tmp_path):
    """The trap: new_entries stops at 50 while new_count and new_keys do not.

    A state landing 93 new notices hands over 50 records. Posting only those
    understates the company's headcount with nothing looking wrong.
    """
    records = [rec(f"Big Co {i}", employees=10) for i in range(93)]
    keys = [f"Big Co {i}__2026-10-01__10" for i in range(93)]
    latest = tmp_path / "warn_latest.json"
    latest.write_text(json.dumps({"records": records}))

    results = {"ca": {"state": "CA", "diff": {
        "new_count": 93,
        "new_keys": keys + ["an-amendment-key"],   # amendments trail the list
        "new_entries": records[:50],
    }}}
    got = sel.collect_new_notices(results, latest_for=lambda code: latest)
    assert len(got) == 93


def test_recovery_falls_back_to_what_it_was_given(tmp_path, caplog):
    """No latest file to read: use the 50 we have, and say so."""
    results = {"ca": {"state": "CA", "diff": {
        "new_count": 93, "new_keys": ["k"] * 93,
        "new_entries": [rec("Big Co", employees=10)] * 50,
    }}}
    got = sel.collect_new_notices(results, latest_for=lambda code: None)
    assert len(got) == 50
    assert "understated" in caplog.text


# ---------------------------------------------------------------------------
# Group — the user's core requirement
# ---------------------------------------------------------------------------

def test_one_company_across_states_is_one_batch():
    """'Combined number of layoffs for that company across all cities/states'."""
    batches = sel.group_by_company([
        notice("Amazon", "CA", 340), notice("Amazon", "TX", 380),
        notice("Amazon", "NJ", 250),
    ])
    assert len(batches) == 1
    b = batches[0]
    assert b.employees == 970
    assert set(b.states) == {"CA", "TX", "NJ"}
    assert b.per_state == {"CA": 340, "TX": 380, "NJ": 250}
    assert b.sites == 3


def test_states_are_ordered_by_size():
    """The inline breakdown should lead with the biggest number."""
    b = sel.group_by_company([
        notice("Amazon", "NJ", 250), notice("Amazon", "TX", 380),
        notice("Amazon", "CA", 610),
    ])[0]
    assert b.states == ["CA", "TX", "NJ"]


@pytest.mark.parametrize("variants", [
    ["AT&T", "AT&T CORP.", "At & T", "At&t"],
    ["Fifth Third Bank", "FIFTH THIRD BANK"],
    ["Intel Corporation", "INTEL CORPORATION", "Intel"],
])
def test_spelling_variants_of_one_employer_collapse(variants):
    """A wrong collapse here is a wrong headline number."""
    batches = sel.group_by_company(
        [notice(v, "CA", 100, effective=f"2026-10-{i + 1:02d}")
         for i, v in enumerate(variants)]
    )
    assert len(batches) == 1
    assert batches[0].employees == 100 * len(variants)


def test_a_contractor_is_never_merged_into_the_client_brand():
    """The worst factual error this system can make.

    "Flagship Facility Services Inc. at Meta Platforms Inc." is Flagship's
    filing — a contractor losing a contract — not Meta's.
    """
    batches = sel.group_by_company([
        notice("Meta Platforms, Inc.", "CA", 500),
        notice("Flagship Facility Services Inc. at Meta Platforms Inc.",
               "CA", 300, effective="2026-11-01"),
    ])
    assert len(batches) == 2
    meta = [b for b in batches if b.employees == 500][0]
    assert "Flagship" not in meta.display
    assert meta.employees == 500          # NOT 800


@pytest.mark.parametrize("a,b", [
    ("Compass Group USA, Inc.", "Compass Minerals"),
    ("Enterprise Rent-A-Car", "Enterprise Products Partners"),
    ("Apple Inc.", "Apple Valley Medical Center"),
    ("Target Corporation", "Target Logistics Management"),
])
def test_different_employers_sharing_a_word_stay_apart(a, b):
    batches = sel.group_by_company([
        notice(a, "CA", 300), notice(b, "TX", 300, effective="2026-11-01"),
    ])
    assert len(batches) == 2


def test_display_name_never_prints_a_lowercase_key():
    """A post says "AT&T", never "at and t"."""
    for raw in ("AT&T CORP.", "FIFTH THIRD BANK", "SOME UNKNOWN LLC"):
        b = sel.group_by_company([notice(raw, "CA", 300)])[0]
        assert b.display
        assert b.display != b.display.lower() or not b.display.isalpha()


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

def _score(company, employees, state="CA"):
    return sel.score(sel.group_by_company(
        [notice(company, state, employees)])[0])


def test_size_arm_fires_for_any_employer():
    assert _score("Totally Unknown LLC", sel.SIZE_THRESHOLD).post is True
    assert _score("Totally Unknown LLC", sel.SIZE_THRESHOLD - 1).post is False


def test_a_recognisable_brand_posts_below_the_size_threshold():
    """Both notices that prompted this feature are below 250."""
    v = _score("Fifth Third Bank", 234)
    assert v.post is True and v.arm in ("brand", "tech")
    v = _score("AT&T", 138, state="NJ")
    assert v.post is True and v.arm in ("brand", "tech")


def test_a_small_unknown_employer_never_posts():
    assert _score("Joe's Diner", 62).post is False


def test_a_brand_still_needs_to_clear_the_floor():
    """Chains file single-digit store closures constantly."""
    assert _score("Walmart", sel.BRAND_THRESHOLD - 1).post is False


def test_a_brand_posts_when_the_state_publishes_no_headcount():
    """Hawaii and Oklahoma report none. A known name is still news."""
    b = sel.group_by_company([notice("AT&T", "HI", 0)])[0]
    assert b.employees_known is False
    assert sel.score(b).post is True


def test_an_unknown_employer_with_no_headcount_never_posts():
    """There would be nothing to say."""
    b = sel.group_by_company([notice("Some Local LLC", "HI", 0)])[0]
    assert sel.score(b).post is False


def test_zero_means_not_reported_not_zero_people():
    """A real WARN filing never affects zero workers."""
    b = sel.group_by_company([notice("Widget Corp", "OK", 0)])[0]
    assert b.employees_known is False


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

def compose(company="Widget Corp", state="CA", employees=400, **kw):
    b = sel.group_by_company([notice(company, state, employees, **kw)])[0]
    return sel.compose(b, include_link=False)


def test_a_post_is_phrased_as_a_filing_not_a_fact():
    """A WARN notice announces PLANNED layoffs, and some are withdrawn."""
    text = compose().text
    assert "filed a WARN notice" in text
    assert "is cutting" not in text


def test_the_headcount_is_formatted_with_separators():
    assert "1,240" in compose(employees=1240).text


def test_a_missing_effective_date_is_stated_not_hidden():
    """Arizona, Illinois, Oklahoma and Utah publish none."""
    assert "not reported" in compose(effective=None).text


def test_a_missing_headcount_is_stated_not_zeroed():
    b = sel.group_by_company([notice("AT&T", "HI", 0)])[0]
    text = sel.compose(b, include_link=False).text
    assert "Headcount not reported" in text
    assert " 0 " not in text


def test_place_falls_back_city_then_county_then_state():
    """Only 39% of recent records carry a city — CA and NY publish county only."""
    assert "Fresno, CA" in compose(city="Fresno").text
    assert "Oakland, MI" in compose(state="MI", county="Oakland").text
    assert "California" in compose().text        # neither published


def test_a_company_name_can_never_become_a_mention():
    """X blocks programmatic @mentions in normal posts (2026-02-23)."""
    text = compose(company="@HomeDepot Services").text
    assert "@" not in text


def test_a_multi_state_post_shows_the_breakdown():
    b = sel.group_by_company([
        notice("Amazon", "CA", 610), notice("Amazon", "TX", 380),
        notice("Amazon", "NJ", 250),
    ])[0]
    text = sel.compose(b, include_link=False).text
    assert "1,240" in text
    assert "3 sites in 3 states" in text
    for fragment in ("CA 610", "TX 380", "NJ 250"):
        assert fragment in text


def test_a_wide_batch_overflows_into_a_self_reply():
    """X confirmed on 2026-02-24 that replying to your own posts still works."""
    states = ["TX", "CA", "FL", "NY", "OH", "GA", "NC", "VA", "MD", "AZ"]
    b = sel.group_by_company(
        [notice("Widespread Co", s, 100 + i) for i, s in enumerate(states)]
    )[0]
    draft = sel.compose(b, include_link=False)
    assert "+2 more" in draft.text
    assert len(draft.thread) == 1
    for s in states:
        assert s in draft.thread[0]
    assert sel.weighted_len(draft.thread[0]) <= sel.MAX_WEIGHTED_LEN


def test_an_over_long_post_is_refused_not_truncated():
    """A post cut mid-number states a wrong figure."""
    draft = compose(company="X" * 400)
    assert draft.weighted_len > sel.MAX_WEIGHTED_LEN
    assert "over_budget" in draft.warnings


def test_every_composed_post_fits():
    """The realistic shapes must all be inside budget, link included."""
    cases = [
        [notice("Fifth Third Bank", "MI", 234, county="Oakland")],
        [notice("AT&T", "NJ", 138, city="Cumberland County")],
        [notice("Amazon", s, 300) for s in ("CA", "TX", "NJ", "NY")],
        [notice("Republic National Distributing Company", s, 120)
         for s in ("TX", "CA", "FL", "NY", "OH", "GA", "NC")],
    ]
    for notices in cases:
        for b in sel.group_by_company(notices):
            draft = sel.compose(b, include_link=True)
            assert draft.weighted_len <= sel.MAX_WEIGHTED_LEN, draft.text
            assert "over_budget" not in draft.warnings


def test_build_drafts_only_returns_postable_batches():
    results = {"ca": {"state": "CA", "diff": {
        "new_count": 2, "new_keys": ["a", "b"],
        "new_entries": [rec("Fifth Third Bank", 234), rec("Joe's Diner", 12)],
    }}}
    drafts = sel.build_drafts(results, include_link=False)
    assert len(drafts) == 1
    assert "Fifth Third" in drafts[0][2].text


# ---------------------------------------------------------------------------
# The display name — what the post actually calls the company
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,must_not_contain", [
    ("Flagship Facility Services Inc. at Meta Platforms Inc.", "Meta"),
    ("HCAL, LLC dba Harrah's Resort Southern California", "Harrah"),
    ("Phoenix Desert Ridege Resort & Spa (JW Marriott)", "Marriott"),
    ("Touchstone Television Productions, LLC dba ABC Studios", "ABC"),
    ("D. Longo, LLC dba: Longo Toyota", "Toyota"),
    ("790 French LLC @ The Hilton Garden Inn Times Square", "Hilton"),
    ("Transform SR LLC (Sidney - Sears Retail Store", "Sears"),
    ("Bon Appetit Management Co. operation at Airbnb", "Airbnb"),
])
def test_a_post_never_prints_the_client_brand(raw, must_not_contain):
    """The grouping was right and the number was right — the NAME was wrong.

    warn_brands correctly refuses to call these filings the client's, and the
    post then printed the whole raw string anyway, publishing "Flagship
    Facility Services Inc. at Meta Platforms Inc. filed a WARN notice for 300
    job cuts". Because only the display was wrong, every test that checked the
    numbers passed. Across the national dataset this was 360 records, 28 of
    them large enough to auto-approve.
    """
    b = sel.group_by_company([notice(raw, "CA", 300)])[0]
    assert must_not_contain.lower() not in sel.compose(b, False).text.lower()


def test_which_client_gets_named_is_not_decided_by_string_length():
    """Two rows from one contractor at two clients.

    _best_raw_name picked the SHORTEST raw string, so the post named Google for
    a 500-job total of which 300 were at Meta.
    """
    b = sel.group_by_company([
        notice("Flagship Facility Services, Inc. at Google LLC", "CA", 200),
        notice("Flagship Facility Services Inc. at Meta Platforms Inc.", "CA",
               300, effective="2026-11-01"),
    ])[0]
    text = sel.compose(b, False).text
    assert "Google" not in text and "Meta" not in text
    assert "Flagship" in text


def test_a_genuine_place_name_survives():
    """The cut must not overshoot. "Inn at Fox Hollow" is the venue's own name,
    and Fox Hollow is a place — cutting at " at " would print "Inn"."""
    b = sel.group_by_company([notice("Inn at Fox Hollow", "NY", 300)])[0]
    assert b.display == "Inn at Fox Hollow"


# ---------------------------------------------------------------------------
# Partial headcounts
# ---------------------------------------------------------------------------

def test_a_state_that_reported_nothing_is_not_reported_as_zero():
    """"CA 610 · TX 0" is meaningless to a reader, and it presented 610 as
    covering three sites when only one published a number."""
    b = sel.group_by_company([
        notice("Globex Corp", "CA", 610),
        notice("Globex Corp", "TX", 0),
        notice("Globex Corp", "TX", 0, effective="2026-11-02"),
    ])[0]
    draft = sel.compose(b, False)
    assert "TX 0" not in draft.text
    assert " 0 job cuts" not in draft.text
    assert "1 sites" not in draft.text
    assert b.partial is True
    assert "partial_headcount" in draft.warnings


def test_the_state_list_still_covers_every_state():
    """per_state is restricted to states that published a number; states is
    not, so the hashtag and the state count stay true."""
    b = sel.group_by_company([
        notice("Globex Corp", "CA", 610), notice("Globex Corp", "TX", 0),
    ])[0]
    assert set(b.states) == {"CA", "TX"}
    assert b.per_state == {"CA": 610}


def test_a_batch_with_no_headcount_anywhere_still_composes():
    """Hawaii and Oklahoma publish none at all — states must not come back
    empty, or the hashtag lookup raises."""
    b = sel.group_by_company([notice("AT&T", "HI", 0)])[0]
    assert b.states == ["HI"]
    assert sel.compose(b, False).text


# ---------------------------------------------------------------------------
# The tech arm
# ---------------------------------------------------------------------------

def test_the_tech_arm_can_actually_fire():
    """It was dead code: TECH_THRESHOLD equalled BRAND_THRESHOLD and every tech
    brand is a registered brand, so the branch only ever relabelled a verdict
    the brand arm had already reached."""
    assert sel.TECH_THRESHOLD < sel.BRAND_THRESHOLD
    v = sel.score(sel.group_by_company(
        [notice("Microsoft", "WA", sel.TECH_THRESHOLD)])[0])
    assert v.post is True and v.arm == "tech"


def test_a_non_tech_brand_at_the_tech_threshold_does_not_post():
    v = sel.score(sel.group_by_company(
        [notice("Kroger", "OH", sel.TECH_THRESHOLD)])[0])
    assert v.post is False


def test_a_post_carries_no_link_by_default():
    """The dashboard is on a github.io address.

    A raw project-hosting URL under a layoff headline reads as a hobby page
    rather than a source, so posts sign themselves with the account's name and
    the link waits for a real domain. X_INCLUDE_LINK=1 turns it back on.
    """
    text = compose().text
    assert "http" not in text
    assert "github.io" not in text


def test_the_link_can_still_be_turned_on():
    b = sel.group_by_company([notice()])[0]
    assert "http" in sel.compose(b, include_link=True).text
