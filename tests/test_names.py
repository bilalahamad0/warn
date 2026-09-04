"""Tests for warn_names + warn_brands — who a WARN filing is actually FROM.

Every case here is a company string that appears in the real national dataset.
Getting one wrong does not produce a crash or a failing number; it produces a
public post naming a company that did not lay anybody off. That is why this
file is mostly a table of names.
"""

import pytest

import warn_brands
import warn_names


# ---------------------------------------------------------------------------
# Spelling variants of one employer must collapse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("AT&T", "AT&T"),
    ("AT&T CORP.", "AT&T"),
    ("At & T", "AT&T"),
    ("At&t", "AT&T"),
    ("AT&T Alabama", "AT&T"),
    ("Fifth Third Bank", "Fifth Third Bank"),
    ("FIFTH THIRD BANK", "Fifth Third Bank"),
    ("Amazon", "Amazon"),
    ("Amazon.com Services LLC", "Amazon"),
    ("Amazon (SJC31)", "Amazon"),
    ("Google Inc.", "Google"),
    ("Meta Platforms, Inc.", "Meta"),
    ("Intel Corporation", "Intel"),
    ("INTEL CORPORATION", "Intel"),
    ("Wal-Mart Stores #1234", "Walmart"),
    ("Boeing Company", "Boeing"),
    # State agencies staple bookkeeping marks onto the company column. Without
    # a rule for them, "(*)UPS" was read as a parenthetical venue label and
    # vetoed as a contractor row.
    ("(*)UPS", "UPS"),
    ("*Updated* Thermo Fisher Scientific", "Thermo Fisher Scientific"),
])
def test_a_brand_is_recognised_however_it_is_spelled(raw, expected):
    assert warn_names.canonical(raw) == expected


# ---------------------------------------------------------------------------
# The contractor rule — the worst error this system can make
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,must_not_be", [
    ("Flagship Facility Services Inc. at Meta Platforms Inc.", "Meta"),
    ("ABM General Services, Inc. at FAT 1 Amazon", "Amazon"),
    ("Restec Contractors Inc. at Facebook", "Meta"),
    ("HMSHost Corporation -Starbucks", "Starbucks"),
    # One deleted space used to defeat the "@" veto entirely.
    ("790 French LLC @ The Hilton Garden Inn", "Hilton"),
    ("790 French LLC @The Hilton Garden Inn", "Hilton"),
    ("790 French LLC@The Hilton Garden Inn", "Hilton"),
    ("790 French LLC (at) The Hilton Garden Inn", "Hilton"),
    # "aka" names a predecessor, not the filer.
    ("LSC Communications US, LLC aka RR Donnelley", "RR Donnelley"),
    # A brand in a TRAILING parenthetical is a venue label too.
    ("Pillar Hotels & Resorts Holiday Inn (Frederick)", "Holiday Inn"),
    ("Artisan Restaurant Collection for Sodexo, Inc.", "Sodexo"),
    ("Amscan Inc., a Party City Holdings, Inc.", "Party City"),
])
def test_the_client_brand_is_never_the_filer(raw, must_not_be):
    """A contractor losing a contract is the contractor's layoff.

    "Flagship Facility Services Inc. at Meta Platforms Inc." is Flagship's
    filing. Posting "Meta filed a WARN notice for N job cuts" from that row is
    the worst factual error this system can make, and it is one regex away at
    all times.
    """
    assert warn_names.canonical(raw) != must_not_be
    assert warn_brands.resolve(raw) != must_not_be


# ---------------------------------------------------------------------------
# Different employers that merely share a word
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,must_not_be", [
    ("Apple Valley Medical Center", "Apple"),
    ("Big Apple Bagels", "Apple"),
    ("Target Logistics Management", "Target"),
    ("Gap Solutions Inc", "Gap"),
    ("Compass Minerals", "Compass Group"),
    ("Sonic Automotive", "Sonic"),
    ("Delta Dental", "Delta Air Lines"),
    ("Delta Faucet Company", "Delta Air Lines"),
    ("Enterprise Products Partners", "Enterprise Rent-A-Car"),
    ("Continental Structural Plastics", "Continental"),
    # Post-Disney; Fox Corporation never employed these people.
    ("Twentieth Century Fox Film Corporation", "Fox"),
    # Spun off from Hilton Worldwide in 2017, separately listed.
    ("Hilton Grand Vacations", "Hilton"),
])
def test_a_shared_word_is_not_a_shared_company(raw, must_not_be):
    assert warn_brands.resolve(raw) != must_not_be


def test_a_dba_of_the_brand_itself_is_still_the_brand():
    """The veto must not overshoot.

    "All Recreational Equipment Inc. dba REI" IS REI — Recreational Equipment,
    Inc. is its legal name, so the dba names the same company, not a client.
    canonical() gets this right because it resolves the EMPLOYER substring,
    which is where the legal name lives.
    """
    assert warn_names.canonical("All Recreational Equipment Inc. dba REI") == "REI"


# ---------------------------------------------------------------------------
# normalize() — the grouping key for everyone unregistered
# ---------------------------------------------------------------------------

def test_a_generic_noun_is_not_an_identity():
    """"university" is a kind of employer, not one.

    _SPLIT_AT strips " at CSU Northridge", _strip_suffixes eats "Corporation"
    and "^the " goes — collapsing two different CSU auxiliary corporations onto
    one key, which posts one combined number under whichever raw name happened
    to be shortest.
    """
    north = warn_names.canonical("The University Corporation at CSU Northridge")
    monterey = warn_names.canonical("The University Corporation at Monterey Bay")
    assert north not in warn_names._GENERIC_KEYS
    assert north != monterey


def test_the_same_auxiliary_still_groups_with_itself():
    assert warn_names.canonical(
        "The University Corporation at Monterey Bay"
    ) == warn_names.canonical("University Corporation at Monterey Bay")


@pytest.mark.parametrize("a,b", [
    ("Compass Group USA, Inc.", "Compass Minerals"),
    ("Enterprise Rent-A-Car", "Enterprise Products Partners"),
    ("West Coast Prime Meats", "West Corporation"),
])
def test_normalisation_does_not_merge_unrelated_employers(a, b):
    """LEGAL is deliberately narrow.

    Adding "group"/"holdings"/"usa"/"services" to it turns "Compass Group USA"
    into "compass", which then merges with any firm named Compass.
    """
    assert warn_names.normalize(a) != warn_names.normalize(b)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", None, "   ", "\t\n", 12345, b"Amazon",
                                   0, [], {}, "(((((", "(?i)", ".*", "$^", "\\"])
def test_resolve_never_raises(value):
    """The docstring promises "or None", and warn_brands is a standalone leaf.

    A source module that skips warn_x_select's str() coercion must not take the
    whole pipeline down on an int or a bytes cell.
    """
    assert warn_brands.resolve(value) is None or isinstance(
        warn_brands.resolve(value), str
    )


def test_a_huge_cell_is_capped():
    """resolve() runs 633 patterns per call; a malformed 1 MB feed cell would
    otherwise cost seconds per record."""
    assert warn_brands.resolve("Amazon " + "z" * 500_000) is not None


def test_no_canonical_collides_with_the_normalize_keyspace():
    """Brand canonicals and normalize keys share one namespace.

    They are kept apart only by every canonical being Title-cased. A lowercase
    one ("bebe") means any unregistered employer normalising to that string is
    treated as the brand — inheriting its tags and its lower posting bar.
    """
    clashes = [
        b for b in warn_brands.all_brands() if warn_names.normalize(b) == b
    ]
    assert clashes == []


def test_a_status_word_does_not_disable_the_vetoes():
    """_LEADING_NOISE matching means "nothing is in front", which SKIPS the
    contractor and multi-brand vetoes. Accepting bare "new"/"final" therefore
    turned an ordinary English word into a veto-disable switch."""
    assert warn_brands.resolve("New Horizons LLC at Costco Wholesale") is None
    assert warn_brands.resolve("Final Touch Services, Inc. dba Costco") is None


def test_resolve_is_deterministic():
    probes = ["Amazon", "Aramark at General Mills 2024", "AT&T CORP.",
              "Compass Group USA, Inc.", "790 French LLC @ The Hilton"]
    once = [warn_brands.resolve(p) for p in probes]
    assert once == [warn_brands.resolve(p) for p in probes]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_tags_are_membership_testable():
    """tags() returned the raw sector string as one member, so "tech" in tags
    was False for every brand — which quietly made warn_x_select's tech arm
    dead code."""
    assert "tech" in warn_names.brand_tags("Intel")
    assert "tech" in warn_names.brand_tags("Amazon")
    assert "finance" in warn_names.brand_tags("Fifth Third Bank")
    assert warn_names.brand_tags("Totally Unknown LLC") == set()


def test_a_tech_brand_exists_for_the_tech_arm_to_find():
    tech = [b for b in warn_brands.all_brands()
            if "tech" in warn_brands.tags(b)]
    assert len(tech) > 50


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def test_display_name_never_prints_a_normalisation_artifact():
    assert warn_names.display_name(warn_names.canonical("AT&T CORP.")) == "AT&T"
    assert warn_names.display_name("some unknown key", "Some Unknown LLC") == \
        "Some Unknown LLC"
