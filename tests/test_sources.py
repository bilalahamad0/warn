"""Tests for the warn_sources multi-state architecture (Phase 0)."""

import json

import pandas as pd
import pytest

import warn_sources
from warn_sources import aggregate
from warn_sources.base import UNIFIED_FIELDS, Source, StatePaths


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSource(Source):
    """Minimal source: no network, feeds a fixed DataFrame through the engine."""

    code = "zz"
    name = "Testland"
    agency = "Testland Dept. of Labor"
    source_url = "https://example.gov/warn"
    frames: list = []  # queue of DataFrames returned by successive parses

    def fetch(self, force: bool = False):
        return True, self.paths.raw

    def parse(self, raw_path) -> pd.DataFrame:
        return self.frames.pop(0).copy()


def _frame(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "company": c,
                "notice_date": nd,
                "effective_date": ed,
                "employees": emp,
                "county": "Test County",
                "city": "Testville",
                "layoff_type": "Layoff",
            }
            for c, nd, ed, emp in rows
        ]
    )


# ---------------------------------------------------------------------------
# Registry + paths
# ---------------------------------------------------------------------------


def test_registry_contains_california():
    codes = [s.code for s in warn_sources.all_sources()]
    assert "ca" in codes


def test_ca_uses_grandfathered_legacy_paths():
    ca = warn_sources.get_source("ca")
    assert ca.paths.latest.name == "warn_latest.json"
    assert ca.paths.latest.parent.name == "data"          # not data/states/ca
    assert ca.paths.notified.parent == ca.paths.latest.parent


def test_get_source_unknown_state_raises():
    with pytest.raises(KeyError):
        warn_sources.get_source("xx")


def test_state_paths_default_layout(tmp_path):
    p = StatePaths.for_state("nj", tmp_path)
    assert p.root == tmp_path / "states" / "nj"
    assert p.latest == p.root / "warn_latest.json"
    assert p.notified == p.root / "notified_keys.json"
    p.ensure()
    assert p.root.is_dir()


# ---------------------------------------------------------------------------
# Unified schema
# ---------------------------------------------------------------------------


def test_unify_stamps_state_and_fills_missing_columns(tmp_path):
    src = FakeSource(tmp_path)
    df = src.unify(_frame(("Acme", "2026-01-01", "2026-03-01", 25)))
    assert list(df.columns)[: len(UNIFIED_FIELDS)] == UNIFIED_FIELDS
    assert (df["state"] == "ZZ").all()
    assert (df["address"] == "").all()      # missing string field -> ""
    assert (df["industry"] == "").all()


# ---------------------------------------------------------------------------
# Generic engine (fetch -> parse -> diff -> persist) in an isolated tmp dir
# ---------------------------------------------------------------------------


def test_engine_first_run_baselines_then_alerts_only_new(tmp_path):
    src = FakeSource(tmp_path)
    FakeSource.frames = [
        _frame(("Acme", "2026-01-01", "2026-03-01", 25)),
        _frame(("Acme", "2026-01-01", "2026-03-01", 25),
               ("Globex", "2026-02-01", "2026-04-01", 50)),
    ]

    # First run establishes the baseline — never alerts on the backlog.
    r1 = src.run()
    assert r1["state"] == "ZZ"
    assert r1["diff"]["new_count"] == 0
    assert src.paths.latest.exists()
    assert src.paths.notified.exists()
    assert src.paths.cumulative.exists()

    # Second run: only the genuinely new filing alerts.
    r2 = src.run()
    assert r2["diff"]["new_count"] == 1
    assert r2["diff"]["new_entries"][0]["company"] == "Globex"
    assert r2["diff"]["new_entries"][0]["state"] == "ZZ"

    # Persisted records carry the state stamp.
    latest = json.loads(src.paths.latest.read_text())
    assert all(r["state"] == "ZZ" for r in latest["records"])
    assert latest["source_url"] == FakeSource.source_url


def test_engine_files_are_isolated_per_state(tmp_path):
    class OtherSource(FakeSource):
        code = "qq"

    a, b = FakeSource(tmp_path), OtherSource(tmp_path)
    assert a.paths.root != b.paths.root
    assert a.paths.notified != b.paths.notified


def test_run_all_isolates_a_failing_source(tmp_path, monkeypatch):
    class BrokenSource(FakeSource):
        code = "br"

        def fetch(self, force=False):
            raise RuntimeError("site moved behind a bot wall")

    class GoodSource(FakeSource):
        code = "ok"

    FakeSource.frames = [_frame(("Acme", "2026-01-01", "2026-03-01", 25))]
    monkeypatch.setattr(
        warn_sources, "SOURCES", {"br": BrokenSource, "ok": GoodSource}
    )
    results = warn_sources.run_all(data_dir=tmp_path)

    assert "site moved" in results["br"]["error"]
    assert results["br"]["diff"]["new_count"] == 0
    assert "error" not in results["ok"]          # the good source still ran
    assert results["ok"]["state"] == "OK"


# ---------------------------------------------------------------------------
# National aggregation
# ---------------------------------------------------------------------------


def test_build_national_merges_states_and_stamps_legacy_records(tmp_path, monkeypatch):
    class EastSource(FakeSource):
        code = "ea"
        name = "Eastland"

    class WestSource(FakeSource):
        code = "we"
        name = "Westland"

    monkeypatch.setattr(
        warn_sources, "SOURCES", {"ea": EastSource, "we": WestSource}
    )

    # Seed each state's cumulative store; Eastland's records predate the
    # unified schema (no state field) to prove legacy stamping works.
    for cls, records in [
        (EastSource, [{"company": "Acme", "employees": 10}]),
        (WestSource, [{"company": "Globex", "employees": 20, "state": "WE"}]),
    ]:
        paths = cls(tmp_path).paths
        paths.ensure()
        paths.cumulative.write_text(json.dumps({"records": records}))

    out = tmp_path / "warn_national.json"
    payload = aggregate.build_national(data_dir=tmp_path, output_file=out)

    assert payload["states_live"] == 2
    assert payload["total_records"] == 2
    assert payload["total_employees"] == 30
    assert payload["states"]["EA"]["total_employees"] == 10
    by_company = {r["company"]: r for r in payload["records"]}
    assert by_company["Acme"]["state"] == "EA"       # legacy record stamped
    assert by_company["Globex"]["state"] == "WE"
    assert json.loads(out.read_text())["states_live"] == 2


def test_build_national_skips_states_with_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(warn_sources, "SOURCES", {"zz": FakeSource})
    out = tmp_path / "warn_national.json"
    payload = aggregate.build_national(data_dir=tmp_path, output_file=out)
    assert payload["states_live"] == 0
    assert payload["records"] == []


# ---------------------------------------------------------------------------
# Record sanity guard — one state's parse debris must not distort the nation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "7.4031878 C39.1565598",      # SVG path data scraped as a company (AL)
    "9.61276098 38.1747183",      # bare coordinates
    "",
])
def test_junk_company_names_are_rejected(name):
    from warn_sources.base import is_junk_company
    assert is_junk_company(name) is True


@pytest.mark.parametrize("name", [
    "3M Company", "3M", "A&B Inc", "7-Eleven", "Big 5 Sporting Goods",
    "21st Century Fox", "H.B. Fuller", "Amazon",
])
def test_real_company_names_survive_the_guard(name):
    """Digits and punctuation are normal in company names — only debris goes."""
    from warn_sources.base import is_junk_company
    assert is_junk_company(name) is False


def test_implausible_dates_are_nulled_not_kept():
    from warn_sources.base import plausible_date
    assert plausible_date("2040-09-01") is None      # crippled the dashboard
    assert plausible_date("1930-03-30") is None
    assert plausible_date("2028-08-25") == "2028-08-25"   # scheduled, genuine
    assert plausible_date("1987-09-01") == "1987-09-01"   # oldest real record


def test_unify_drops_junk_and_nulls_bad_dates(tmp_path):
    src = FakeSource(tmp_path)
    df = pd.DataFrame([
        {"company": "7.4031878 C39.1565598", "notice_date": "2034-12-01",
         "effective_date": "2040-09-01", "employees": 0},
        {"company": "Acme", "notice_date": "2026-01-01",
         "effective_date": "2040-09-01", "employees": 25},
    ])
    out = src.unify(df)
    assert list(out["company"]) == ["Acme"]           # debris row gone
    assert out.iloc[0]["effective_date"] is None      # implausible date nulled
    assert out.iloc[0]["notice_date"] == "2026-01-01"  # good date untouched


def test_build_national_cleans_stale_junk_already_in_a_store(tmp_path, monkeypatch):
    """A bad row written before a parser fix is cleaned on the next build."""
    monkeypatch.setattr(warn_sources, "SOURCES", {"zz": FakeSource})
    paths = FakeSource(tmp_path).paths
    paths.ensure()
    paths.cumulative.write_text(json.dumps({"records": [
        {"company": "7.4031878 C39.1565598", "employees": 0,
         "notice_date": "2034-12-01", "effective_date": "2040-09-01"},
        {"company": "Acme", "employees": 10, "notice_date": "2026-05-01"},
    ]}))
    payload = aggregate.build_national(
        data_dir=tmp_path, output_file=tmp_path / "warn_national.json"
    )
    assert [r["company"] for r in payload["records"]] == ["Acme"]
