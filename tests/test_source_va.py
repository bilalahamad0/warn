"""Tests for the Virginia (VA) WARN source."""

import json
import re
from pathlib import Path

import pytest

import warn_monitor
import warn_sources
from warn_sources import va as va_module

FIXTURE = Path(__file__).parent / "fixtures" / "va_sample.csv"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_virginia():
    assert "va" in warn_sources.SOURCES
    cls = warn_sources.SOURCES["va"]
    assert cls.code == "va"
    assert cls.name == "Virginia"
    assert cls.source_url.startswith("https://virginiaworks.gov/")


def test_paths_use_per_state_layout(tmp_path):
    src = warn_sources.get_source("va", tmp_path)
    assert src.paths.root == tmp_path / "states" / "va"
    assert src.paths.latest.name == "warn_latest.json"


# ---------------------------------------------------------------------------
# Offline parse against a 13-row fixture of real feed rows
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed(tmp_path):
    src = warn_sources.get_source("va", tmp_path)
    return src.parse(FIXTURE)


def test_parse_columns(parsed):
    assert list(parsed.columns) == [
        "company",
        "notice_date",
        "effective_date",
        "employees",
        "layoff_type",
        "city",
    ]
    # VA publishes no county, street address, or industry; "Contact
    # Person" / "Collective Bargaining Unit" have no unified field.
    for absent in ("county", "address", "industry", "Contact Person"):
        assert absent not in parsed.columns


def test_parse_row_count(parsed):
    assert len(parsed) == 13
    assert (parsed["company"].str.strip() != "").all()


def test_parse_dates_are_iso_or_none(parsed):
    for col in ("notice_date", "effective_date"):
        for val in parsed[col]:
            assert val is None or ISO_DATE.match(val), (col, val)


def test_parse_employees_are_int(parsed):
    assert all(isinstance(v, int) for v in parsed["employees"])
    assert parsed["employees"].sum() == 1359  # fixture total, all published


def test_parse_clean_row(parsed):
    row = parsed[parsed["company"] == "Emerson"]
    assert row["notice_date"].tolist() == ["2026-07-09"]
    assert row["effective_date"].tolist() == ["2026-09-30"]
    assert row["employees"].tolist() == [139]
    assert row["layoff_type"].tolist() == ["Closure"]
    # Feed's double space ("Charlottesville  VA") collapses; " VA" strips.
    assert row["city"].tolist() == ["Charlottesville"]


def test_parse_unescapes_html_entities(parsed):
    # The CSV export carries "T&amp;H Services LLC".
    assert "T&H Services LLC" in set(parsed["company"])
    row = parsed[parsed["company"] == "Medical Faculty Associates, Inc. (MFA)"]
    if row.empty:  # feed writes it without the comma
        row = parsed[parsed["company"] == "Medical Faculty Associates Inc. (MFA)"]
    assert row["city"].tolist() == ["Alexandria, Arlington, Ashburn & Reston"]


def test_parse_applies_bln_1973_date_correction(parsed):
    # BLN warn-transformer date_corrections: "10/01/1973" -> None.
    row = parsed[parsed["company"] == "Yoga Works Inc."]
    assert row["notice_date"].tolist() == ["2020-07-14"]
    assert row["effective_date"].tolist() == [None]


def test_parse_location_variants(parsed):
    def city_of(company, **extra):
        rows = parsed[parsed["company"] == company]
        for col, val in extra.items():
            rows = rows[rows[col] == val]
        assert len(rows) == 1, company
        return rows["city"].tolist()[0]

    assert city_of("Conduent Commercial Solutions LLC") == "VA-Statewide"
    # Doubled state suffix "Sandston, VA VA" -> "Sandston".
    assert city_of("LL Flooring", layoff_type="Closure") == "Sandston"
    # Bare " VA" location -> empty, never a fabricated city.
    assert city_of("PAE Shared Services LLC") == ""
    # Out-of-state suffixes stay verbatim.
    assert city_of("Kmart") == "Hoffman Estates IL"
    assert city_of("American Eagle") == "Washington DC"


def test_parse_layoff_type_variants(parsed):
    types = dict(zip(parsed["company"], parsed["layoff_type"]))
    assert types["Management Science for Health (MSH)"] == ""  # blank in feed
    assert types["American Eagle"] == "Permanent Reduction"
    # The feed runs multi-type values together; kept verbatim.
    combined = parsed[parsed["layoff_type"] == "ClosureLayoff"]
    assert combined["company"].tolist() == ["LL Flooring"]
    assert combined["city"].tolist() == ["Richmond"]


def test_parse_strips_company_whitespace(parsed):
    # Feed publishes "General Dynamics Information Technology " (trailing sp).
    assert "General Dynamics Information Technology" in set(parsed["company"])


def test_unify_produces_unified_schema(tmp_path, parsed):
    src = warn_sources.get_source("va", tmp_path)
    df = src.unify(parsed)
    assert list(df.columns)[: len(warn_sources.UNIFIED_FIELDS)] == (
        warn_sources.UNIFIED_FIELDS
    )
    assert (df["state"] == "VA").all()
    for col in ("county", "address", "industry"):
        assert (df[col] == "").all()


# ---------------------------------------------------------------------------
# Cleaning helpers (rules vendored from BLN warn-transformer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("07/09/2026", "2026-07-09"),
        ("1/5/2020", "2020-01-05"),
        ("10/01/1973", None),  # vendored BLN date correction
        ("", None),
        (None, None),
        ("TBD", None),
    ],
)
def test_clean_date(raw, expected):
    assert va_module._clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Charlottesville  VA", "Charlottesville"),
        ("Clintwood &amp; Nora VA", "Clintwood & Nora"),
        ("VA-Statewide VA", "VA-Statewide"),
        ("Sandston, VA VA", "Sandston"),
        (" VA", ""),
        ("Hoffman Estates IL", "Hoffman Estates IL"),
        ("Washington, DC DC", "Washington, DC DC"),
        ("Pittsylvania County VA", "Pittsylvania County"),
    ],
)
def test_clean_location(raw, expected):
    assert va_module._clean_location(raw) == expected


# ---------------------------------------------------------------------------
# Rescinded notices carry no effective date
# ---------------------------------------------------------------------------

_HEADER = (
    "Company,Notice Date,Impact Date,Employees Affected,Location,"
    "Contact Person,Notice Type,Collective Bargaining Unit\n"
)


def _csv(tmp_path, name, *rows):
    path = tmp_path / name
    path.write_text(_HEADER + "".join(r + "\n" for r in rows))
    return path


def _aerofarms(impact_date):
    """The feed's currently-rescinded notice: Impact Date = the render date."""
    return (
        f'"AeroFarms Inc. - Rescinded",04/29/2026,{impact_date},133,'
        '"Ringgold VA","Carlos Nunez",Closure,'
    )


_EMERSON = (
    'Emerson,07/09/2026,09/30/2026,139,"Charlottesville VA",Justin Johnson,'
    "Closure,"
)


@pytest.mark.parametrize("company,expected", [
    ("AeroFarms Inc. - Rescinded", True),
    ("JELD-WEN-rescinded", True),
    ("Pyrotechnique by Grucci Inc. *notice rescinded", True),
    ("Emerson", False),
    ("", False),
    (None, False),
])
def test_is_rescinded(company, expected):
    assert va_module.is_rescinded(company) is expected


def test_parse_rescinded_rows_have_no_effective_date(tmp_path):
    """Every way the feed has spelled a rescission, and nothing else."""
    csv = _csv(
        tmp_path, "r.csv",
        _aerofarms("09/02/2026"),
        '"Pyrotechnique by Grucci Inc. *notice rescinded",04/04/2016,04/08/2016,0,'
        '"Radford VA",Someone,Layoff,',
        'JELD-WEN-rescinded,11/06/2012,12/31/2012,138,"Christianburg VA",'
        "Someone,Closure,",
        _EMERSON,
    )
    df = warn_sources.get_source("va", tmp_path).parse(csv)
    by = {r["company"]: r for r in df.to_dict("records")}
    for name in (
        "AeroFarms Inc. - Rescinded",
        "Pyrotechnique by Grucci Inc. *notice rescinded",
        "JELD-WEN-rescinded",
    ):
        assert by[name]["effective_date"] is None, name
    # The row itself stays exactly as published — a rescission is information.
    aero = by["AeroFarms Inc. - Rescinded"]
    assert aero["notice_date"] == "2026-04-29"
    assert aero["employees"] == 133
    assert aero["layoff_type"] == "Closure"
    assert aero["city"] == "Ringgold"
    # A live notice keeps its date.
    assert by["Emerson"]["effective_date"] == "2026-09-30"


def _stub_fetch(src, monkeypatch, tmp_path):
    """Make fetch return whatever CSV the test staged last as feed.csv."""
    monkeypatch.setattr(
        src, "fetch", lambda force=False: (True, str(tmp_path / "feed.csv"))
    )


def _va_with_feed(tmp_path, monkeypatch):
    """A VA source whose fetch returns whatever CSV the test staged last."""
    src = warn_sources.get_source("va", tmp_path)
    src.paths.ensure()
    _stub_fetch(src, monkeypatch, tmp_path)
    return src


def test_daily_redating_of_a_rescinded_notice_is_not_an_amendment(
    tmp_path, monkeypatch
):
    """The Virginia churn, end to end through the shared engine: the same
    rescinded filing with a new Impact Date every download must produce no
    amendment, no new notice, no ledger growth, and one dashboard record."""
    src = _va_with_feed(tmp_path, monkeypatch)
    _csv(tmp_path, "feed.csv", _aerofarms("08/30/2026"), _EMERSON)
    src.run()  # baseline
    ledger_before = src.paths.notified.read_text()

    for day in ("08/31/2026", "09/01/2026", "09/02/2026"):
        _csv(tmp_path, "feed.csv", _aerofarms(day), _EMERSON)
        diff = src.run()["diff"]
        assert diff["amendment_count"] == 0, day
        assert diff["new_count"] == 0, day
        assert diff["removed_count"] == 0, day

    assert src.paths.notified.read_text() == ledger_before
    assert not src.paths.amended.exists()
    cumulative = json.loads(src.paths.cumulative.read_text())["records"]
    aero = [r for r in cumulative if r["company"].startswith("AeroFarms")]
    assert len(aero) == 1
    assert aero[0]["effective_date"] is None


def test_seeded_ledgers_make_the_key_change_silent(tmp_path, monkeypatch):
    """The production transition. Before this rule the live ledgers held the
    rescinded notice under a dated key and the cumulative store carried that
    dated version. Seeding the new undated key into BOTH ledgers (done once,
    by hand, for the three rescinded rows) turns the parser's key change into
    a no-op: nothing reported, nothing held, and the dated version collapses
    out of the cumulative store on the next run."""
    src = _va_with_feed(tmp_path, monkeypatch)

    # The state the live pipeline left behind, written through the engine so
    # every file has its real shape: a run under the OLD rule (dated).
    monkeypatch.setattr(va_module, "is_rescinded", lambda company: False)
    _csv(tmp_path, "feed.csv", _aerofarms("09/02/2026"), _EMERSON)
    src.run()
    dated_key = "AeroFarms Inc. - Rescinded__2026-09-02__133"
    assert dated_key in json.loads(src.paths.notified.read_text())["keys"]
    monkeypatch.undo()
    _stub_fetch(src, monkeypatch, tmp_path)

    # The one-off seed.
    undated_key = "AeroFarms Inc. - Rescinded__None__133"
    warn_monitor.record_notified_keys([undated_key], src.paths.notified)
    warn_monitor.record_amended_keys([undated_key], src.paths.amended)

    _csv(tmp_path, "feed.csv", _aerofarms("09/03/2026"), _EMERSON)
    diff = src.run()["diff"]
    assert diff["amendment_count"] == 0
    assert diff["new_count"] == 0
    cumulative = json.loads(src.paths.cumulative.read_text())["records"]
    aero = [r for r in cumulative if r["company"].startswith("AeroFarms")]
    assert [r["effective_date"] for r in aero] == [None]


def test_unseeded_key_change_surfaces_as_one_held_amendment_not_a_new_notice(
    tmp_path, monkeypatch
):
    """Without the seed the change is still safe: the engine pairs the undated
    row with its dated predecessor as ONE amendment (held, never a fresh
    'new notice' alert) and the cumulative store still ends up with one row."""
    src = _va_with_feed(tmp_path, monkeypatch)
    monkeypatch.setattr(va_module, "is_rescinded", lambda company: False)
    _csv(tmp_path, "feed.csv", _aerofarms("09/02/2026"), _EMERSON)
    src.run()
    monkeypatch.undo()
    _stub_fetch(src, monkeypatch, tmp_path)

    _csv(tmp_path, "feed.csv", _aerofarms("09/03/2026"), _EMERSON)
    diff = src.run()["diff"]
    assert diff["new_count"] == 0
    assert diff["amendment_count"] == 1
    assert diff["amendments"][0]["old_effective_date"] == "2026-09-02"
    assert diff["amendments"][0]["new_effective_date"] is None
    cumulative = json.loads(src.paths.cumulative.read_text())["records"]
    aero = [r for r in cumulative if r["company"].startswith("AeroFarms")]
    assert [r["effective_date"] for r in aero] == [None]
