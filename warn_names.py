"""
warn_names
----------
Company-name canonicalisation for the WARN pipeline.

One employer files under many spellings. California alone carries ``AT&T``,
``AT&T CORP.``, ``At&t``, ``At & T`` and ``AT&T - 5001``; Michigan writes
``Fifth Third Bank`` and ``FIFTH THIRD BANK``. Anything that needs to answer
"how many jobs did *this company* cut in this batch" has to collapse those
first — the X poster (``warn_x_select``) is the current caller, and its
headline number is wrong if the collapse is wrong.

Two layers, in this order:

1. ``normalize`` — deterministic, corpus-free. Case, unicode, punctuation,
   parentheticals, store numbers, ``dba``/``at`` splits, trailing legal forms.
   Conservative on purpose (see LEGAL below).
2. ``warn_brands`` — an explicit registry that maps a company string onto a
   curated brand. Only this layer may merge ``Amazon.com Services LLC`` into
   ``Amazon``: pure normalisation cannot know that initialisms, renames and
   subsidiaries name one company.

``canonical`` runs both and is what callers want.

ORDER MATTERS, AND IT IS BRAND FIRST. ``warn_brands`` reads the RAW string —
its patterns and its contractor veto were validated against the corpus with
casing, punctuation and "dba" tails intact, and normalising first would
delete the evidence the veto reads ("(*)UPS" is UPS; "790 French LLC @ The
Hilton Garden Inn" is not Hilton). So ``canonical`` hands ``warn_brands`` the
employer substring raw, and only falls back to ``normalize`` — still the
grouping key for every unregistered company — when no brand claims it.

Imports nothing from the project except ``warn_brands`` (itself a leaf) —
keep it that way, so this module stays cheap enough to call per record.
"""

import collections
import re
import unicodedata

import warn_brands

# Trailing legal-entity forms, stripped repeatedly off the end of a name.
#
# DELIBERATELY NARROW — only actual legal forms. It is tempting to add
# "group", "holdings", "usa" and "services", and that is exactly the bug:
# stripping them turns "Compass Group USA" into "compass", which then merges
# with any firm literally named Compass, and "Enterprise Products" and
# "Enterprise Rent-A-Car" into one company. Those are different employers and
# a merged post states a false headcount under a real brand's name. Brand-level
# merging is warn_brands' job, where each merge is written down and tested.
LEGAL = (
    r"(?:incorporated|inc|llc|l l c|lllp|llp|lp|ltd|limited|corp|corporation"
    r"|corportation|company|companies|co|plc|pllc|pc|n a|na|sa|ag|gmbh)"
)

# "X LLC dba Y" / "X fka Y" / "X aka Y" — the filer is X.
#
# "aka" is load-bearing and was missing: "LSC Communications US, LLC aka RR
# Donnelley" (571 employees) resolved to RR Donnelley, the predecessor LSC was
# spun out of — a company that did not file and no longer employed anyone
# there.
_SPLIT_DBA = re.compile(
    r"\s+(?:d\s*/?\s*b\s*/?\s*a|dba|f\s*/?\s*k\s*/?\s*a|fka"
    r"|a\s*/?\s*k\s*/?\s*a|aka|formerly known as|also known as)\s+",
    re.I,
)

# " at <client>" splits only when the LEFT side names a legal entity or a
# staffing/contract operator.
#
# LOAD-BEARING, twice over. First, an ungated split is wrong: 543 company
# strings contain " at " and 282 carry no entity token, so it would turn
# "Help at Home" into "help" and "Resort At Squaw Creek" into "resort".
# Second, the side it keeps is the point. "Flagship Facility Services Inc. at
# Meta Platforms Inc." is FLAGSHIP's filing — a janitorial contractor losing a
# contract — not Meta's. Posting "Meta filed a WARN notice for N job cuts" off
# that record is the worst factual error this system can make, and it is one
# regex away at all times.
_SPLIT_AT = re.compile(
    r"^(?P<lhs>.*?\b(?:inc|llc|l\.l\.c|corp|corporation|co|company|companies|"
    r"ltd|lp|llp|services|service|group|partners|associates|solutions|"
    r"staffing|contractors?)\b\.?,?)\s+at\s+\S",
    re.I,
)

# A facility code must CONTAIN a digit. An earlier [a-z]{2,5} alternative
# merged "WEST COAST PRIME MEATS", "WEST CORPORATION" and "WEST GROUP" into
# "west", and "FLEX-N-GATE-ALABAMA" into "flex".
_FACILITY = re.compile(r"^(?:[a-z]{1,6}\s?-?\s?\d{1,5}[a-z]?|\d{1,6})$")

# Generic site words that may be trimmed off a tail by trim_tail().
_TAILWORD = {
    "rif", "facility", "facilities", "store", "stores", "location",
    "locations", "site", "sites", "division", "dc", "plant", "center",
    "centre", "closure", "layoff", "layoffs", "hq", "headquarters",
    "office", "branch", "warehouse", "mall", "campus",
}

# Status markers some feeds prepend. Without this, "*Updated* Community
# Healthlink, Inc." canonicalises to "updated community healthlink" and never
# groups with the original filing.
_STATUS = re.compile(r"^\s*\*\s*(?:updated|revised|amended|corrected)\s*\*\s*", re.I)

# Two feeds leak raw HTML into the company column (SRG Global Coatings,
# National Pen). Recover the title= attribute rather than canonicalising CSS.
_HTML_LEAK = re.compile(r'title="([^"]+)"')

_UNICODE_FOLD = (
    ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
    ("–", "-"), ("—", "-"), (" ", " "),
)


def _clean_unicode(s: str) -> str:
    """Fold smart quotes/dashes/nbsp and strip combining accents."""
    for a, b in _UNICODE_FOLD:
        s = s.replace(a, b)
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _strip_suffixes(s: str) -> str:
    """Peel trailing legal forms, store numbers and dangling punctuation."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"[\s\-]+" + LEGAL + r"$", "", s).strip()
        s = re.sub(r"[\s\-]+\d{1,6}$", "", s).strip()
        s = re.sub(r"\s*[-,]\s*$", "", s).strip()
    return s


def employer_substring(name) -> str:
    """The part of a raw company string that names the FILING employer.

    Everything ``normalize`` does before it starts lowercasing and stripping
    punctuation — unicode folding, the HTML leak, the "*Updated*" marker, the
    "(*)" mark, and the ``dba``/``at`` splits — and nothing after it. Casing,
    punctuation and internal parentheticals survive.

    They have to. ``warn_brands`` reads this string, and its contractor veto
    is looking at exactly the characters normalisation destroys: the "(" in
    "Housing Works Inc. (at Holiday Inn ...)" and the " @ " in "790 French LLC
    @ The Hilton Garden Inn" are the evidence that the brand named there is
    the venue, not the employer. Hand the registry a normalised key instead
    and it posts the hotel's name over a nonprofit's layoff.
    """
    if not name:
        return ""
    s = _clean_unicode(str(name))
    m = _HTML_LEAK.search(s)
    if m:
        s = m.group(1)
    s = _STATUS.sub("", s)
    s = re.sub(r"^\s*\(\*\)\s*", "", s)          # "(*)UPS"
    s = _SPLIT_DBA.split(s)[0]                   # "X LLC dba Y" -> X
    at = _SPLIT_AT.match(s)
    if at:                                       # "X Inc. at Y" -> X
        s = at.group("lhs")
    return s.strip()


# Words that name a KIND of organisation, not one. A key that has been reduced
# to a single one of these is not an identity, and grouping on it merges
# unrelated employers under whichever raw name happens to be shortest.
#
# Real case: _SPLIT_AT strips " at CSU Northridge", _strip_suffixes eats
# "Corporation" and "^the " goes, so BOTH "The University Corporation at CSU
# Northridge" (398 employees) and "The University Corporation at Monterey Bay"
# (134) collapse to "university" — different CSU auxiliaries, one post, one
# wrong total, one arbitrarily-chosen name.
_GENERIC_KEYS = frozenset((
    "university", "college", "school", "schools", "district", "hospital",
    "hospitals", "medical", "health", "healthcare", "clinic", "center",
    "centre", "services", "service", "group", "holdings", "resort", "resorts",
    "hotel", "hotels", "restaurant", "restaurants", "cafe", "management",
    "partners", "associates", "solutions", "systems", "enterprises",
    "industries", "foundation", "institute", "company", "corporation",
    "store", "stores", "market", "markets", "bank", "credit union", "church",
    "staffing", "transport", "transportation", "logistics", "manufacturing",
))


def normalize(name) -> str:
    """Deterministic, corpus-free canonicalisation of one company string.

    Returns a lowercase key. Never raises: junk in, empty string out.
    """
    s = employer_substring(name)
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)             # drop parentheticals
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = s.lower()
    s = re.sub(r"['’]", "", s)              # macy's -> macys, no space
    s = s.replace("&", " and ")
    s = re.sub(r'[.,"/]', " ", s)
    s = re.sub(r"[^a-z0-9#\- ]", " ", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\b(?:stores?|facility|store no|no)\s*#?\s*\d+\b", " ", s)
    s = re.sub(r"#\s*\d+", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)    # "Walgreens 2023"
    s = re.sub(r"\s+", " ", s).strip()
    key = _strip_suffixes(s)
    if key in _GENERIC_KEYS:
        # Too generic to be an identity. Fall back to the un-split name, which
        # still distinguishes "University Corporation at Monterey Bay" from
        # "…at CSU Northridge" — a longer key groups less, and grouping too
        # little only costs a post, while grouping too much states a false
        # number under a real name.
        full = _basic_key(_clean_unicode(str(name)))
        return full or key
    return key


def _basic_key(text: str) -> str:
    """The lowercase/punctuation pass alone — no dba/at split, no stoplist."""
    s = re.sub(r"\([^)]*\)", " ", text)
    s = re.sub(r"\[[^\]]*\]", " ", s).lower()
    s = re.sub(r"['’]", "", s).replace("&", " and ")
    s = re.sub(r'[.,"/]', " ", s)
    s = re.sub(r"[^a-z0-9#\- ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the\s+", "", s)


def build_vocab(names) -> collections.Counter:
    """Stem frequencies over a corpus of raw company strings, for trim_tail."""
    v = collections.Counter()
    for n in names:
        k = normalize(n)
        if k:
            v[k] += 1
    return v


def trim_tail(key: str, vocab, places=frozenset(), min_stem: int = 3) -> str:
    """Drop a trailing site/city/facility qualifier from a normalised key.

    Only fires when the surviving head is itself an established stem in this
    corpus (``vocab[head] >= min_stem``), which is what keeps it from eating
    real two-word company names. Opt-in: it needs a corpus, and the X poster
    runs without one because ``warn_brands`` already covers the chains whose
    filings carry site suffixes.
    """
    if not key:
        return key
    changed = True
    while changed:
        changed = False
        # "wal - mart" -> "walmart" when the joined form is the commoner stem.
        if " - " in key:
            joined = key.replace(" - ", "")
            spaced = key.replace(" - ", " ")
            best = max((joined, spaced), key=lambda c: vocab.get(c, 0))
            if vocab.get(best, 0) > vocab.get(key, 0):
                key, changed = best, True
                continue
        toks = key.split()
        for cut in range(len(toks) - 1, 0, -1):
            head = _strip_suffixes(" ".join(toks[:cut]).rstrip(" -"))
            tail = " ".join(toks[cut:]).strip(" -")
            if not head or len(head) < 3 or not tail:
                continue
            if vocab.get(head, 0) < min_stem:
                continue
            parts = [w for w in tail.replace(" - ", " ").split() if w and w != "-"]
            if (
                parts
                and all(
                    w in places or w in _TAILWORD or _FACILITY.match(w)
                    for w in parts
                )
            ) or tail in places:
                key, changed = head, True
                break
    return key


def canonical(name, vocab=None, places=frozenset()) -> str:
    """Brand, else normalised key. The caller's entry point.

    Brand first, on the RAW employer substring — see ORDER MATTERS above. A
    registered brand returns its canonical, which is already the printable
    name ("AT&T CORP." -> "AT&T"). Everything else falls through to
    ``normalize`` (plus ``trim_tail`` when a corpus was supplied), so an
    unregistered employer still groups across its own spellings.
    """
    brand = warn_brands.resolve(employer_substring(name))
    if brand:
        return brand
    key = normalize(name)
    if vocab:
        key = trim_tail(key, vocab, places)
    return key


def display_name(key: str, fallback: str = "") -> str:
    """Human-facing name for a canonical key — what a post actually prints.

    A registered brand's canonical IS its display form, so ``warn_brands``
    hands it straight back ("AT&T" -> "AT&T"). For everything else the raw
    company string is far better copy than the lowercased key, so the caller
    passes one as ``fallback``.
    """
    known = warn_brands.display(key)
    if known:
        return known
    return (fallback or key).strip()


def is_known_brand(key: str) -> bool:
    """True when the canonical key is a curated brand."""
    return warn_brands.is_brand(key)


def brand_tags(key: str) -> set:
    """Tag set for a curated brand ({"tech"}, {"finance"}, ...); empty if unknown."""
    return warn_brands.tags(key)
