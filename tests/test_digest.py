"""Tests for warn_digest — the whole-US monthly digest email builder."""

import json
from datetime import date
from pathlib import Path

import pytest

import warn_digest as wd


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def rec(state, company="Acme", employees=None, notice=None, effective=None,
        city="", county=""):
    """One unified-schema national record."""
    return {
        "state": state,
        "company": company,
        "notice_date": notice,
        "effective_date": effective,
        "employees": employees,
        "layoff_type": "",
        "county": county,
        "city": city,
        "address": "",
        "industry": "",
    }


def write_national(tmp_path: Path, records, states=None, name="national.json"):
    """Write a stand-in data/warn_national.json and return its path."""
    codes = states if states is not None else sorted(
        {r["state"] for r in records}
    )
    payload = {
        "last_updated": "2026-07-01T00:00:00+00:00Z",
        "states_live": len(codes),
        "total_records": len(records),
        "states": {c: {"name": wd.STATE_NAMES.get(c, c)} for c in codes},
        "records": records,
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def simple_file(tmp_path):
    """June 2026 with May 2026 and June 2025 baselines."""
    records = [
        # --- June 2026 (the digest month) --------------------------------
        rec("CA", "Big Co", 500, notice="2026-06-10", county="Alameda"),
        rec("CA", "Small Co", 100, notice="2026-06-20", city="Fresno"),
        rec("NY", "Gotham Inc", 300, notice="2026-06-05"),
        # --- May 2026 (prior month) --------------------------------------
        rec("CA", "Big Co", 200, notice="2026-05-10"),
        rec("NY", "Gotham Inc", 600, notice="2026-05-11"),
        # --- June 2025 (same month last year) ----------------------------
        rec("CA", "Old Co", 400, notice="2025-06-15"),
    ]
    return write_national(tmp_path, records, states=["CA", "NY", "HI"])


# ---------------------------------------------------------------------------
# Calendar + math helpers
# ---------------------------------------------------------------------------


def test_month_bounds():
    assert wd.month_bounds(2026, 6) == (date(2026, 6, 1), date(2026, 6, 30))
    assert wd.month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert wd.month_bounds(2025, 2) == (date(2025, 2, 1), date(2025, 2, 28))


def test_month_bounds_rejects_bad_month():
    with pytest.raises(ValueError):
        wd.month_bounds(2026, 13)
    with pytest.raises(ValueError):
        wd.month_bounds(2026, 0)


def test_previous_month_wraps_the_year():
    assert wd.previous_month(2026, 6) == (2026, 5)
    assert wd.previous_month(2026, 1) == (2025, 12)


def test_same_month_last_year():
    assert wd.same_month_last_year(2026, 1) == (2025, 1)


def test_previous_complete_month():
    assert wd.previous_complete_month(date(2026, 7, 29)) == (2026, 6)
    assert wd.previous_complete_month(date(2026, 1, 2)) == (2025, 12)


def test_period_label():
    assert wd.period_label(2026, 6) == "June 2026"


def test_pct_change_guards_zero_baseline():
    assert wd.pct_change(10, 5) == 100.0
    assert wd.pct_change(0, 5) == -100.0
    # Zero (or missing) baseline has no defined percentage — never a crash,
    # never a fabricated infinity.
    assert wd.pct_change(7, 0) is None
    assert wd.pct_change(0, 0) is None
    assert wd.pct_change(7, None) is None


def test_employee_count_treats_non_positive_as_unreported():
    assert wd.employee_count(120) == 120
    assert wd.employee_count("85") == 85
    assert wd.employee_count(12.7) == 12
    # Feeds with no headcount column publish 0 for every notice.
    assert wd.employee_count(0) is None
    assert wd.employee_count(None) is None
    assert wd.employee_count("") is None
    assert wd.employee_count("n/a") is None


def test_event_date_prefers_notice_date_and_never_synthesizes():
    when, field = wd.event_date(
        rec("CA", notice="2026-06-10", effective="2026-08-01")
    )
    assert (when, field) == (date(2026, 6, 10), "notice_date")

    when, field = wd.event_date(rec("GA", effective="2026-06-10"))
    assert (when, field) == (date(2026, 6, 10), "effective_date")

    assert wd.event_date(rec("CA")) == (None, None)


# ---------------------------------------------------------------------------
# Month filtering
# ---------------------------------------------------------------------------


def test_month_filtering_excludes_records_outside_the_month(tmp_path):
    records = [
        rec("CA", "In A", 10, notice="2026-06-01"),      # first day: in
        rec("CA", "In B", 20, notice="2026-06-30"),      # last day: in
        rec("CA", "Out early", 30, notice="2026-05-31"),  # day before: out
        rec("CA", "Out late", 40, notice="2026-07-01"),  # day after: out
        # notice_date wins: filed in May even though it takes effect in June.
        rec("CA", "Straddler", 50, notice="2026-05-20", effective="2026-06-10"),
        # No notice date published -> bucketed by its real effective date.
        rec("GA", "Eff only", 60, effective="2026-06-12"),
        rec("CA", "No dates", 70),                       # undateable: excluded
    ]
    path = write_national(tmp_path, records)
    stats = wd.build_stats(2026, 6, path)

    assert stats["notices"] == 3
    assert stats["employees"] == 10 + 20 + 60
    assert {r["code"] for r in stats["states"]} == {"CA", "GA"}
    assert stats["states_with_activity"] == 2


def test_records_with_no_usable_date_never_land_in_any_month(tmp_path):
    path = write_national(tmp_path, [rec("CA", "Ghost", 99)])
    for month in range(1, 13):
        assert wd.build_stats(2026, month, path)["notices"] == 0


# ---------------------------------------------------------------------------
# Delta math
# ---------------------------------------------------------------------------


def test_mom_and_yoy_deltas(simple_file):
    stats = wd.build_stats(2026, 6, simple_file)

    assert stats["notices"] == 3
    assert stats["employees"] == 900
    assert stats["prior"] == {
        "label": "May 2026", "year": 2026, "month": 5,
        "notices": 2, "employees": 800,
    }
    assert stats["last_year"]["label"] == "June 2025"
    assert stats["last_year"]["notices"] == 1
    assert stats["last_year"]["employees"] == 400

    delta = stats["delta"]
    assert delta["notices_mom"] == 1
    assert delta["notices_mom_pct"] == pytest.approx(50.0)
    assert delta["employees_mom"] == 100
    assert delta["employees_mom_pct"] == pytest.approx(12.5)
    assert delta["notices_yoy_pct"] == pytest.approx(200.0)
    assert delta["employees_yoy_pct"] == pytest.approx(125.0)


def test_zero_baseline_deltas_are_none_not_a_crash(tmp_path):
    # Nothing at all in May 2026 or June 2025 — both baselines are zero.
    path = write_national(tmp_path, [rec("CA", "Only", 250, notice="2026-06-09")])
    digest = wd.build_monthly_digest(2026, 6, path)
    delta = digest["stats"]["delta"]

    assert delta["notices_mom_pct"] is None
    assert delta["employees_mom_pct"] is None
    assert delta["notices_yoy_pct"] is None
    assert delta["employees_yoy_pct"] is None
    assert delta["employees_mom"] == 250

    # And it is described honestly rather than as a percentage.
    assert "no May 2026 baseline" in digest["text"]
    assert "no June 2025 baseline" in digest["text"]


def test_state_row_zero_baseline_delta(tmp_path):
    records = [
        rec("CA", "Steady", 100, notice="2026-06-02"),
        rec("CA", "Steady", 50, notice="2026-05-02"),
        rec("NY", "Brand New", 400, notice="2026-06-03"),  # no May activity
    ]
    path = write_national(tmp_path, records)
    rows = {r["code"]: r for r in wd.build_stats(2026, 6, path)["states"]}

    assert rows["CA"]["employees_delta_pct"] == pytest.approx(100.0)
    assert rows["NY"]["prev_employees"] == 0
    assert rows["NY"]["employees_delta"] == 400
    assert rows["NY"]["employees_delta_pct"] is None


# ---------------------------------------------------------------------------
# Unreported headcounts
# ---------------------------------------------------------------------------


def test_states_without_headcounts_are_not_reported_as_zero(tmp_path):
    records = [
        # HI-style feed: notices published, headcount column absent (0s).
        rec("HI", "Island Resort", 0, notice="2026-06-04"),
        rec("HI", "Beach Cafe", None, notice="2026-06-08"),
        rec("CA", "Counted Co", 300, notice="2026-06-06"),
    ]
    path = write_national(tmp_path, records)
    digest = wd.build_monthly_digest(2026, 6, path)
    stats = digest["stats"]
    rows = {r["code"]: r for r in stats["states"]}

    assert rows["HI"]["notices"] == 2
    assert rows["HI"]["counts_reported"] is False
    assert wd._emp_text(rows["HI"]) == "counts not reported"

    # National totals count the notices but never invent headcounts.
    assert stats["notices"] == 3
    assert stats["employees"] == 300
    assert stats["notices_without_counts"] == 2

    # The rendered digest says "counts not reported" for Hawaii, not "0".
    hawaii_lines = [
        ln for ln in digest["text"].splitlines() if ln.startswith("Hawaii")
    ]
    assert len(hawaii_lines) == 1
    tail = hawaii_lines[0].split("Hawaii (HI)")[1]
    assert "counts not reported" in tail
    # No fabricated "0 employees" anywhere on Hawaii's row.
    assert "0" not in tail.replace("counts not reported", "")
    assert "counts not reported" in digest["html"]


def test_partial_headcount_reporting_is_flagged(tmp_path):
    records = [
        rec("WV", "Has count", 40, notice="2026-06-04"),
        rec("WV", "No count", 0, notice="2026-06-05"),
    ]
    path = write_national(tmp_path, records)
    digest = wd.build_monthly_digest(2026, 6, path)
    row = digest["stats"]["states"][0]

    assert row["notices"] == 2
    assert row["notices_with_counts"] == 1
    assert row["partial_counts"] is True
    assert row["employees"] == 40
    assert wd._emp_text(row) == "40*"
    assert "publish no headcount" in digest["text"]


def test_headcountless_states_are_left_out_of_movers(tmp_path):
    records = [
        rec("HI", "Island A", 0, notice="2026-06-04"),
        rec("HI", "Island B", 0, notice="2026-05-04"),
        rec("CA", "Counted", 900, notice="2026-06-06"),
        rec("CA", "Counted", 100, notice="2026-05-06"),
    ]
    path = write_national(tmp_path, records)
    stats = wd.build_stats(2026, 6, path)

    assert [m["code"] for m in stats["movers_up"]] == ["CA"]
    assert "HI" not in {m["code"] for m in stats["movers_up"]}
    assert "HI" not in {m["code"] for m in stats["movers_down"]}


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


def test_state_table_lists_every_active_state_sorted_by_employees(tmp_path):
    records = [
        rec("CA", "A", 100, notice="2026-06-01"),
        rec("NY", "B", 900, notice="2026-06-02"),
        rec("TX", "C", 500, notice="2026-06-03"),
        rec("HI", "D", 0, notice="2026-06-04"),
    ]
    path = write_national(tmp_path, records)
    rows = wd.build_stats(2026, 6, path)["states"]

    assert [r["code"] for r in rows] == ["NY", "TX", "CA", "HI"]
    assert all(r["notices"] >= 1 for r in rows)


def test_movers_rank_by_headcount_swing(tmp_path):
    records = [
        rec("CA", "A", 100, notice="2026-06-01"),
        rec("CA", "A", 900, notice="2026-05-01"),   # -800
        rec("NY", "B", 700, notice="2026-06-02"),
        rec("NY", "B", 200, notice="2026-05-02"),   # +500
        rec("OH", "C", 50, notice="2026-05-03"),    # went silent: -50
    ]
    path = write_national(tmp_path, records)
    stats = wd.build_stats(2026, 6, path)

    assert [m["code"] for m in stats["movers_up"]] == ["NY"]
    assert [m["code"] for m in stats["movers_down"]] == ["CA", "OH"]
    assert stats["movers_down"][0]["employees_delta"] == -800
    ohio = stats["movers_down"][1]
    assert ohio["employees"] == 0 and ohio["prev_employees"] == 50
    assert ohio["employees_delta_pct"] == pytest.approx(-100.0)


def test_largest_notices_and_top_employers(tmp_path):
    records = [
        rec("CA", "MegaCorp", 400, notice="2026-06-01", city="Oakland"),
        rec("NY", "MegaCorp  ", 350, notice="2026-06-02"),
        rec("TX", "Tiny LLC", 5, notice="2026-06-03"),
        rec("CA", "Solo Inc", 600, notice="2026-06-04"),
        rec("HI", "Uncounted Co", 0, notice="2026-06-05"),
    ]
    path = write_national(tmp_path, records)
    stats = wd.build_stats(2026, 6, path)

    largest = stats["largest_notices"]
    assert [n["employees"] for n in largest] == [600, 400, 350, 5]
    assert largest[0]["company"] == "Solo Inc"
    # A notice with no published headcount is not ranked as the smallest.
    assert "Uncounted Co" not in {n["company"] for n in largest}

    employers = {e["company"].strip(): e for e in stats["top_employers"]}
    assert employers["MegaCorp"]["employees"] == 750
    assert employers["MegaCorp"]["notices"] == 2
    assert employers["MegaCorp"]["states"] == ["CA", "NY"]
    assert stats["top_employers"][0]["employees"] == 750


# ---------------------------------------------------------------------------
# Coverage caveats
# ---------------------------------------------------------------------------


def test_no_data_states_line_names_every_uncoverable_state(simple_file):
    digest = wd.build_monthly_digest(2026, 6, simple_file)
    stats = digest["stats"]

    assert [g["code"] for g in stats["gap_states"]] == sorted(wd.GAP_STATES)
    for text in (digest["text"], digest["html"]):
        assert "States with no data this month" in text
        for code in ("AR", "WY", "NH", "MO", "TX"):
            assert wd.STATE_NAMES[code] in text
            assert f"({code})" in text
    # The reasons travel with the names, so absence is never read as zero.
    assert "confidential by statute" in digest["text"]
    assert "bot wall" in digest["text"]
    assert "publishes no publicly available WARN list" in digest["text"]


def test_quiet_tracked_states_are_reported_separately(simple_file):
    stats = wd.build_stats(2026, 6, simple_file)
    # HI is tracked by the platform but filed nothing this month — that is a
    # quiet state, not a coverage gap.
    assert stats["quiet_states"] == ["HI"]
    assert "HI" not in {g["code"] for g in stats["gap_states"]}


def test_gap_state_with_archived_data_is_not_called_missing(tmp_path):
    records = [
        rec("TX", "Historical Co", 200, notice="2019-06-10"),
        rec("CA", "Other Co", 50, notice="2019-06-11"),
    ]
    path = write_national(tmp_path, records)
    digest = wd.build_monthly_digest(2019, 6, path)
    stats = digest["stats"]

    assert "TX" not in {g["code"] for g in stats["gap_states"]}
    assert [g["code"] for g in stats["gap_states_with_history"]] == ["TX"]
    assert "TX" in {r["code"] for r in stats["states"]}
    assert "archived records only" in digest["text"]


# ---------------------------------------------------------------------------
# Empty month
# ---------------------------------------------------------------------------


def test_empty_month_builds_a_valid_digest(simple_file):
    digest = wd.build_monthly_digest(2026, 9, simple_file)
    stats = digest["stats"]

    assert stats["empty"] is True
    assert stats["notices"] == 0
    assert stats["employees"] == 0
    assert stats["states"] == []
    assert stats["largest_notices"] == []
    assert stats["top_employers"] == []
    assert "no notices recorded" in digest["subject"]
    assert "September 2026" in digest["subject"]
    for body in (digest["text"], digest["html"]):
        assert body.strip()
        assert "September 2026" in body
        assert "not a statement that no layoffs occurred" in body
        assert "States with no data this month" in body


def test_completely_empty_dataset_does_not_crash(tmp_path):
    path = write_national(tmp_path, [], states=[])
    digest = wd.build_monthly_digest(2026, 6, path)
    assert digest["stats"]["empty"] is True
    assert digest["stats"]["quiet_states"] == []
    assert digest["html"] and digest["text"]


# ---------------------------------------------------------------------------
# Rendering contract
# ---------------------------------------------------------------------------


def test_digest_shape_and_period_label(simple_file):
    digest = wd.build_monthly_digest(2026, 6, simple_file)

    assert set(digest) == {
        "year", "month", "period_label", "html", "text", "subject", "stats"
    }
    assert (digest["year"], digest["month"]) == (2026, 6)
    assert digest["period_label"] == "June 2026"
    assert digest["html"].strip() and digest["text"].strip()
    assert "June 2026" in digest["html"]
    assert "June 2026" in digest["text"]
    assert "June 2026" in digest["subject"]


def test_html_is_self_contained_and_email_safe(simple_file):
    html = wd.build_monthly_digest(2026, 6, simple_file)["html"]

    for tag in ("<script", "<img", "<link", "<iframe", "<style"):
        assert tag not in html.lower()
    for banned in ("@media", 'class="', "url("):
        assert banned not in html            # no external CSS/JS/images
    assert "max-width:640px" in html
    assert 'style="' in html                  # inline styles only
    assert wd.US_DASHBOARD_URL in html
    # The dashboard is the only external URL the email points at.
    assert html.count("http") == html.count(wd.US_DASHBOARD_URL)


def test_text_alternative_stands_on_its_own(simple_file):
    text = wd.build_monthly_digest(2026, 6, simple_file)["text"]

    assert "<" not in text and "&nbsp;" not in text
    assert "U.S. WARN LAYOFF NOTICES — JUNE 2026" in text
    assert "Notices filed:" in text
    assert "Employees affected:" in text
    assert wd.US_DASHBOARD_URL in text


def test_subject_summarises_the_month(simple_file):
    subject = wd.build_monthly_digest(2026, 6, simple_file)["subject"]
    assert subject.startswith("US WARN monthly digest — June 2026:")
    assert "3 notices" in subject
    assert "900 employees" in subject


def test_company_names_are_html_escaped(tmp_path):
    path = write_national(
        tmp_path, [rec("CA", "Smith & Sons <Holdings>", 10, notice="2026-06-01")]
    )
    html = wd.build_monthly_digest(2026, 6, path)["html"]
    assert "Smith &amp; Sons &lt;Holdings&gt;" in html
    assert "<Holdings>" not in html


# ---------------------------------------------------------------------------
# I/O + CLI
# ---------------------------------------------------------------------------


def test_missing_dataset_raises_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="National dataset not found"):
        wd.build_monthly_digest(2026, 6, tmp_path / "nope.json")


def test_bare_record_list_is_tolerated(tmp_path):
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([rec("CA", "Solo", 12, notice="2026-06-01")]))
    stats = wd.build_stats(2026, 6, path)
    assert stats["notices"] == 1
    assert stats["employees"] == 12


def test_cli_prints_text_and_writes_html(simple_file, tmp_path, capsys):
    out_html = tmp_path / "preview" / "digest.html"
    code = wd.main(
        [
            "--year", "2026", "--month", "6",
            "--national", str(simple_file),
            "--html", str(out_html),
            "--stats",
        ]
    )
    captured = capsys.readouterr().out

    assert code == 0
    assert "Subject: US WARN monthly digest — June 2026" in captured
    assert "U.S. WARN LAYOFF NOTICES — JUNE 2026" in captured
    assert out_html.exists()
    assert "max-width:640px" in out_html.read_text()
    assert '"period_label": "June 2026"' in captured


def test_cli_defaults_to_the_previous_complete_month(simple_file, capsys):
    expected = wd.period_label(*wd.previous_complete_month())
    wd.main(["--national", str(simple_file)])
    assert expected in capsys.readouterr().out
