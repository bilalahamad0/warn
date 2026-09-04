"""
warn_brands
-----------
Registry of employers whose name a general US audience recognises.

The X poster (``warn_x_select``) uses this for two things: to state a
company's name properly in a post ("AT&T", never "at and t"), and to decide
that a modest filing is still news because of *whose* filing it is. A 138-job
notice from an unknown LLC is not news; the same notice from AT&T is.

``resolve`` takes the RAW company string the state published, not a
normalised key. Every pattern here was written and validated against the
40,956 distinct company strings in the national dataset with their casing,
punctuation and "dba" tails intact — that context is what the vetoes below
read, and normalising it away first would delete the evidence they need.
(``warn_names.canonical`` still splits a "``X LLC dba Y``" / "``X Inc. at Y``"
string down to the filer before calling in; it hands over that substring raw.)

Fields
    canonical   grouping key, the registry's own id, AND what a post prints —
                it has to read correctly in "<canonical> filed a WARN notice
                for 234 job cuts in Oakland, MI."
    pattern     positive regex, applied with ``re.search`` under IGNORECASE
    negative    veto regex, "" when unused; a match disqualifies the entry
    tech        product is software, an internet platform, semiconductors or
                computing/communications hardware — see TECH RULE
    tier        1 household name nationally · 2 well known in its field or
                region · 3 notable but niche
    sector      the sector list this entry came from (provenance, not a claim)

A false positive here names an innocent company in a public layoff post, so
every rule trades coverage for precision and the module returns None rather
than guess.

DETERMINISTIC WINNER RULE
    A company string may legitimately name more than one brand ("Aramark at
    General Mills 2024", "Broadcom Inc. - VMware Inc."). ``resolve`` orders
    the candidates by:

      1. earliest match start offset   — the leading name is the filing employer
      2. longest match length          — prefers the more specific name
      3. lowest tier                   — tier 1 beats tier 2/3
      4. canonical name, alphabetical  — final, total tie-break

    Rules 1-4 are a total order over a finite set, so two overlapping patterns
    always resolve the same way — no dependence on registry order, which is
    what made the old per-brand blocklists resolve one employer's own filings
    inconsistently.

CONTRACTOR VETO
    Rule 1 alone only demotes a "<contractor> at <Brand>" row to the
    contractor when the contractor is ITSELF registered — the minority case.
    Housing Works, CDL New York, Crestview Management, Evolution Hospitality,
    Packard Pacifica and Restec are not in this registry, so their rows
    resolved to the flag they operate under.

    So: when the winning match does not start the string (a leading "The " and
    a state agency's filing marks are allowed) and the text in front of it
    contains " at ", " @ ", "dba", "(", "providing services for", "on behalf
    of", or an operator token (Hospitality, Lodging, Staffing, Services
    Inc/LLC, Management LLC/Corp, Associates, Ventures, Holdings), the answer
    is None. It rejects 290 corpus strings / 33,487 employees, every one of
    them a contractor, franchisee or management-company row.

    The concrete failure it prevents: "Flagship Facility Services Inc. at Meta
    Platforms Inc." is the CONTRACTOR's layoff. Posting "Meta filed a WARN
    notice for N job cuts" off that record is the worst factual error this
    system can make. It also stops "Housing Works Inc. (at Holiday Inn, Howard
    Johnson, Fresh Meadows, La Quinta)" — an HIV/AIDS-services nonprofit —
    from being posted as Holiday Inn.

MULTI-BRAND VETO
    Two or more brands match and none of them starts the string: nobody is the
    filer, so None. Prevents "Gourmet Management Corp. (TGI Fridays, KFC, Taco
    Bell, Pizza Hut, Haagen Dazs, Tim Hortons …)" from posting as whichever
    tenant the tie-break happened to land on.

LODGING AMBIGUITY VETO
    Hotel and casino flags double as venue and address labels far more than
    other marks, so a lodging brand may only win when it is the ONLY operator
    named. Without this, "Hyatt Centric Waikiki Rack (Nordstrom)" posts the
    landlord instead of the Nordstrom Rack that closed, and "Marriott Hyatt
    House Belmont" — a Hyatt property — posts as Marriott, both purely because
    the venue name comes first. Offset rules cannot catch either: the wrong
    brand is at offset 0. Sister brands under one parent are not a second
    operator, so ``_FAMILY`` collapses them first — otherwise "The
    Ritz-Carlton, Los Angeles, JW Marriott L.A. LIVE" (1,009 employees) is
    thrown away as ambiguous.

WHY ``negative`` STILL EXISTS. The vetoes are structural; ``negative`` handles
the plain word collisions no structure can see. "Apple" must not claim "Apple
Valley Medical Center", "Target" must not claim "Target Logistics", "Compass
Group" must not claim "Compass Minerals", and "Sonic" (the drive-in) must not
claim "Sonic Automotive" — different employers that merely share a word. Every
pattern here was run against all 40,956 company strings and its matches read
by hand; the guards are the residue.

TECH RULE
    ``tech`` is True when the company's PRODUCT is software, an internet
    platform, semiconductors, or computing/communications hardware. A company
    that merely uses the internet to sell physical goods, houses, cars, rides,
    groceries, loans or insurance is not tech — that line puts eBay, Etsy,
    Zillow, Uber and DoorDash on one side and Chewy, Wayfair, Carvana, Redfin,
    Opendoor and WeWork on the other. It matters because ``warn_x_select``
    posts a tech brand at a 50-employee floor.

TIER RULE
    1  a general US news audience recognises the name unprompted
    2  widely known, but mainly to business readers, or strong only in one
       region or one industry's customer base
    3  niche: B2B, industrial, contract-services and specialist firms whose
       name means little outside their own industry, plus faded names
    Tier is only tie-break #3; it never decides whether to post.

Patterns that match nothing today are deliberate, not broken. Nissan, Kia,
Volkswagen and Mazda are anchored so they reject the 150+ franchise
dealership rows ("Bay Ridge Nissan", "Browning Mazda"); SoFi rejects Sofitel;
Stripe rejects "Sunoco LP - Stripes LLC"; Vanguard rejects "Vanguard
Marketing Services". Do not "fix" them by loosening the anchor.

Imports nothing but ``re``. Keep it that way — ``warn_names`` imports this,
not the reverse.
"""

import re

BRANDS = [
    {
        "canonical": "Amazon",
        "pattern": "^\\s*amazon\\b|\\bamazon\\.com\\b|\\bamazonfresh\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "retail+tech",
    },
    {
        "canonical": "Google",
        "pattern": "^\\s*google\\b|^\\s*alphabet\\s+inc\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Meta",
        "pattern": "^\\s*meta\\s+platforms\\b|^\\s*facebook[,.]?\\s*(inc\\b|$)",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Microsoft",
        "pattern": "^\\s*microsoft\\b",
        "negative": "microsoft\\s+theat",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Apple",
        "pattern": "^\\s*apple[,.]?\\s*(inc\\b|computer\\b|$)",
        "negative": "apple\\s*(valley|bee|gate|green|bagel)",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Intel",
        "pattern": "^\\s*intel\\b",
        "negative": "intelligen|intelligrat|intelli-",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Nvidia",
        "pattern": "^\\s*nvidia\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "AMD",
        "pattern": "\\badvanced\\s+micro\\s+devices\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Qualcomm",
        "pattern": "^\\s*qualcomm\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Micron",
        "pattern": "^\\s*micron\\s+technolog",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Broadcom",
        "pattern": "^\\s*broadcom\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Texas Instruments",
        "pattern": "^\\s*texas\\s+instruments\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Applied Materials",
        "pattern": "^\\s*applied\\s+materials\\b",
        "negative": "ad+ecco|addeco",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Lam Research",
        "pattern": "^\\s*lam\\s+research\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Marvell",
        "pattern": "^\\s*marvell\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Western Digital",
        "pattern": "^\\s*western\\s+digital\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Seagate",
        "pattern": "^\\s*seagate\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Dell",
        "pattern": "^\\s*dell\\s+(technologies|computer|products|"
                   "financial|emc|marketing|usa|inc\\b)|^\\s*dell[,.]?\\s*inc\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "HP Inc.",
        "pattern": "^\\s*hp[,.]?\\s+inc\\b|^\\s*hewlett[-\\s]?packard\\b",
        "negative": "hewlett[-\\s]?packard\\s+enterprise",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Hewlett Packard Enterprise",
        "pattern": "\\bhewlett[-\\s]?packard\\s+enterprise\\b|"
                   "^\\s*hpe[,.]?\\s+(inc|co)",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "IBM",
        "pattern": "^\\s*ibm\\b|^\\s*international\\s+business\\s+machines\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Cisco",
        "pattern": "^\\s*cisco\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Oracle",
        "pattern": "^\\s*oracle\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Salesforce",
        "pattern": "^\\s*salesforce",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Adobe",
        "pattern": "^\\s*adobe\\b",
        "negative": "adobes?\\s+(center|village|inn|creek)",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "ServiceNow",
        "pattern": "^\\s*servicenow\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Workday",
        "pattern": "^\\s*workday\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "VMware",
        "pattern": "\\bvmware\\b|\\bvm\\s?ware\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Atlassian",
        "pattern": "^\\s*atlassian\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Dropbox",
        "pattern": "^\\s*dropbox\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Okta",
        "pattern": "^\\s*okta[,.]?\\s*(inc\\b|$)",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Splunk",
        "pattern": "^\\s*splunk\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Palantir",
        "pattern": "^\\s*palantir\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Snowflake",
        "pattern": "^\\s*snowflake\\s+(inc|computing)\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Databricks",
        "pattern": "^\\s*databricks\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Twilio",
        "pattern": "^\\s*twilio\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Zoom",
        "pattern": "\\bzoom\\s+video\\s+communications\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Slack",
        "pattern": "\\bslack\\s+technologies\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Intuit",
        "pattern": "^\\s*intuit\\b|\\bturbotax\\b",
        "negative": "intuitive",
        "tech": True,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "Autodesk",
        "pattern": "^\\s*autodesk\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Block",
        "pattern": "^\\s*block,?\\s+inc\\b|\\bof\\s+block,?\\s*inc\\b|"
                   "\\bcash\\s+app\\b",
        "negative": "h\\s?&\\s?r\\s+block|block\\s+by\\s+block|union\\s+square",
        "tech": False,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "PayPal",
        "pattern": "^\\s*paypal\\b|\\bbraintree\\s+payment",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "eBay",
        "pattern": "^\\s*ebay\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Coinbase",
        "pattern": "^\\s*coinbase\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "Robinhood",
        "pattern": "^\\s*robinhood\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "Stripe",
        "pattern": "^\\s*stripe[,.]?\\s*(inc\\b|$)",
        "negative": "stripes|stripeside",
        "tech": False,
        "tier": 1,
        "sector": "finance+tech",
    },
    {
        "canonical": "Shopify",
        "pattern": "^\\s*shopify\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Etsy",
        "pattern": "^\\s*etsy[,.]?\\s*(inc\\b|$)",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Chewy",
        "pattern": "^\\s*chewy[,.]?\\s*(inc\\b|$)",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Wayfair",
        "pattern": "^\\s*wayfair\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Netflix",
        "pattern": "^\\s*netflix",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media+tech",
    },
    {
        "canonical": "Spotify",
        "pattern": "^\\s*spotify\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media+tech",
    },
    {
        "canonical": "Uber",
        "pattern": "\\buber\\s+technologies\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media+tech",
    },
    {
        "canonical": "Lyft",
        "pattern": "^\\s*lyft\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media+tech",
    },
    {
        "canonical": "Airbnb",
        "pattern": "^\\s*airbnb\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "DoorDash",
        "pattern": "^\\s*doordash\\b|^\\s*door\\s?dash\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Instacart",
        "pattern": "\\binstacart\\b|^\\s*maplebear\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Grubhub",
        "pattern": "^\\s*grubhub\\b|^\\s*grub\\s?hub\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Snap",
        "pattern": "^\\s*snap[,.]?\\s+inc\\b",
        "negative": "snap[-\\s]?on",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Pinterest",
        "pattern": "^\\s*pinterest\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "LinkedIn",
        "pattern": "^\\s*linkedin\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "X",
        "pattern": "^\\s*twitter\\b|^\\s*x\\s+corp\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "TikTok",
        "pattern": "^\\s*tiktok\\b|^\\s*bytedance\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Reddit",
        "pattern": "^\\s*reddit[,.]?\\s*(inc\\b|$)",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Discord",
        "pattern": "^\\s*discord\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Yelp",
        "pattern": "^\\s*yelp\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Groupon",
        "pattern": "^\\s*groupon\\b|\\bsubsidiary\\s+of\\s+groupon\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Electronic Arts",
        "pattern": "\\belectronic\\s+arts\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Activision Blizzard",
        "pattern": "^\\s*activision\\b|^\\s*blizzard\\s+entertainment\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Riot Games",
        "pattern": "^\\s*riot\\s+games\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Epic Games",
        "pattern": "^\\s*epic\\s+games\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Unity",
        "pattern": "^\\s*unity\\s+technologies\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Roblox",
        "pattern": "^\\s*roblox\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Zynga",
        "pattern": "^\\s*zynga\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Twitch",
        "pattern": "^\\s*twitch\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Zillow",
        "pattern": "^\\s*zillow\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Redfin",
        "pattern": "^\\s*redfin\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Opendoor",
        "pattern": "^\\s*opendoor\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Carvana",
        "pattern": "^\\s*carvana\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Peloton",
        "pattern": "^\\s*peloton\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "WeWork",
        "pattern": "^\\s*wework\\b|\\bsubsidiary\\s+of\\s+wework\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "real_estate",
    },
    {
        "canonical": "Tesla",
        "pattern": "\\bTesla\\b",
        "negative": "Tesla (Ave|Blvd|St|Street|Road|Rd|Dr|Drive|Ct)\\b",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Rivian",
        "pattern": "\\bRivian\\b",
        "negative": "Maclellan|Aerotek|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Lucid Motors",
        "pattern": "^\\s*lucid\\s+(usa|group|motors)\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Waymo",
        "pattern": "^\\s*waymo\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "Cruise",
        "pattern": "^\\s*cruise[,.]?\\s+llc\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "SpaceX",
        "pattern": "^\\s*spacex\\b|^\\s*space\\s+exploration\\s+technologies\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Nikola",
        "pattern": "^\\s*nikola\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "Illumina",
        "pattern": "^\\s*illumina\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "23andMe",
        "pattern": "^\\s*23andme\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "tech",
    },
    {
        "canonical": "JPMorgan Chase",
        "pattern": "\\b(?:j\\.?\\s?p\\.?\\s?morgan|jpmorgan)\\b|"
                   "\\bchase\\s+(?:bank|card\\s+services|home\\s+lending|"
                   "manhattan|mortgage)\\b",
        "negative": "aramark|restaurant\\s+associates|chartwells|"
                    "compass\\s+group|restec|chevy\\s+chase|"
                    "chase\\s+center|kimble",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Bank of America",
        "pattern": "bank\\s*of\\s*america|\\bbofa\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Merrill Lynch",
        "pattern": "merrill\\s+lynch",
        "negative": "howard\\s+johnson",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Wells Fargo",
        "pattern": "wells\\s+fargo",
        "negative": "wells\\s+fargo\\s+center",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Citigroup",
        "pattern": "\\bciti(?:bank|group|corp|mortgage|financial|"
                   "share)\\b|^\\s*citi\\b(?=\\s*(financial|"
                   "mortgage|,|\\.|-|$))",
        "negative": "citi\\s*trends|citi\\s*field|citi\\s*west|aramark",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Goldman Sachs",
        "pattern": "goldman,?\\s+sachs",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Morgan Stanley",
        "pattern": "morgan\\s+stanley|smith\\s+barney",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "U.S. Bank",
        "pattern": "\\bu\\.?\\s?s\\.?\\s*bancorp\\b|\\bu\\.?\\s?s\\.?\\s+bank\\b|"
                   "\\bus\\s+bank\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "PNC Bank",
        "pattern": "\\bpnc\\b",
        "negative": "neighborhood\\s+healthcare",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Truist",
        "pattern": "\\btruist\\b|\\bsuntrust\\b|\\bbb\\s?&\\s?t\\b|"
                   "branch\\s+banking\\s+and\\s+trust",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Fifth Third Bank",
        "pattern": "fifth\\s+third",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "KeyBank",
        "pattern": "\\bkeycorp\\b|\\bkeybank\\b|\\bkey\\s+bank\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Regions Bank",
        "pattern": "\\bregions\\s+(?:bank|financial|mortgage)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "M&T Bank",
        "pattern": "\\bm\\s?&\\s?t\\s+(?:bank|mortgage|financial)\\b|"
                   "manufacturers\\s+(?:and|&)\\s+tra",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Huntington Bank",
        "pattern": "huntington\\s+ban",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Ally Financial",
        "pattern": "\\bally\\s+(?:bank|financial|invest)\\b|\\bgmac\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Capital One",
        "pattern": "capital\\s+one",
        "negative": "aramark|parkhurst|compass\\s+group",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Discover",
        "pattern": "discover\\s+(?:financial|bank|card|products|"
                   "services|home\\s+loans)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "American Express",
        "pattern": "american\\s+express",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Charles Schwab",
        "pattern": "charles\\s+schwab",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Fidelity Investments",
        "pattern": "fidelity\\s+investments|\\bfmr\\s+llc\\b|fidelity\\s+brokerage",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Vanguard",
        "pattern": "\\bvanguard\\s+group\\b|the\\s+vanguard\\s+group",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "BlackRock",
        "pattern": "\\bblackrock\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "State Street",
        "pattern": "state\\s+street\\s+(?:bank|corp|global)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Synchrony",
        "pattern": "\\bsynchrony\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Santander",
        "pattern": "\\bsantander\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "BMO",
        "pattern": "\\bbmo\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "TD Bank",
        "pattern": "\\btd\\s+bank\\b|toronto[\\s\\-]dominion",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "HSBC",
        "pattern": "\\bhsbc\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Barclays",
        "pattern": "\\bbarclays\\b",
        "negative": "restaurant\\s+associates|aramark|compass\\s+group|culinart",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Deutsche Bank",
        "pattern": "deutsche\\s+bank\\b|\\bdb\\s+usa\\b",
        "negative": "restaurant\\s+associates|aramark|compass\\s+group|culinart",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "UBS",
        "pattern": "\\bubs\\b",
        "negative": "restaurant\\s+associates|aramark|compass\\s+group",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Credit Suisse",
        "pattern": "credit\\s+suisse",
        "negative": "restaurant\\s+associates|aramark|compass\\s+group|culinart",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "First Republic Bank",
        "pattern": "first\\s+republic\\s+bank",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Silicon Valley Bank",
        "pattern": "silicon\\s+valley\\s+bank|\\bsvb\\s+financial\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Signature Bank",
        "pattern": "signature\\s+bank",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "BNY Mellon",
        "pattern": "bank\\s+of\\s+new\\s+york\\s+mellon|\\bbny\\b",
        "negative": "restaurant\\s+associates|aramark|compass\\s+group|"
                    "federal\\s+reserve",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Washington Mutual",
        "pattern": "washington\\s+mutual|\\bwamu\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Fannie Mae",
        "pattern": "fannie\\s+mae|federal\\s+national\\s+mortgage\\s+association",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Freddie Mac",
        "pattern": "freddie\\s+mac|federal\\s+home\\s+loan\\s+mortgage\\s+corp",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Sallie Mae",
        "pattern": "sallie\\s+mae|\\bslm\\s+corp",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Navient",
        "pattern": "\\bnavient\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Rocket Mortgage",
        "pattern": "rocket\\s+mortgage|quicken\\s+loans|rocket\\s+companies",
        "negative": "aramark|fieldhouse|arena|compass\\s+group",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Better.com",
        "pattern": "better\\.com|better\\s+mortgage\\b|better\\s+holdco",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Mr. Cooper",
        "pattern": "\\bmr\\.?\\s+cooper\\b|\\bnationstar\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Cigna",
        "pattern": "\\bcigna\\b|\\bevernorth\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Express Scripts",
        "pattern": "express\\s+scripts",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Humana",
        "pattern": "\\bhumana\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Anthem",
        "pattern": "\\belevance\\b|\\banthem\\b\\s*(health|"
                   "blue|inc|,|insurance\\s+company|blue\\s+cross)|"
                   "^\\s*anthem\\s*$|\\bwellpoint\\b|\\bwell\\s+point\\b",
        "negative": "anthem\\s+(institute|education|college|"
                    "casualty)|\\bctg,?\\s+inc",
        "tech": False,
        "tier": 1,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Aetna",
        "pattern": "^\\s*aetna\\b|\\baetna\\s+(life|insurance|"
                   "health|us\\s+healthcare|better\\s+health)\\b",
        "negative": "insulated wire|aetna wire|aetna building|aetna bridge|plywood",
        "tech": False,
        "tier": 1,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Progressive Insurance",
        "pattern": "\\bprogressive\\s+(?:casualty|insurance)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "GEICO",
        "pattern": "\\bgeico\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Allstate",
        "pattern": "\\ballstate\\b",
        "negative": "aramark|parkhurst|allstate\\s+arena|compass\\s+group",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "State Farm",
        "pattern": "state\\s+farm",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Liberty Mutual",
        "pattern": "liberty\\s+mutual",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Travelers",
        "pattern": "\\btravelers\\s+(?:insurance|indemnity|"
                   "property|companies|group|one)\\b|^the\\s+travelers\\b|"
                   "^travelers$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "AIG",
        "pattern": "\\baig\\b|american\\s+international\\s+group",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "MetLife",
        "pattern": "\\bmetlife\\b|\\bmet\\s+life\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Prudential",
        "pattern": "\\bprudential\\b",
        "negative": "prudential\\s+(overall|cleanroom|uniform)",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Nationwide",
        "pattern": "\\bnationwide\\s+(?:mutual|insurance|financial|"
                   "sales\\s+solutions)\\b|^nationwide$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Farmers Insurance",
        "pattern": "farmers\\s+insurance|farmers\\s+group,?\\s+inc",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "USAA",
        "pattern": "\\busaa\\b|united\\s+services\\s+automobile\\s+association",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "The Hartford",
        "pattern": "\\bthe\\s+hartford\\b|hartford\\s+(?:financial|"
                   "insurance|life|fire\\s+insurance)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Chubb",
        "pattern": "^\\s*chubb\\b|\\bchubb\\s*(&|and)\\s*son\\b|"
                   "\\bchubb\\s+(group|insurance|corp|ltd|"
                   "limited|national)\\b",
        "negative": "institute|fire\\s*(&|and)?\\s*security|chubb\\s+edwards",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "New York Life",
        "pattern": "new\\s+york\\s+life",
        "negative": "canon\\s+solutions|providing\\s+services",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Transamerica",
        "pattern": "\\btransamerica\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Principal Financial Group",
        "pattern": "principal\\s+(?:financial|life\\s+insurance)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Aon",
        "pattern": "\\baon\\s+(?:corp|hewitt|service|plc|risk|group)|^aon$",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Experian",
        "pattern": "\\bexperian\\b",
        "negative": "%\\s*experian|employer\\s+services",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Equifax",
        "pattern": "\\bequifax\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "TransUnion",
        "pattern": "\\btrans\\s?union\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Visa",
        "pattern": "^visa(?:,?\\s+inc\\.?)?$|\\bvisa\\s+(?:inc\\b|"
                   "u\\.?s\\.?a\\.?\\b|international\\b)",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Mastercard",
        "pattern": "\\bmastercard\\b|\\bmaster\\s?card\\s+(?:inc|"
                   "international|worldwide)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Fiserv",
        "pattern": "\\bfiserv\\b|\\bfirst\\s+data\\b",
        "negative": "levy|forum|aramark|compass\\s+group",
        "tech": False,
        "tier": 3,
        "sector": "finance",
    },
    {
        "canonical": "Western Union",
        "pattern": "western\\s+union",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Affirm",
        "pattern": "\\baffirm,?\\s+(?:inc|holdings)\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Klarna",
        "pattern": "\\bklarna\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Chime",
        "pattern": "\\bchime\\s+financial\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "SoFi",
        "pattern": "\\bsofi\\b|social\\s+finance,?\\s+inc",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "LendingClub",
        "pattern": "\\blending\\s?club\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "ADP",
        "pattern": "\\bADP,?\\s+(?:LLC|Inc|Benefits)|automatic\\s+data\\s+processin"
                   "g",
        "negative": "standex",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "H&R Block",
        "pattern": "\\bh\\s?&\\s?r\\s+block\\b|\\bh\\.?\\s?r\\.?\\s+block\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "finance",
    },
    {
        "canonical": "Walmart",
        "pattern": "\\bwal[\\s-]?mart\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Sam's Club",
        "pattern": "\\bsam[’']?s?\\s*club\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Target",
        "pattern": "^\\s*target\\b(?=$|[,.]|\\s+(corp|corporation|"
                   "corporate|stores?|sourcing|distribution|"
                   "dc\\b|-|–))",
        "negative": "target\\s*=\\s*\\\"",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Costco",
        "pattern": "\\bcostco\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Kroger",
        "pattern": "\\bkroger\\b",
        "negative": "southstar",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Albertsons",
        "pattern": "\\balbertson[’']?s?\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Safeway",
        "pattern": "\\bsafeway\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Publix",
        "pattern": "\\bpublix\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Whole Foods Market",
        "pattern": "\\bwhole\\s+foods\\b",
        "negative": "instacart|^summit\\s+hill",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Trader Joe's",
        "pattern": "\\btrader\\s+joe[’']?s?\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Aldi",
        "pattern": "\\baldi\\b",
        "negative": "penske",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Dollar General",
        "pattern": "\\bdollar\\s+general\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Dollar Tree",
        "pattern": "\\bdollar\\s+tree\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Family Dollar",
        "pattern": "\\bfamily\\s+dollar\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Big Lots",
        "pattern": "\\bbig\\s+lots\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Best Buy",
        "pattern": "\\bbest\\s+buy\\b",
        "negative": "aeg\\s+presents|theat",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "The Home Depot",
        "pattern": "\\bhome\\s+depot\\b",
        "negative": "^exel\\b",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Lowe's",
        "pattern": "^lowe[’']?s\\b",
        "negative": "^lowe[’']?s\\s+food",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Staples",
        "pattern": "\\bstaples\\b",
        "negative": "staples\\s+center|arena\\s+company",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Office Depot",
        "pattern": "\\boffice\\s?depot\\b|\\boffice\\s?max\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "GameStop",
        "pattern": "\\bgame\\s?stop\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Petco",
        "pattern": "\\bpetco\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "PetSmart",
        "pattern": "\\bpetsmart\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Barnes & Noble",
        "pattern": "\\bbarnes\\s*(&|and)\\s*noble\\b",
        "negative": "college\\s+booksellers|barnes\\s*(&|and)\\s*noble\\s+education",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Barnes & Noble Education",
        "pattern": "\\bbarnes\\s*(&|and)\\s*noble\\s+(college\\s+booksellers|"
                   "education)",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "retail",
    },
    {
        "canonical": "Michaels",
        "pattern": "\\bmichaels\\s+stores\\b|\\bthe\\s+michaels\\s+comp",
        "negative": "lamrite",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Joann",
        "pattern": "\\bjo-?\\s?ann[’']?s?\\s+(stores?|fabric|"
                   "distribution|omni|craft|support)|^jo-?\\s?ann[’']?s?[,.]?\\s*(i"
                   "nc|llc)?\\.?\\s*$",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Party City",
        "pattern": "\\bparty\\s+city\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Bed Bath & Beyond",
        "pattern": "\\bbed\\s*,?\\s*bath\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Dick's Sporting Goods",
        "pattern": "\\bdick[’']?s\\s+sporting\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "REI",
        "pattern": "\\bdba\\s+REI\\b|\\bREI\\s+Co-?op\\b|\\brecreational\\s+equipme"
                   "nt,?\\s+inc\\b|^REI[,.]?\\s*(inc\\.?|llc)?\\s*$",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Walgreens",
        "pattern": "\\bwalgreens?\\b|\\bwalgreen\\s+co\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media+retail",
    },
    {
        "canonical": "Rite Aid",
        "pattern": "rite aid",
        "negative": "eclipse advantage|sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media+retail",
    },
    {
        "canonical": "Macy's",
        "pattern": "\\bmacy[’']?s\\b",
        "negative": "finish\\s+line",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Nordstrom",
        "pattern": "\\bnordstrom\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Kohl's",
        "pattern": "\\bkohl[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "JCPenney",
        "pattern": "\\bjcpenney\\b|\\bj\\.?\\s?c\\.?\\s+penney\\b|"
                   "\\bpenney\\s+opco\\b|\\bjc\\s?penney\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Dillard's",
        "pattern": "\\bdillard[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Sears",
        "pattern": "\\bsears\\b",
        "negative": "flooring|plumbing|roofing|realty|insurance|"
                    "landscap|masonry|excavat|termite|pest|"
                    "sears\\s+manufacturing|trostel|sears\\s+lumber|"
                    "sears\\s+oil|sears\\s+seating",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Kmart",
        "pattern": "\\bk-?\\s?mart\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Neiman Marcus",
        "pattern": "\\bneiman[\\s-]+marcus\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Saks Fifth Avenue",
        "pattern": "\\bsaks\\b",
        "negative": "centerplate",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Bloomingdale's",
        "pattern": "\\bbloomingdale[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Gap",
        "pattern": "^gap[,.]?\\s*(inc\\b|$)|\\bgap\\s+inc\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Old Navy",
        "pattern": "\\bold\\s+navy\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Nike",
        "pattern": "\\bnike\\b|\\bniketown\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Adidas",
        "pattern": "\\badidas\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Under Armour",
        "pattern": "\\bunder\\s+armou?r\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Lululemon",
        "pattern": "\\blululemon\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Foot Locker",
        "pattern": "\\bfoot\\s?locker\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Bath & Body Works",
        "pattern": "\\bbath\\s*(&|and)\\s*body\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Victoria's Secret",
        "pattern": "\\bvictoria[’']?s\\s+secret\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Ulta Beauty",
        "pattern": "\\bulta\\s+(salon|beauty|cosmetics|stores?)\\b|"
                   "\\bdba\\s+ulta\\b|^ulta[,.]?\\s*(inc|llc)?\\.?\\s*$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Sephora",
        "pattern": "\\bsephora\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Express",
        "pattern": "^express(\\s*[,.]?\\s*(llc|inc)\\b|\\s+fashion\\b|"
                   "\\s*$|\\s+-\\s)",
        "negative": "scripts|truck|freight|logistic|transport|"
                    "courier|parcel|delivery|grain|\\bmart\\b|"
                    "manufactur",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Forever 21",
        "pattern": "\\bforever\\s*21\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "H&M",
        "pattern": "\\bh\\s*&\\s*m\\b",
        "negative": "international\\s+transportation|^h\\s*&\\s*m\\s*$",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Zara",
        "pattern": "\\bzara\\s+usa\\b|^\\s*zara[,.]?\\s*(inc|llc|usa)?\\.?\\s*$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Burlington",
        "pattern": "\\bburlington\\s+coat\\b|\\bdba\\s+burlington\\b|"
                   "^burlington\\s+stores\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Ross Stores",
        "pattern": "\\bross\\s+stores\\b|\\bross\\s+dress\\s+for\\s+less\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "TJ Maxx",
        "pattern": "\\bt\\.?\\s?j\\.?\\s*maxx\\b|\\bmarmaxx\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "TJX Companies",
        "pattern": "\\btjx\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Marshalls",
        "pattern": "\\bmarshalls\\b",
        "negative": "marshalls?\\s+(creek|island)",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Starbucks",
        "pattern": "\\bstarbucks\\b",
        "negative": "hmshost",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "McDonald's",
        "pattern": "\\bmcdonald[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Chipotle",
        "pattern": "\\bchipotle\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Subway",
        "pattern": "\\bsubway\\s+(restaurants?|sandwich|franchise)\\b|"
                   "\\bdba\\s+subway\\b|^subway[,.]?\\s*(inc|"
                   "llc)?\\.?\\s*$",
        "negative": "transit|railway|railroad|authority|surface|supervisor",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Burger King",
        "pattern": "\\bburger\\s+king\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Wendy's",
        "pattern": "\\bwendy[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Taco Bell",
        "pattern": "\\btaco\\s+bell\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "KFC",
        "pattern": "\\bkfc\\b|\\bkentucky\\s+fried\\s+chicken\\b",
        "negative": "kentucky\\s+fuel",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Pizza Hut",
        "pattern": "\\bpizza\\s+hut\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Domino's Pizza",
        "pattern": "\\bdomino[’']?s\\s+pizza\\b|^domino[’']s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Papa John's",
        "pattern": "\\bpapa\\s+john[’']?s?\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Dunkin'",
        "pattern": "\\bdunkin[’']?\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Panera Bread",
        "pattern": "\\bpanera\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Applebee's",
        "pattern": "\\bapplebee[’']?s?\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Chili's",
        "pattern": "\\bchili[’']?s\\s+(grill|bar|restaurant)\\b|"
                   "^chili[’']s\\s*$|\\bdba\\s+chili[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Olive Garden",
        "pattern": "\\bolive\\s+garden\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Red Lobster",
        "pattern": "\\bred\\s+lobster\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "TGI Fridays",
        "pattern": "\\bt\\.?\\s?g\\.?\\s?i\\.?\\s*(fridays?|"
                   "friday[’']s|firday)|\\btgif\\s+restaurant\\b",
        "negative": "office\\s+automation",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Denny's",
        "pattern": "\\bdenny[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "IHOP",
        "pattern": "\\bihop\\b",
        "negative": "ihope|academy\\s+of\\s+hope",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Cracker Barrel",
        "pattern": "\\bcracker\\s+barrel\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Outback Steakhouse",
        "pattern": "\\boutback\\s+steakhouse\\b|\\boutback\\s+\\d{3,4}\\b",
        "negative": "\\bpower\\b|\\bsolar\\b|\\bmine\\b",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "The Cheesecake Factory",
        "pattern": "\\bcheesecake\\s+factory\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Shake Shack",
        "pattern": "\\bshake\\s+shack\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Five Guys",
        "pattern": "\\bfive\\s+guys\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Panda Express",
        "pattern": "\\bpanda\\s+(express|restaurant\\s+group)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Jack in the Box",
        "pattern": "\\bjack\\s+in\\s+the\\s+box\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Arby's",
        "pattern": "\\barby[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Popeyes",
        "pattern": "\\bpopeye[’']?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Chick-fil-A",
        "pattern": "\\bchick[\\s-]?fil[\\s-]?a\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Hooters",
        "pattern": "\\bhooters\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Bloomin' Brands",
        "pattern": "\\bbloomin[’']?s?\\s+brands\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Darden Restaurants",
        "pattern": "\\bdarden\\s+restaurant",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Ford",
        "pattern": "\\bFord Motor\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "General Motors",
        "pattern": "\\bGeneral Motors\\b|^\\s*GM\\b|\\bGM (Powertrain|"
                   "Components|Global|Financial|Defense)\\b|"
                   "\\bUltium Cells\\b",
        "negative": "Robinson Solutions|Staffing|Aerotek|GM[-\\s]?UAW",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Stellantis",
        "pattern": "\\bStellantis\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Chrysler",
        "pattern": "\\bFCA US\\b|\\bFiat Chrysler Automobiles\\b|"
                   "\\bChrysler (Group|Corporation|Corp\\b|"
                   "Motors|Financial)\\b|\\bDaimler ?Chrysler (Corp|"
                   "Motors|Financial)\\b",
        "negative": "\\bdba\\b|\\bd/b/a\\b|Jeep|Dodge",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Toyota",
        "pattern": "\\bToyota (Motor|Financial Services|Industries|"
                   "Material Handling|Racing Development|Research)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Honda",
        "pattern": "\\bAmerican Honda Motor\\b|\\bHonda (of America )?(Manufacturin"
                   "g|Motor Co|Performance Development|Aircraft|"
                   "Development|R ?& ?D|Aero|Precision|Transmission|"
                   "Power Equipment|North America|Financial|"
                   "Logistics)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Nissan",
        "pattern": "\\bNissan (North America|Motor|Technical|"
                   "Design|Financial|Trading|Extended)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Hyundai",
        "pattern": "\\bHyundai (Motor|Capital America|America Shipping|"
                   "Mobis|Glovis|Transys|Translead|Rotem)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Kia",
        "pattern": "\\bKia (Motors|America|Georgia|Corporation)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "BMW",
        "pattern": "\\bBMW (of North America|North America|"
                   "Manufacturing|Financial|Group|US Capital)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mercedes-Benz",
        "pattern": "\\bMercedes[- ]?Benz (USA|U\\.S\\.|Financial|"
                   "Research|R ?& ?D|Vans|Group|Manufacturing|"
                   "High Performance)\\b|^Mercedes[- ]?Benz$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Volkswagen",
        "pattern": "\\bVolkswagen (Group|of America|AG|Chattanooga|Credit)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Subaru",
        "pattern": "\\bSubaru of (America|Indiana)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mazda",
        "pattern": "\\bMazda (North American|Motor|of America|"
                   "Toyota Manufacturing)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Boeing",
        "pattern": "\\bBoeing\\b",
        "negative": "Aramark|Sodexo|Aerotek|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Airbus",
        "pattern": "\\bAirbus\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Lockheed Martin",
        "pattern": "\\bLockheed\\b",
        "negative": "Lockheed\\s+(Dr|Drive|Blvd|Boulevard|Way|"
                    "Ave|Avenue|Rd|Road|St|Street|Ct|Cir)\\b",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Northrop Grumman",
        "pattern": "\\bNorthr[ou]p\\b",
        "negative": "Northr[ou]p\\s+(Dr|Drive|Blvd|Way|Ave|Rd|Road)\\b",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "RTX",
        "pattern": "\\bRaytheon\\b|\\bRTX\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "General Dynamics",
        "pattern": "\\bGeneral Dynamics\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "L3Harris",
        "pattern": "\\bL3\\s?Harris\\b|\\bL-?3 Communications\\b|"
                   "\\bL3 Technologies\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Textron",
        "pattern": "\\bTextron\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Spirit AeroSystems",
        "pattern": "\\bSpirit Aero",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Pratt & Whitney",
        "pattern": "\\bPratt ?& ?Whitney\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Collins Aerospace",
        "pattern": "\\bCollins Aerospace\\b|\\bRockwell Collins\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Rolls-Royce",
        "pattern": "\\bRolls[- ]?Royce\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "BAE Systems",
        "pattern": "\\bBAE Systems\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Huntington Ingalls",
        "pattern": "\\bHuntington Ingalls\\b|\\bNewport News Shipbuilding\\b|"
                   "\\bIngalls Shipbuilding\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Caterpillar",
        "pattern": "\\bCaterpillar\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "John Deere",
        "pattern": "\\bJohn Deere\\b|\\bDeere ?& ?Co\\b|\\bDeere and Co\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg+manufacturing",
    },
    {
        "canonical": "3M",
        "pattern": "\\b3M\\b",
        "negative": "Aramark|Irwin Industries|Sodexo|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "DuPont",
        "pattern": "\\bDu ?Pont\\b",
        "negative": "Dupont Circle|Dupont Plaza|BE ?& ?K|Fort Dupont|"
                    "Irwin Industries",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Dow",
        "pattern": "\\bDow Chemical\\b|\\bDow Inc\\b|\\bDow Corning\\b|"
                   "\\bDowDuPont\\b|\\bDow Silicones\\b|\\bDow AgroSciences\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Goodyear",
        "pattern": "\\bGoodyear\\b",
        "negative": "Goodyear (Fitness|Municipal|Ballpark|Community|"
                    "Medical|Health|Elementary)|City of Goodyear",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Whirlpool",
        "pattern": "\\bWhirlpool\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Emerson",
        "pattern": "\\bEmerson (Electric|Process|Network Power|"
                   "Climate|Automation|Industrial)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Eaton",
        "pattern": "\\bEaton (Corp|Corporation|Aerospace|Electrical|"
                   "Industries|Hydraulics)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Honeywell",
        "pattern": "\\bHoneywell\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "General Electric",
        "pattern": "\\bGeneral Electric\\b|^\\s*GE\\b",
        "negative": "Lufkin|Granite Services|Marriott|Baker Hughes|"
                    "Roper|Savant|Current Powered|Portland General",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Siemens",
        "pattern": "\\bSiemens\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Cummins",
        "pattern": "\\bcummins[,.]?\\s+(inc|filtration|engines?|"
                   "military|metropower|meritor|alabama|power|"
                   "atlantic|northwest|southern|crosspoint|"
                   "sales|distribution|emission)\\b|^\\s*cummins[,.]?\\s*(inc\\.?)?"
                   "\\s*$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Bosch",
        "pattern": "\\bRobert Bosch\\b|\\bBosch (Automotive|"
                   "Rexroth|Security|Thermotechnology|Packaging|"
                   "Healthcare|Emissions|Power Tools|Solar)\\b|"
                   "^BOSCH$",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Johnson Controls",
        "pattern": "\\bJohnson Controls\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Harley-Davidson",
        "pattern": "\\bHarley[- ]Davidson\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Stanley Black & Decker",
        "pattern": "\\bStanley Black ?& ?Decker\\b|\\bBlack ?& ?Decker\\b|"
                   "\\bDeWalt\\b|\\bStanley Works\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Alcoa",
        "pattern": "\\bAlcoa\\b",
        "negative": "Engineered Plastic|Aramark|Staffing",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "U.S. Steel",
        "pattern": "\\bU\\.? ?S\\.? Steel\\b|\\bUnited States Steel\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Nucor",
        "pattern": "\\bNucor\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Cleveland-Cliffs",
        "pattern": "\\bCleveland[- ]Cliffs\\b|\\bAK Steel\\b",
        "negative": "Johnson Controls|Aramark|Staffing",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "ArcelorMittal",
        "pattern": "\\bArcelor ?Mittal\\b|\\bMittal Steel\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "ExxonMobil",
        "pattern": "\\bExxon\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Chevron",
        "pattern": "\\bChevron\\b",
        "negative": "Chevron ?Texaco \\d",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Shell",
        "pattern": "\\bShell (Oil|Chemical|Energy|Exploration|"
                   "Trading|Deer Park|Pipeline|Offshore|Global)\\b|"
                   "\\bRoyal Dutch Shell\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "BP",
        "pattern": "\\bBP (America|Amoco|Exploration|Products|"
                   "Corporation|Energy|p\\.l\\.c|Plc|Pipelines|"
                   "Solar|West Coast)\\b|\\bBritish Petroleum\\b|"
                   "\\bAmoco\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "ConocoPhillips",
        "pattern": "\\bConoco\\b|\\bPhillips Petroleum\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Marathon",
        "pattern": "\\bMarathon (Oil|Petroleum|Pipe|Ashland|Refin)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Valero",
        "pattern": "\\bValero\\b",
        "negative": "Irwin Industries|Zachry|Turner Industries|Brock Services",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Phillips 66",
        "pattern": "\\bPhillips ?66\\b",
        "negative": "Irwin Industries|Zachry|Turner Industries|Brock Services",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Halliburton",
        "pattern": "\\bHalliburton\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "SLB",
        "pattern": "\\bSchlumberger\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Baker Hughes",
        "pattern": "\\bBaker Hughes\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Duke Energy",
        "pattern": "\\bDuke Energy\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "NextEra Energy",
        "pattern": "\\bNextEra\\b|\\bFlorida Power ?& ?Light\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Exelon",
        "pattern": "\\bExelon\\b|\\bComEd\\b|\\bCommonwealth Edison\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "PG&E",
        "pattern": "\\bPG ?& ?E\\b|\\bPacific Gas ?(and|&) ?Electric\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Southern California Edison",
        "pattern": "\\bSouthern California Edison\\b|\\bEdison International\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Tennessee Valley Authority",
        "pattern": "\\bTennessee Valley Authority\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "BASF",
        "pattern": "\\bBASF\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "PPG",
        "pattern": "\\bPPG\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Sherwin-Williams",
        "pattern": "\\bSherwin[- ]?Williams\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Bayer",
        "pattern": "\\bbayer\\s+(corporation|corp\\b|cropscience|"
                   "crop\\s?science|healthcare|health\\s?care|"
                   "pharmaceutic|inc\\b|materialscience|material\\s?science|"
                   "u\\.s|us\\b|ag\\b|research|animal|consumer)\\b|"
                   "^\\s*bayer\\s*$",
        "negative": "crop\\s?science|sodexo|aramark|compass\\s+group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media+manufacturing",
    },
    {
        "canonical": "Monsanto",
        "pattern": "\\bMonsanto\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg+manufacturing",
    },
    {
        "canonical": "Kimberly-Clark",
        "pattern": "\\bKimberly[- ]?Clark\\b",
        "negative": "Syzygy|Aramark|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "cpg+manufacturing",
    },
    {
        "canonical": "International Paper",
        "pattern": "\\bInternational Paper\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Georgia-Pacific",
        "pattern": "\\bGeorgia[- ]Pacific\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Bridgestone/Firestone",
        "pattern": "\\bBridgestone\\b|\\bFirestone (Tire|Complete|"
                   "Building|Industrial|Polymers|Retail|Fibers|"
                   "Synthetic|Ag)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Michelin",
        "pattern": "\\bMichelin\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Continental",
        "pattern": "\\bContinental (Automotive|Tire|AG|General Tire|"
                   "Teves|ContiTech)\\b",
        "negative": "structural plastics",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Panasonic",
        "pattern": "\\bPanasonic\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "LG Electronics",
        "pattern": "\\bLG (Electronics|Chem|Display|Energy Solution)\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Samsung",
        "pattern": "\\bSamsung\\b",
        "negative": "Mosaic Sales|Staffing|Aerotek",
        "tech": True,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Sony",
        "pattern": "\\bsony\\b",
        "negative": "sodexo|aramark|compass group",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media+manufacturing",
    },
    {
        "canonical": "Hitachi",
        "pattern": "\\bHitachi\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "UPS",
        "pattern": "(?<![\\w-])UPS\\b|\\bUnited Parcel Servi",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "FedEx",
        "pattern": "\\bFed ?Ex\\b|\\bFederal Express\\b",
        "negative": "Command Security|Aramark|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "XPO",
        "pattern": "\\bXPO\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Yellow",
        "pattern": "\\bYRC\\b|\\bYellow (Corporation|Corp\\b|"
                   "Freight|Transportation)\\b|\\bRoadway Express\\b|"
                   "\\bUSF (Holland|Reddaway)\\b|\\bNew Penn Motor\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "J.B. Hunt",
        "pattern": "\\bJ\\.? ?B\\.? Hunt\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Schneider National",
        "pattern": "\\bSchneider National\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Ryder",
        "pattern": "\\bRyder\\b",
        "negative": "Ryder Cup|Ryder (Ave|St|Street|Rd|Road|Dr|Drive)\\b",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "DHL",
        "pattern": "\\bDHL\\b",
        "negative": "JMK Services|Command Security|Staffing",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Union Pacific",
        "pattern": "\\bUnion Pacific\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "CSX",
        "pattern": "\\bCSX\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Norfolk Southern",
        "pattern": "\\bNorfolk Southern\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "BNSF",
        "pattern": "\\bBNSF\\b|\\bBurlington Northern\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Amtrak",
        "pattern": "\\bamtrak\\b|national railroad passenger",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media+manufacturing",
    },
    {
        "canonical": "Pfizer",
        "pattern": "\\bpfizer\\b",
        "negative": "sodexo|aramark|compass group|aerotek",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Moderna",
        "pattern": "^moderna\\b|\\bmoderna(tx)?[, ]+(inc|llc|"
                   "us)|moderna therapeutics",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Johnson & Johnson",
        "pattern": "johnson\\s*&\\s*johnson|johnson and johnson|"
                   "\\bjanssen\\b|\\bkenvue\\b",
        "negative": "sodexo|aramark|compass group|aerotek",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Merck",
        "pattern": "\\bmerck\\b",
        "negative": "sodexo|aramark|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Bristol Myers Squibb",
        "pattern": "bristol[- ]?myers",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "AbbVie",
        "pattern": "\\babbvie\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Amgen",
        "pattern": "\\bamgen\\b",
        "negative": "bright horizons|sodexo|aramark|compass group",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Gilead Sciences",
        "pattern": "\\bgilead\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Eli Lilly",
        "pattern": "\\beli lilly\\b|\\blilly usa\\b|^lilly\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Novartis",
        "pattern": "\\bnovartis\\b|\\bsandoz\\b",
        "negative": "sodexo|aramark|compass group",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "AstraZeneca",
        "pattern": "astra\\s*zeneca",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "GSK",
        "pattern": "glaxo\\s*smith\\s*kline|glaxosmithkline|"
                   "^GSK\\b|\\bGSK (plc|consumer|inc)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Sanofi",
        "pattern": "\\bsanofi\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Abbott",
        "pattern": "\\babbott\\s+(laborator|labs|vascular|"
                   "rapid|point of care|nutrition|diabetes|"
                   "diagnostics|molecular|medical|informatics)|"
                   "^abbott(,| inc| labs)",
        "negative": "aerotek|sodexo|aramark|compass group|abbott house",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Kaiser Permanente",
        "pattern": "kaiser\\s+(permanente|foundation)",
        "negative": "morrison|sodexo|aramark|compass group|crothall|touchpoint",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "HCA Healthcare",
        "pattern": "^\\s*hca\\b|\\bhca\\s+(healthcare|shared\\s+services|"
                   "inc|management|physician|holdings|hospital)\\b",
        "negative": "sodexo|aramark|morrison|compass group|crothall|touchpoint",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Tenet Healthcare",
        "pattern": "\\btenet\\s+(health|healthcare|physician|"
                   "patient|hospital|medical)\\b",
        "negative": "sodexo|aramark|morrison|compass group|crothall",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Ascension Health",
        "pattern": "\\bascension\\s+(health|medical|technolog|"
                   "living|st\\.?|saint|via christi|seton|"
                   "columbia|borgess|genesys|providence|sacred|"
                   "macomb|michigan|wisconsin|texas)",
        "negative": "touchpoint|sodexo|aramark|morrison|compass group|crothall",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Providence Health",
        "pattern": "providence\\s+(health\\s*(&|and)\\s*(services|"
                   "$)|holy cross|expresscare|sacred heart|"
                   "saint joseph|st\\.? joseph|st\\.? vincent|"
                   "little company of mary|home\\b|medical\\s+(group|"
                   "foundation|institute))|^providence$",
        "negative": "our lady|farms|industries|learning|library|"
                    "marriott|mariott|nordstrom|call center|"
                    "new providence|life services|school|aramark|"
                    "sodexo|morrison|compass group",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Cleveland Clinic",
        "pattern": "cleveland clinic",
        "negative": "sodexo|aramark|morrison|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Mayo Clinic",
        "pattern": "mayo clinic",
        "negative": "sodexo|aramark|morrison|compass group",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Labcorp",
        "pattern": "\\blabcorp\\b|laboratory corporation of america",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "UnitedHealth Group",
        "pattern": "unitedhealth|united health\\s*(care|group)|"
                   "\\boptum|\\buniprise\\b",
        "negative": "sodexo|aramark|compass group",
        "tech": False,
        "tier": 1,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "CVS Health",
        "pattern": "^\\s*cvs\\b|\\bcvs\\s+(health|pharmacy|"
                   "caremark|specialty|store)\\b|caremark\\s+subsidiary\\s+of\\s+cv"
                   "s",
        "negative": "med\\s+care\\s+pharmacy|sodexo|aramark|compass\\s+group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media+retail",
    },
    {
        "canonical": "McKesson",
        "pattern": "\\bmckesson\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "AT&T",
        "pattern": "\\ba\\.?\\s*t\\.?\\s*&\\s*t\\b|american\\s+telephone\\s+(and|"
                   "&)\\s+telegraph",
        "negative": "sodexo|aramark|a\\.?\\s*t\\.?\\s*&\\s*t\\s+(stadium|"
                    "center|centre|park|field)",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Verizon",
        "pattern": "\\bverizon\\b",
        "negative": "RNN News|sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "T-Mobile",
        "pattern": "\\bt-mobile\\b|\\btmobile\\b|^t mobile\\b|\\bT Mobile USA\\b",
        "negative": "sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Sprint",
        "pattern": "^\\s*sprint\\b|\\bsprint\\s+(pcs|nextel)\\b",
        "negative": "newsprint|sprint\\s+(industrial|waste|"
                    "staffing|logistics|freight|carrier|transport|"
                    "systems|holdings|energy|electric|plumbing)",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Comcast",
        "pattern": "\\bcomcast\\b|\\bxfinity\\b",
        "negative": "bright horizons|sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Spectrum",
        "pattern": "charter communications|charter spectrum|"
                   "spectrum (cable|communications|enterprise|"
                   "reach|news|networks)",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "DISH Network",
        "pattern": "\\bdish\\s*network\\b|^dish$|^DISH -",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "DirecTV",
        "pattern": "\\bdirectv\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Lumen",
        "pattern": "centurylink|century link|lumen technologies|\\bqwest\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Nokia",
        "pattern": "\\bnokia\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Motorola",
        "pattern": "\\bmotorola\\b",
        "negative": "mobility|solutions",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Motorola Mobility",
        "pattern": "\\bmotorola\\s+mobility\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Motorola Solutions",
        "pattern": "\\bmotorola\\s+solutions\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Disney",
        "pattern": "\\bdisney",
        "negative": "sodexo|aryzta|morrow-meadows|restaurant associates|"
                    "compass group|aramark|deluca|aspire bakeries|"
                    "la brea|clubhouse|\\bat disney",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Warner Bros. Discovery",
        "pattern": "warner bros|warner brothers|warner\\s*media|"
                   "discovery communications",
        "negative": "sodexo|aramark|compass group|deluca",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Paramount",
        "pattern": "paramount\\s+(global|pictures|skydance|"
                   "network|home entertainment|studios|television|"
                   "players|worldwide)|paramount\\+",
        "negative": "compass group|eurest|aramark|sodexo|triage partners|"
                    "uptown productions|building services|\\bat paramount",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "NBCUniversal",
        "pattern": "nbcuniversal|nbc universal|\\btelemundo\\b",
        "negative": "bright horizons|sodexo|aramark|compass group",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Fox",
        "pattern": "fox\\s+(broadcasting|news|sports|corporation|"
                   "television|studio lot|entertainment group|"
                   "alternative entertainment|networks|cable)|"
                   "^fox corp",
        "negative": "restaurant concepts|fox factory|fox head|"
                    "fox knob|fox hills|m\\.e\\. fox|twentieth century|"
                    "21st century|20th century",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "20th Century Studios",
        "pattern": "twentieth\\s+century\\s+fox|21st\\s+century\\s+fox|"
                   "20th\\s+century\\s+(fox|studios)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "CBS",
        "pattern": "\\bCBS\\b|viacomcbs",
        "negative": "personnel|staffing|mechanical|construction|"
                    "logistics|trucking|\\brecord",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "ABC",
        "pattern": "\\bABC\\s+(studios|news|television|entertainment|"
                   "family|broadcasting|signature)|american broadcasting comp",
        "negative": "restaurant associates|sodexo|aramark|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "CNN",
        "pattern": "cable news network|\\bCNN\\b",
        "negative": "omni|hotel|marriott|hilton|sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Universal Studios",
        "pattern": "universal (city )?studios",
        "negative": "sodexo|aramark|compass group|deluca",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "AMC Theatres",
        "pattern": "american\\s+multi.?\\s?cinema|\\bAMC\\s+(theatre|"
                   "theater|entertainment|cinema|dine)|^\\s*AMC\\s+[A-Za-z][\\w'.&-"
                   "]*(?:\\s+[\\w'.&-]+){0,3}\\s+(?:[4-9]|"
                   "[1-3]\\d)\\b(?!\\d)(?=\\s*(?:$|[-–—,(]))",
        "negative": "deluca|sodexo|aramark|compass group|health|"
                    "solution|industr|staffing|supply|manufactur|"
                    "logistic|transport|construct|medical|ambulan",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Cinemark",
        "pattern": "\\bcinemark\\b",
        "negative": "deluca",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Live Nation",
        "pattern": "live nation",
        "negative": "sodexo|aramark|compass group|levy restaurant",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Ticketmaster",
        "pattern": "ticketmaster",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "The New York Times",
        "pattern": "new york times\\s*(co|company|newspaper|"
                   "building)|the new york times\\b|\\bnytimes\\b|"
                   "^new york times$",
        "negative": "hotel|square|marriott|hilton|sheraton|"
                    "novotel|renaissance|intercontinental|riu",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "HBO",
        "pattern": "home box office|\\bHBO\\b",
        "negative": "sodexo|aramark",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "SeaWorld",
        "pattern": "\\bseaworld\\b|sea world\\s*(llc|san diego|"
                   "orlando|parks|entertainment|inc)|^sea world$",
        "negative": "hotel|inn|suites|comfort|homewood|azul",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "American Airlines",
        "pattern": "american airlines",
        "negative": "\\bABM\\b|us perma|sodexo|aramark|swissport|"
                    "prospect airport|dolce\\s+international",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Delta Air Lines",
        "pattern": "delta air ?lines?\\b|^delta airlines",
        "negative": "sodexo|aramark|\\bABM\\b|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "United Airlines",
        "pattern": "united air\\s?lines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Southwest Airlines",
        "pattern": "southwest airlines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "JetBlue",
        "pattern": "jet\\s?blue airways|\\bjetblue\\b",
        "negative": "airline service professionals|swissport|\\bABM\\b|\\bat the\\b",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Alaska Airlines",
        "pattern": "alaska airlines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Spirit Airlines",
        "pattern": "spirit airlines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Frontier Airlines",
        "pattern": "frontier airlines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Hawaiian Airlines",
        "pattern": "hawaiian airlines",
        "negative": "sodexo|aramark|\\bABM\\b|swissport",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Marriott",
        "pattern": "\\bmarriott\\b",
        "negative": "sodexo|aramark|compass group|vacations?\\s+worldwide|"
                    "ownership\\s+resorts|resorts\\s+hospitality|"
                    "vacation\\s+club|host\\s+marriott",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Marriott Vacations Worldwide",
        "pattern": "\\bmarriott\\s+vacations?\\s+worldwide|"
                   "\\bmarriott\\s+ownership\\s+resorts|\\bmarriott\\s+resorts\\s+h"
                   "ospitality|\\bmarriott\\s+vacation\\s+club",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Ritz-Carlton",
        "pattern": "ritz.?\\s?carlton",
        "negative": "sodexo|aramark|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Hilton",
        "pattern": "\\bhilton\\b|waldorf astoria",
        "negative": "corporate casuals|hilton head|hilton davis|"
                    "paris hilton|sodexo|aramark|compass group|"
                    "hilton\\s+grand\\s+vac|hilton\\s+resorts\\s+corp|"
                    "hilton\\s+club|\\belara\\b",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Hilton Grand Vacations",
        "pattern": "\\bhilton\\s+grand\\s+vac|\\bhilton\\s+resorts\\s+corp|"
                   "\\belara\\s+by\\s+hilton|\\bhilton\\s+club\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Hyatt",
        "pattern": "\\bhyatt\\b",
        "negative": "sodexo|aramark|compass group|aspen sports",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Holiday Inn",
        "pattern": "\\bholiday inn\\b",
        "negative": "sodexo|aramark|compass group",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Expedia",
        "pattern": "\\bexpedia\\b",
        "negative": "",
        "tech": True,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Carnival Corporation",
        "pattern": "carnival (cruise|corporation|corp\\b|plc)|"
                   "holland america|princess cruises",
        "negative": "food store|foods|pharmacy|minyard",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Royal Caribbean",
        "pattern": "royal caribbean",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Avis Budget Group",
        "pattern": "\\bavis budget\\b|avis rent",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Hertz",
        "pattern": "^\\s*(the\\s+)?hertz\\b|\\bhertz\\s+(corporation|"
                   "corp|rent|equipment|global|transporting|"
                   "local)\\b",
        "negative": "\\bABM\\b|aramark|sodexo|baker hughes|"
                    "c/o|hertz\\s+(furniture|metals|farm)",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Enterprise Rent-A-Car",
        "pattern": "^\\s*enterprise\\s+(rent|holdings|fleet|"
                   "leasing|mobility)\\b|\\benterprise\\s+rent[\\s-]?a?[\\s-]?car\\"
                   "b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "MGM Resorts",
        "pattern": "MGM\\s+(resorts|grand|national harbor|"
                   "springfield|northfield|growth)|park mgm\\b|"
                   "\\bbellagio\\b|mandalay bay",
        "negative": "mgmt|management|medical group|women and children",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Caesars Entertainment",
        "pattern": "\\bcaesars\\b",
        "negative": "caesarstone|mr chow|stripeside|little caesars",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Sodexo",
        "pattern": "\\bsodexo\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Aramark",
        "pattern": "\\baramark\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Procter & Gamble",
        "pattern": "\\bproct[eo]r\\s*(&|and)\\s*gamble\\b",
        "negative": "compass\\s+group|eurest|aramark|sodexo",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Unilever",
        "pattern": "\\bunilever\\b|\\bconopco\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Colgate-Palmolive",
        "pattern": "\\bcolgate[\\s\\-]*palmolive\\b|\\bcolgate\\s+oral\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Clorox",
        "pattern": "\\bclorox\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "SC Johnson",
        "pattern": "\\bs\\.?\\s?c\\.?\\s+johnson\\b|\\bjohnson\\s+wax\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Church & Dwight",
        "pattern": "\\bchurch\\s*(&|and)\\s*dwight\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Reckitt Benckiser",
        "pattern": "\\breckitt\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Henkel",
        "pattern": "\\bhenkel\\b",
        "negative": "harris",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Energizer",
        "pattern": "\\benergizer\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Estee Lauder",
        "pattern": "\\best[eé]e?\\s+lauder\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Revlon",
        "pattern": "\\brevlon\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Avon",
        "pattern": "\\bavon\\s+products\\b|\\bnew\\s+avon\\b|^avon$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Herbalife",
        "pattern": "\\bherbalife\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "General Mills",
        "pattern": "\\bgeneral\\s+mills\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Kellogg's",
        "pattern": "\\bkellogg",
        "negative": "brown\\s*,?\\s*&\\s*root|\\broot\\b",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Kellanova",
        "pattern": "\\bkellanova\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Kraft Heinz",
        "pattern": "\\bkraft\\s+heinz\\b|\\bheinz\\b|\\bkraft\\s+(general\\s+)?food"
                   "s\\b",
        "negative": "store\\s*kraft|kraftmaid",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Oscar Mayer",
        "pattern": "\\boscar\\s+mayer\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Conagra Brands",
        "pattern": "\\bcon\\s?agra\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Campbell's",
        "pattern": "\\bcampbell'?s?\\s+(soup|snacks|fresh)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Hershey",
        "pattern": "\\bhershey",
        "negative": "medical|entertain|resort|hospital|park|"
                    "lodge|trust|realty|creamery|ice\\s+cream",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Mars",
        "pattern": "\\bmars\\s+(wrigley|petcare|food|inc|incorporated|"
                   "chocolate|candy|snackfood)\\b|\\bwrigley\\b",
        "negative": "super\\s*market",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Mondelez",
        "pattern": "\\bmondel[eē]z\\b|\\bnabisco\\b",
        "negative": "\\brjr\\b",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "PepsiCo",
        "pattern": "\\bpepsi",
        "negative": "pepsiamericas",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Frito-Lay",
        "pattern": "\\bfrito[\\s\\-]*lay\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Quaker Oats",
        "pattern": "\\bquaker\\s+oats\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Gatorade",
        "pattern": "\\bgatorade\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Coca-Cola",
        "pattern": "\\bcoca[\\s\\-]?cola\\b|\\bminute\\s+maid\\b",
        "negative": "\\breyes\\b|consolidated|liberty\\s+coca|"
                    "great\\s+lakes|\\bbci\\b|southwest\\s+beverages|"
                    "european\\s+partners",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Nestle",
        "pattern": "\\bnestl[eé]\\b",
        "negative": "elite\\s+staffing|labor\\s+network|post-nestle",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Danone",
        "pattern": "\\bdanone\\b|\\bdannon\\b|\\bstonyfield\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Tyson Foods",
        "pattern": "\\btyson\\s+(foods?|farms|fresh|prepared|"
                   "warehousing|extension)\\b|^tyson\\s*-",
        "negative": "tysons\\s+corner|\\bfortrex\\b|packers\\s+sanitation|\\bpssi\\b",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "JBS",
        "pattern": "\\bJBS\\s+(USA|Souderton|Foods|Beef|Pork|Green\\s+Bay|Swift)\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Smithfield Foods",
        "pattern": "\\bsmithfield\\s+(foods|packing|packaged|"
                   "fresh|distribution)\\b|^smithfield$",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Perdue Farms",
        "pattern": "\\bperdue\\s+(farms|foods)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Hormel Foods",
        "pattern": "\\bhormel\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Cargill",
        "pattern": "\\bcargill\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Archer Daniels Midland",
        "pattern": "\\barcher[\\s\\-]?daniels[\\s\\-]?midland\\b|"
                   "\\bADM\\s+(compan|milling|trucking|alliance|"
                   "processing|grain|animal|cocoa|nutrition|"
                   "inc\\b)|\\(ADM\\)|^ADM$",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Bunge",
        "pattern": "\\bbunge\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Pilgrim's Pride",
        "pattern": "\\bpilgrim'?s\\s+pride\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Foster Farms",
        "pattern": "\\bfoster\\s+farms\\b",
        "negative": "\\bpssi\\b|packers\\s+sanitation",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Sanderson Farms",
        "pattern": "\\bsanderson\\s+farms\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Butterball",
        "pattern": "\\bbutterball\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Boar's Head",
        "pattern": "\\bboar'?s\\s+head\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Hillshire Brands",
        "pattern": "\\bhillshire\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Sara Lee",
        "pattern": "\\bsara\\s+lee\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Bumble Bee Foods",
        "pattern": "\\bbumble\\s+bee\\s+(foods|seafood|tuna)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "StarKist",
        "pattern": "\\bstar\\s?kist\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Anheuser-Busch",
        "pattern": "\\banheuser",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Molson Coors",
        "pattern": "\\bmolson\\b|\\bcoors\\s+(brewing|beverage)\\b",
        "negative": "coorstek",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Constellation Brands",
        "pattern": "\\bconstellation\\s+brands\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Diageo",
        "pattern": "\\bdiageo\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Brown-Forman",
        "pattern": "\\bbrown[\\s\\-]forman\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Bacardi",
        "pattern": "\\bbacardi\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "E&J Gallo Winery",
        "pattern": "\\bgallo\\s+(winer|sales|vineyard)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Keurig Dr Pepper",
        "pattern": "\\bkeurig\\b|\\bdr\\.?\\s*pepper\\b|\\bsnapple\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Monster Beverage",
        "pattern": "\\bmonster\\s+(beverage|energy)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Red Bull",
        "pattern": "\\bred\\s+bull\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Niagara Bottling",
        "pattern": "\\bniagara\\s+bottling\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Del Monte Foods",
        "pattern": "\\bdel\\s+monte\\s+foods\\b|^\\s*del\\s+monte[,.]?\\s*(inc|"
                   "corp)",
        "negative": "capitol\\s+meat|fresh\\s+del\\s+monte|del\\s+monte\\s+fresh",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Dole",
        "pattern": "\\bdole\\s+(food|fresh|packaged|berry)\\b|^dole\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Chiquita",
        "pattern": "\\bchiquita\\b|\\bfresh\\s+express\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Driscoll's",
        "pattern": "\\bdriscoll'?s\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Ocean Spray",
        "pattern": "\\bocean\\s+spray\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Welch's",
        "pattern": "\\bwelch'?s\\b|\\bwelch\\s+foods\\b",
        "negative": "allyn|scientific",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Blue Diamond Growers",
        "pattern": "\\bblue\\s+diamond\\s+growers\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Land O'Lakes",
        "pattern": "\\bland\\s+o'?’?\\s?lakes\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Dairy Farmers of America",
        "pattern": "\\bdairy\\s+farmers\\s+of\\s+america\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Dean Foods",
        "pattern": "\\bdean\\s+foods\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Sysco",
        "pattern": "\\bsysco\\b",
        "negative": "packers\\s+sanitation|\\bpssi\\b",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "US Foods",
        "pattern": "\\bU\\.?\\s?S\\.?\\s+Foods\\b|\\bU\\.?\\s?S\\.?\\s+Foodservice"
                   "\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Performance Food Group",
        "pattern": "\\bperformance\\s+food\\s+group\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "McCormick",
        "pattern": "\\bmccormick\\s*(&|and)\\s*co",
        "negative": "schmick",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "J.M. Smucker",
        "pattern": "\\bsmucker",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Post Holdings",
        "pattern": "\\bpost\\s+holdings\\b|\\bmichael\\s+foods\\b|"
                   "\\bbob\\s+evans\\s+farms\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "TreeHouse Foods",
        "pattern": "\\btreehouse\\s+(foods|private\\s+brands)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Utz Brands",
        "pattern": "\\butz\\s+(quality|brands|snack)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Bimbo Bakeries",
        "pattern": "\\bbimbo\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Flowers Foods",
        "pattern": "\\bflowers\\s+(foods|baking)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Hostess Brands",
        "pattern": "\\bhostess\\s+(brands|cake|baking)\\b|"
                   "\\bwonder[\\s/]*hostess\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Ferrara Candy",
        "pattern": "\\bferrara\\s+candy\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Ferrero",
        "pattern": "\\bferrero\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Lamb Weston",
        "pattern": "\\blamb\\s+weston\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "J.R. Simplot",
        "pattern": "\\bsimplot\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Beyond Meat",
        "pattern": "\\bbeyond\\s+meat\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "HelloFresh",
        "pattern": "\\bhello\\s?fresh\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Corteva Agriscience",
        "pattern": "\\bcorteva\\b|\\bpioneer\\s+hi[\\s\\-]?bred\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Syngenta",
        "pattern": "\\bsyngenta\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Bayer CropScience",
        "pattern": "\\bbayer\\s+crop\\s?science\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "cpg",
    },
    {
        "canonical": "Scotts Miracle-Gro",
        "pattern": "\\bscotts\\s+(miracle|company)\\b|\\bmiracle[\\s\\-]gro\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "Blue Shield of California",
        "pattern": "\\bblue\\s*shield\\s+of\\s+california\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Premera Blue Cross",
        "pattern": "\\bpremera\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Horizon Blue Cross Blue Shield of New Jersey",
        "pattern": "\\bhorizon\\s+blue\\s*cross|\\bblue\\s*cross\\s+blue\\s*shield"
                   "\\s+of\\s+n(ew\\s+)?j(ersey)?\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Blue Cross Blue Shield of Texas",
        "pattern": "\\bblue\\s*cross\\s*(/|and\\s+)?\\s*blue\\s*shield\\s+of\\s+tex"
                   "as\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "BlueCross BlueShield of Tennessee",
        "pattern": "\\bblue\\s*cross\\s*(/|and)?\\s*blue\\s*sh(ie|"
                   "ei)ld\\s+of\\s+t(n|ennessee)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Blue Cross of Idaho",
        "pattern": "\\bblue\\s*cross\\s+of\\s+idaho\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "BlueCross BlueShield of South Carolina",
        "pattern": "\\bblue\\s*cross\\s*(and)?\\s*blue\\s*shield\\s+of\\s+south\\s+"
                   "carolina\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Blue Cross Blue Shield of Rhode Island",
        "pattern": "\\bblue\\s*cross\\s+blue\\s*shield\\s+of\\s+r(i|"
                   "hode\\s+island)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Regence",
        "pattern": "\\bregence\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "finance+healthcare_media",
    },
    {
        "canonical": "Mitsubishi Motors",
        "pattern": "\\bmitsubishi\\s+motor",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mitsubishi Electric",
        "pattern": "\\bmitsubishi\\s+electric",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mitsubishi Heavy Industries",
        "pattern": "\\bmitsubishi\\s+heavy\\s+(industries|equipment)",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mitsubishi Chemical",
        "pattern": "\\bmitsubishi\\s+chemical",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Mitsubishi Corporation",
        "pattern": "\\bmitsubishi\\s+international\\s+corp",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Advance Auto Parts",
        "pattern": "\\badvance\\s+auto\\s+parts\\b|^advance\\s+stores\\s+comp",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "David's Bridal",
        "pattern": "\\bdavid'?s\\s+bridal\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "HMSHost",
        "pattern": "\\bhms\\s?host\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "A&P",
        "pattern": "\\ba\\s*&\\s*p\\b|\\bgreat\\s+atlantic\\s*&?\\s*pacific\\b|"
                   "\\bsuper\\s?fresh\\b|\\bpathmark\\b|\\bwaldbaum",
        "negative": "coat.*apron|unitex",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Northwest Airlines",
        "pattern": "\\bnorthwest\\s+airlines\\b|\\bnorthwest\\s+airlink\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Delaware North",
        "pattern": "\\bdelaware\\s+north\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "US Cellular",
        "pattern": "\\bu\\.?\\s?s\\.?\\s+cellular\\b|\\bunited\\s+states\\s+cellula"
                   "r\\b|\\buscellular\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "DynCorp",
        "pattern": "\\bdyncorp\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Jabil",
        "pattern": "\\bjabil\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Eastern Air Lines",
        "pattern": "\\beastern\\s+air\\s?lines\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Harrah's",
        "pattern": "\\bharrah'?s\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Horseshoe Casino",
        "pattern": "\\bhorseshoe\\s+(casino|hammond|bossier|"
                   "tunica|council\\s+bluffs|indianapolis|"
                   "baltimore|lake\\s+charles|southern\\s+indiana)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Shaw Industries",
        "pattern": "\\bshaw\\s+industries\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "First Student",
        "pattern": "\\bfirst\\s+student\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Gate Gourmet",
        "pattern": "\\bgate\\s+gourmet\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "LSG Sky Chefs",
        "pattern": "\\bsky\\s+chefs\\b|\\blsg\\s+sky\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "MV Transportation",
        "pattern": "\\bmv\\s+transportation\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Convergys",
        "pattern": "\\bconvergys\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "First Transit",
        "pattern": "\\bfirst\\s+transit\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Durham School Services",
        "pattern": "\\bdurham\\s+school\\s+services\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Fluor",
        "pattern": "\\bfluor\\s+(corp|enterprise|daniel|federal|"
                   "government|marine)\\b|^fluor\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Concentrix",
        "pattern": "\\bconcentrix\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Transdev",
        "pattern": "\\btransdev\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Silgan",
        "pattern": "\\bsilgan\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Alorica",
        "pattern": "\\balorica\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Crothall",
        "pattern": "\\bcrothall\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "P.F. Chang's",
        "pattern": "\\bp\\.?\\s?f\\.?\\s+chang",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Toys R Us",
        "pattern": "\\btoys\\s*[\"']?\\s*r\\s*[\"']?\\s*us\\b|"
                   "\\btoysrus\\b|\\bbabies\\s*[\"']?\\s*r\\s*[\"']?\\s*us\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "WestRock",
        "pattern": "\\bwestrock\\b|\\bsmurfit\\s+westrock\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Alcatel",
        "pattern": "\\balcatel\\b",
        "negative": "genesys",
        "tech": True,
        "tier": 3,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "RR Donnelley",
        "pattern": "\\br\\.?\\s?r\\.?\\s+donnelley\\b|\\bdonnelley\\s+financial\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Montgomery Ward",
        "pattern": "\\bmontgomery\\s+ward\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Cox Automotive",
        "pattern": "\\bcox\\s+automotive\\b|\\bautotrader\\b|"
                   "\\bkelley\\s+blue\\s+book\\b|\\bmanheim\\s+(auto|"
                   "auction)",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Cox Communications",
        "pattern": "\\bcox\\s+(communications|business)\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Cox Media Group",
        "pattern": "\\bcox\\s+media\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Shopko",
        "pattern": "\\bshopko\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "American Apparel",
        "pattern": "\\bamerican\\s+apparel\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "Corizon Health",
        "pattern": "\\bcorizon\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Mohawk Industries",
        "pattern": "\\bmohawk\\s+(industries|carpet|flooring)\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Wachovia",
        "pattern": "\\bwachovia\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Maytag",
        "pattern": "\\bmaytag\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Amentum",
        "pattern": "\\bamentum\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Reynolds Metals",
        "pattern": "\\breynolds\\s+metals\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Reynolds Consumer Products",
        "pattern": "\\breynolds\\s+(consumer|wrap)\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "cpg",
    },
    {
        "canonical": "AECOM",
        "pattern": "\\baecom\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "LEGOLAND",
        "pattern": "\\blegoland\\b|\\blego\\s+(brand|systems)\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Thermo Fisher Scientific",
        "pattern": "\\bthermo\\s+fisher\\b|\\bfisher\\s+scientific\\b|"
                   "\\bthermo\\s+electron\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Conduent",
        "pattern": "\\bconduent\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Cooper Tire",
        "pattern": "\\bcooper\\s+tire\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Cooper Standard",
        "pattern": "\\bcooper[\\s-]standard\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Husqvarna",
        "pattern": "\\bhusqvarna\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Cingular Wireless",
        "pattern": "\\bcingular\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Oxford Industries",
        "pattern": "\\boxford\\s+industries\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "retail",
    },
    {
        "canonical": "Tommy Bahama",
        "pattern": "\\btommy\\s+bahama\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Electrolux",
        "pattern": "\\belectrolux\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Fruit of the Loom",
        "pattern": "\\bfruit\\s+of\\s+the\\s+loom\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "99 Cents Only",
        "pattern": "\\b99\\s*(cents?|¢)\\s*only\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "retail",
    },
    {
        "canonical": "American Greetings",
        "pattern": "\\bamerican\\s+greetings\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "cpg",
    },
    {
        "canonical": "Zenith Electronics",
        "pattern": "\\bzenith\\s+electronics\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Harris Teeter",
        "pattern": "\\bharris\\s+teeter\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Cardinal Health",
        "pattern": "\\bcardinal\\s+health\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Madison Square Garden",
        "pattern": "\\bmadison\\s+square\\s+garden\\b|\\bmsg\\s+(entertainment|"
                   "arena|sports|network)\\b|\\bradio\\s+city\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Continental Airlines",
        "pattern": "\\bcontinental\\s+air\\s?lines\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Visionworks",
        "pattern": "\\bvisionworks\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "retail",
    },
    {
        "canonical": "Symantec",
        "pattern": "\\bsymantec\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "NortonLifeLock",
        "pattern": "\\bnortonlifelock\\b|\\bgen\\s+digital\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Peraton",
        "pattern": "\\bperaton\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Perspecta",
        "pattern": "\\bperspecta\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Howmet Aerospace",
        "pattern": "\\bhowmet\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Ericsson",
        "pattern": "\\bericsson\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "manufacturing+tech",
    },
    {
        "canonical": "Kodak",
        "pattern": "\\beastman\\s+kodak\\b|^kodak\\b",
        "negative": "",
        "tech": False,
        "tier": 1,
        "sector": "manufacturing",
    },
    {
        "canonical": "Boardriders",
        "pattern": "\\bboardriders\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "retail",
    },
    {
        "canonical": "Quiksilver",
        "pattern": "\\bquiksilver\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Savers",
        "pattern": "\\bsavers\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Value Village",
        "pattern": "\\bvalue\\s+village\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Leidos",
        "pattern": "\\bleidos\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Schneider Electric",
        "pattern": "\\bschneider\\s+electric\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "manufacturing",
    },
    {
        "canonical": "Sun Microsystems",
        "pattern": "\\bsun\\s+microsystems\\b",
        "negative": "",
        "tech": True,
        "tier": 2,
        "sector": "tech",
    },
    {
        "canonical": "STMicroelectronics",
        "pattern": "\\bstmicroelectronics\\b",
        "negative": "",
        "tech": True,
        "tier": 3,
        "sector": "tech",
    },
    {
        "canonical": "Devon Energy",
        "pattern": "\\bdevon\\s+energy\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "CNH Industrial",
        "pattern": "\\bcnh\\s+industrial\\b|\\bcase\\s+new\\s+holland\\b",
        "negative": "",
        "tech": False,
        "tier": 3,
        "sector": "manufacturing",
    },
    {
        "canonical": "Bebe",
        "pattern": "\\bbebe\\s+stores\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
    {
        "canonical": "Pennymac",
        "pattern": "\\bpennymac\\b|\\bpenny\\s?mac\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "finance",
    },
    {
        "canonical": "Cedars-Sinai",
        "pattern": "\\bcedars[\\s-]sinai\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "healthcare_media",
    },
    {
        "canonical": "Finish Line",
        "pattern": "\\bfinish\\s+line\\b",
        "negative": "",
        "tech": False,
        "tier": 2,
        "sector": "retail",
    },
]


# --- resolution ------------------------------------------------------

_COMPILED = [
    (_b,
     re.compile(_b["pattern"], re.IGNORECASE),
     re.compile(_b["negative"], re.IGNORECASE) if _b["negative"] else None)
    for _b in BRANDS
]

_BY_CANONICAL = {_b["canonical"]: _b for _b in BRANDS}

# What may sit in front of a brand and still leave it "first": a leading
# article, because it is part of plenty of real names ("The Great Atlantic &
# Pacific Tea Co. - A&P-Hoboken"), and the bookkeeping marks state agencies
# staple onto a company string -- "(*)UPS", "*Updated* Thermo Fisher
# Scientific", "*UPDATED* Schneider Electric 6th Notice". Without the second
# half, "(*)UPS" (177 employees) was vetoed as a contractor row because of
# its opening parenthesis.
#
# The status words must be BRACKETED or ASTERISKED. Accepting them bare made
# this match "new " and "final ", which are ordinary words that begin real
# company names (New Balance, New York & Company, Final Touch) -- and because
# a match here SKIPS the contractor and multi-brand vetoes entirely, that was
# a veto-disable path: "new Costco Wholesale" resolved to Costco.
_LEADING_NOISE = re.compile(
    r"^[\s*()\[\]{}#~.,:;\"'\-\u2013\u2014]*"
    r"(?:[*(\[]\s*(?:updated?|amended|revised|corrected)\s*[*)\]]"
    r"[\s*()\[\]{}#~.,:;\-\u2013\u2014]*)*"
    r"(?:the\s+)?$",
    re.IGNORECASE)

# Text in front of the winning brand that means the filer is somebody else:
# a contractor, a franchisee, a management company or a landlord.
_CONTRACTOR_PREFIX = re.compile(
    r"\s\(?at\)?\s"                 # "... Inc. at Meta", "... LLC (at) Hilton"
    # \s@\s required exactly one space each side, so deleting one keystroke
    # defeated the veto: "790 French LLC @The Hilton Garden Inn" resolved to
    # Hilton. An "@" in front of a brand is never anything but a venue pointer.
    r"|\s*@\s*"
    r"|\bd\s*/?\s*b\s*/?\s*a\b"     # dba, d/b/a, d b a
    r"|\ba\s*/?\s*k\s*/?\s*a\b"     # "LSC Communications LLC aka RR Donnelley"
    r"|\("                          # "Housing Works Inc. (at Holiday Inn ...)"
    r"|providing\s+services\s+for"
    r"|on\s+behalf\s+of"
    # The long forms above fire zero times in the corpus; these are what the
    # feeds actually write.
    r"|\sfor\s"                     # "Artisan Restaurant Collection for Sodexo"
    r"|\swith\s"                    # "... P.C. with Humana at Home Inc."
    #
    # "/" is NOT here, and that is a measured decision. It reads as a
    # contractor separator in "Apogee Call Center/Savers", but at corpus scale
    # it is far more often a parent ("Illinois Bell/AT&T", "Adm/archer Daniels
    # Midland"), a sister brand ("LSG Group/Sky Chefs") or two CITIES
    # ("Doubletree by Hilton - Anaheim/Orange"). Vetoing on it lost more
    # correct rows than it saved.
    r"|\b(?:hospitality|lodging|staffing|associates|ventures|holdings)\b"
    r"|\bservices,?\s+(?:inc|llc)\b"
    r"|\bmanagement,?\s+(?:llc|corp)\b",
    re.IGNORECASE)

# Hotel and casino flags double as venue and address labels, so they only
# win when no OTHER operator is named. Sister brands under one parent are
# not another operator: "The Ritz-Carlton, Los Angeles, JW Marriott L.A.
# LIVE" (1,009 employees) is one Marriott filing, not an ambiguous one, so
# families collapse before the count.
_LODGING = frozenset((
    "Marriott", "Hilton", "Hyatt", "Holiday Inn", "Ritz-Carlton",
    "MGM Resorts", "Caesars Entertainment", "Harrah's", "Horseshoe Casino",
    "Marriott Vacations Worldwide", "Hilton Grand Vacations",
))

_FAMILY = {
    "Marriott": "marriott",
    "Ritz-Carlton": "marriott",
    "Caesars Entertainment": "caesars",
    "Harrah's": "caesars",
    "Horseshoe Casino": "caesars",
}


def _all_matches(company):
    """Every brand whose pattern hits `company` and whose negative does not."""
    out = []
    for brand, pos, neg in _COMPILED:
        m = pos.search(company)
        if m and not (neg and neg.search(company)):
            out.append((brand, m))
    return out


def _starts_the_string(company, match):
    """True when only an article or a filing marker precedes the match."""
    return bool(_LEADING_NOISE.match(company[:match.start()]))


def resolve(company):
    """Canonical brand for a RAW company string, or None.

    Applies the deterministic winner rule, then the contractor, multi-brand
    and lodging vetoes. Returns None whenever the filing employer is not
    unambiguously the brand -- never a best guess, because the output of
    this function is printed in a public post naming a real company.
    """
    # The docstring promises "or None", and warn_brands is documented as a
    # standalone leaf: a source module that skips warn_x_select's str()
    # coercion must not crash the run on an int or a bytes cell. The length
    # cap is cost control -- resolve() runs 633 patterns per call, so a
    # malformed 1 MB feed cell would cost seconds per record.
    if not isinstance(company, str):
        return None
    company = company.strip()[:400]
    if not company:
        return None
    matches = _all_matches(company)
    if not matches:
        return None

    matches.sort(key=lambda t: (t[1].start(),
                                -(t[1].end() - t[1].start()),
                                t[0]["tier"],
                                t[0]["canonical"]))
    brand, match = matches[0]

    # Multi-brand veto: nobody is at the front of the string, so the string
    # is a list of tenants or concessions, not one employer's name.
    distinct = {b["canonical"] for b, _ in matches}
    if len(distinct) > 1 and not any(_starts_the_string(company, m)
                                     for _, m in matches):
        return None

    # Lodging ambiguity veto.
    if brand["canonical"] in _LODGING:
        families = {_FAMILY.get(c, c) for c in distinct}
        if len(families) > 1:
            return None

    # Contractor veto. Read the WHOLE string, not just what precedes the
    # match: the "(" that proves a string is a venue list can sit on either
    # side. Reading only the prefix gave identical shapes opposite answers --
    # "Sikorsky (a Lockheed Martin Company)" was vetoed while
    # "Pillar Hotels & Resorts Holiday Inn (Frederick)" resolved to Holiday Inn.
    if not _starts_the_string(company, match):
        if _CONTRACTOR_PREFIX.search(company):
            return None

    return brand["canonical"]


# --- lookups ---------------------------------------------------------

def is_brand(canonical):
    """True when `canonical` is a name this registry can post."""
    return canonical in _BY_CANONICAL


# "healthcare_media", "retail+tech" — the sector field is provenance from the
# sector agent that drafted an entry, not a vocabulary. Split it, so a caller
# asking `"tech" in tags(...)` or `"finance" in tags(...)` gets an answer
# instead of silently always False.
def _sector_tags(sector: str) -> set:
    return {part for part in re.split(r"[^a-z0-9]+", (sector or "").lower())
            if part and part not in ("and", "the")}


def tags(canonical):
    """Clean tag set for a brand — {"tech"}, {"finance"}, … Empty when unknown.

    Returning the raw sector string as a single member made every membership
    test fail: AT&T's tags were {"healthcare_media"}, so `"tech" in tags` was
    False for a telecom, and False for EVERY brand — which quietly turned
    warn_x_select's tech arm into dead code.
    """
    brand = _BY_CANONICAL.get(canonical)
    if brand is None:
        return set()
    out = _sector_tags(brand["sector"])
    if brand["tech"]:
        out.add("tech")
    return out


def tier(canonical):
    """1/2/3 per the TIER RULE above, or None when unknown."""
    brand = _BY_CANONICAL.get(canonical)
    return brand["tier"] if brand else None


def display(canonical):
    """The canonical IS the display name: echo it back, or None if unknown."""
    return canonical if canonical in _BY_CANONICAL else None


def all_brands():
    """Canonicals in registry order."""
    return [_b["canonical"] for _b in BRANDS]
