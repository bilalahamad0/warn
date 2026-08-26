"""Tests for chart rendering (warn_charts).

Focused on chart 7, the year-over-year bar, which was rebuilt to read the
national dataset instead of the fiscal-year PDF sample in warn_all_years.json.
That sample captured 3-5% of actual filings and rendered California's 2020
COVID spike — 6,066 notices covering 656,501 employees — as 14 notices and
1,394 employees.
"""

import json
from datetime import datetime, timezone

import pytest

import warn_charts


YEAR = datetime.now(timezone.utc).year


@pytest.fixture(autouse=True)
def _charts_to_tmp(tmp_path, monkeypatch):
    """Keep generated fragments out of the real docs/charts."""
    out = tmp_path / "charts"
    out.mkdir()
    monkeypatch.setattr(warn_charts, "CHARTS_DIR", out)
    return out


def _entry(year, records, employees, partial=False, gaps=()):
    return {"year": year, "label": str(year), "records": records,
            "employees": employees, "partial": partial,
            "gap_months": list(gaps)}


@pytest.fixture
def summary():
    return [
        _entry(2014, 338, 30231, partial=True, gaps=("02", "04", "05")),
        _entry(2019, 729, 60937),
        _entry(2020, 6066, 656501),
        _entry(2025, 827, 45924, partial=True, gaps=("02", "03", "04")),
        _entry(2026, 831, 46585, partial=True),
    ]


def test_years_are_plotted_in_chronological_order(summary):
    """Complete and incomplete years are separate traces so they can be styled
    differently, and Plotly derives category order from trace order — which
    puts 2014 after 2020 unless the axis is pinned."""
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    assert list(fig.layout.xaxis.categoryarray) == [
        "2014", "2019", "2020", "2025", "2026"
    ]
    assert fig.layout.xaxis.categoryorder == "array"


def test_complete_and_incomplete_years_are_visually_distinct(summary):
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    bars = [t for t in fig.data if t.type == "bar"]
    assert len(bars) == 2
    solid = next(t for t in bars if not t.marker.pattern.shape)
    hatched = next(t for t in bars if t.marker.pattern.shape)
    assert list(solid.x) == ["2019", "2020"]
    assert list(hatched.x) == ["2014", "2025", "2026"]


def test_the_2020_spike_is_charted_at_full_size(summary):
    """The whole point of the rebuild: the PDF sample flattened this year."""
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    solid = next(t for t in fig.data if t.type == "bar" and not t.marker.pattern.shape)
    assert dict(zip(solid.x, solid.y))["2020"] == 656501


def test_notice_count_spans_every_year(summary):
    """It used to be plotted for the live year alone — a single point, because
    the PDF-derived counts were not comparable to it."""
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    line = next(t for t in fig.data if t.type == "scatter")
    assert list(line.x) == ["2014", "2019", "2020", "2025", "2026"]
    assert list(line.y) == [338, 729, 6066, 827, 831]


def test_a_gap_year_says_which_months_are_missing(summary):
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    hatched = next(t for t in fig.data
                   if t.type == "bar" and t.marker.pattern.shape)
    hovers = dict(zip(hatched.x, hatched.hovertemplate))
    assert "February, March and April" in hovers["2025"]
    assert "below the" in hovers["2025"]
    # The running year is incomplete for a different reason, and says so.
    assert "still in progress" in hovers["2026"]
    assert "No filings recorded" not in hovers["2026"]


def test_caption_names_the_incomplete_years(summary):
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    caption = fig.layout.annotations[0].text
    assert "2014, 2025, 2026" in caption
    assert "Calendar years" in caption
    # The retired framing must not linger in the copy.
    assert "PDF" not in caption
    assert "Fiscal" not in fig.layout.xaxis.title.text


def test_all_complete_years_get_a_clean_caption():
    fig = warn_charts.chart_yoy_bar(
        [_entry(2019, 729, 60937), _entry(2020, 6066, 656501)], save_png=False
    )
    assert "Every year shown is complete." in fig.layout.annotations[0].text
    assert len([t for t in fig.data if t.type == "bar"]) == 1


def test_empty_summary_renders_an_empty_state():
    """Rather than falling back to the 3%-complete PDF sample."""
    fig = warn_charts.chart_yoy_bar([], save_png=False)
    assert "No historical data yet" in fig.layout.title.text
    assert not fig.data


def test_title_spans_the_actual_range(summary):
    fig = warn_charts.chart_yoy_bar(summary, save_png=False)
    assert "(2014–2026)" in fig.layout.title.text


# ---------------------------------------------------------------------------
# recent_year_window — the six-year window shared by the US map and the
# national ranking charts in warn_site_us
# ---------------------------------------------------------------------------


def test_a_future_year_does_not_count_against_the_window():
    # 2027 exists only because one filing carries no notice date and an
    # effective date that far out. Counted against max_years it cost the
    # window its oldest real year; it now sits beside the six, not inside them.
    years = {"2027", "2026", "2025", "2024", "2023", "2022", "2021", "2020"}
    assert warn_charts.recent_year_window(years, now_year=2026) == [
        "2027", "2026", "2025", "2024", "2023", "2022", "2021",
    ]


def test_the_window_still_stops_at_max_years_without_a_future_year():
    years = {str(y) for y in range(2018, 2027)}
    assert warn_charts.recent_year_window(years, now_year=2026) == [
        "2026", "2025", "2024", "2023", "2022", "2021",
    ]


def test_every_future_year_sorts_ahead_of_the_recent_ones():
    assert warn_charts.recent_year_window({2029, 2027, 2026, 2025},
                                          now_year=2026) == [2029, 2027, 2026, 2025]


def test_window_hands_back_the_type_it_was_given():
    assert warn_charts.recent_year_window({2025, 2024}, now_year=2026) == [2025, 2024]
    assert warn_charts.recent_year_window({"2025"}, now_year=2026) == ["2025"]
    assert warn_charts.recent_year_window(set(), now_year=2026) == []


def test_us_map_keeps_six_recent_years_beside_a_future_one(tmp_path, monkeypatch):
    """The map builds its own dropdown, so pin its window end to end.

    Seven whole years of history plus one future-dated notice: the six most
    recent must all survive, which before the shared window they did not —
    the lone future bucket displaced the oldest of them.
    """
    records = [
        {"state": "CA", "company": f"Acme {y}", "notice_date": f"{y}-06-01",
         "effective_date": f"{y}-08-01", "employees": 100}
        for y in range(YEAR - 6, YEAR + 1)
    ]
    records.append(
        {"state": "MI", "company": "General Motors", "notice_date": None,
         "effective_date": f"{YEAR + 1}-01-14", "employees": 350}
    )
    national = tmp_path / "warn_national.json"
    national.write_text(json.dumps({
        "records": records,
        "states": {"CA": {"name": "California"}, "MI": {"name": "Michigan"}},
    }))
    monkeypatch.setattr(warn_charts, "NATIONAL_FILE", national)

    fig = warn_charts.chart_us_map(save_png=False)
    menu = fig.layout.updatemenus[1]          # [0] is the metric toggle
    labels = [b.label for b in menu.buttons]

    # All time heads the list, the current year follows and is the default …
    assert labels[:2] == ["All years", str(YEAR)]
    assert menu.buttons[menu.active].label == str(YEAR)
    # … the future bucket stays selectable …
    assert str(YEAR + 1) in labels
    # … and it costs the window nothing: six recent years, oldest included.
    assert {lbl for lbl in labels if lbl != "All years"} == (
        {str(YEAR + 1)} | {str(y) for y in range(YEAR, YEAR - 6, -1)}
    )
