import json
from unittest.mock import patch
import warn_publish


@patch("warn_publish.git_commit_push")
@patch("warn_publish.build_site")
@patch("warn_publish.warn_charts.run")
@patch("warn_publish.warn_history.run")
@patch("warn_publish.warn_diff.generate_report")
@patch("warn_publish.warn_monitor.run")
def test_run_full_pipeline(
    mock_monitor, mock_diff, mock_history, mock_charts, mock_site, mock_push, tmp_path
):
    """run() orchestrates every stage and honours no_push — without touching the
    real data/ directory, the network, or git.

    Every stage that does real I/O (monitor download, diff report, historical
    PDF fetch, charts, site build, git push) is mocked, and DATA_DIR is
    redirected to a tmp dir so the manifest read after the chart step hits a
    seeded file rather than data/charts_manifest.json.
    """
    mock_monitor.return_value = {"diff": {"new_count": 0}, "summary": {}}
    mock_charts.return_value = {}

    # run() reads charts_manifest.json after the (mocked) chart step.
    (tmp_path / "charts_manifest.json").write_text(
        json.dumps({"charts": [], "total_records": 0, "total_employees": 0})
    )

    with patch("warn_publish.DATA_DIR", tmp_path):
        warn_publish.run(no_push=True)

    # Every stage ran, and push was skipped (no_push=True).
    assert mock_monitor.called
    assert mock_diff.called
    assert mock_history.called
    assert mock_charts.called
    assert mock_site.called
    assert not mock_push.called

def test_format_number():
    assert warn_publish._format_number(1234) == "1,234"
    assert warn_publish._format_number("invalid") == "invalid"


def test_strip_county():
    assert warn_publish._strip_county("Los Angeles County") == "Los Angeles"
    assert warn_publish._strip_county("Orleans Parish") == "Orleans"
    assert warn_publish._strip_county(None) == ""


def test_fmt_human_date():
    # Must match the client JS fmtDate ('Jul 4, 2026' — no leading zero on day).
    assert warn_publish._fmt_human_date("2026-01-01") == "Jan 1, 2026"
    assert warn_publish._fmt_human_date("2026-07-04") == "Jul 4, 2026"
    assert warn_publish._fmt_human_date("2025-12-31") == "Dec 31, 2025"
    assert warn_publish._fmt_human_date("garbage") == "garbage"


# Records spanning two calendar years, used to exercise the KPI date window.
_KPI_RECORDS = [
    {"company": "Alpha", "county": "Los Angeles County", "employees": 100,
     "notice_date": "2026-02-01", "effective_date": "2026-04-02"},  # lead 60d
    {"company": "Beta", "county": "Los Angeles County", "employees": 250,
     "notice_date": "2026-03-01", "effective_date": "2026-05-30"},  # lead 90d
    {"company": "Gamma", "county": "Orange County", "employees": 400,
     "notice_date": "2025-06-01", "effective_date": "2025-07-01"},  # prior year
]


def test_compute_kpis_current_year_window():
    """A notice-date window restricts every metric to that period and strips
    the county suffix so it matches the notices table."""
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2026-01-01", "2026-12-31")
    assert k["count"] == 2
    assert k["employees_total"] == 350
    assert k["largest_company"] == "Beta"
    assert k["largest_employees"] == "250"
    assert k["top_county"] == "Los Angeles"          # suffix stripped
    assert k["top_county_employees"] == "350"
    assert k["avg_lead_days"] == "75d"               # mean of 60 and 90


def test_compute_kpis_no_window_counts_everything():
    k = warn_publish._compute_kpis(_KPI_RECORDS)
    assert k["count"] == 3
    assert k["employees_total"] == 750
    assert k["largest_company"] == "Gamma"           # 400 is the largest overall
    assert k["top_county"] == "Orange"               # Orange 400 > LA 350


def test_compute_kpis_prior_year_window():
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2025-01-01", "2025-12-31")
    assert k["count"] == 1
    assert k["employees_total"] == 400
    assert k["top_county"] == "Orange"


def test_compute_kpis_empty_window_returns_defaults():
    k = warn_publish._compute_kpis(_KPI_RECORDS, "2030-01-01", "2030-12-31")
    assert k["count"] == 0
    assert k["employees_total"] == 0
    assert k["top_county"] == "N/A"
    assert k["largest_employees"] == "N/A"          # not "0" — matches client
    assert k["top_county_employees"] == "N/A"
    assert k["avg_lead_days"] == "N/A"


def test_compute_kpis_avg_lead_rounds_half_up():
    """Mean lead of exactly x.5 rounds up (int(x + 0.5)) to match the client's
    JS Math.round — Python's banker's round() would give 58 here."""
    recs = [
        {"company": "A", "county": "X", "employees": 10,
         "notice_date": "2026-01-01", "effective_date": "2026-02-28"},  # 58 days
        {"company": "B", "county": "X", "employees": 10,
         "notice_date": "2026-01-01", "effective_date": "2026-03-01"},  # 59 days
    ]
    assert warn_publish._compute_kpis(recs)["avg_lead_days"] == "59d"    # mean 58.5


def test_compute_kpis_largest_tiebreak_is_order_independent():
    """On an employee tie the latest notice wins, regardless of input order, so
    the server matches the client (which iterates the newest-first table)."""
    recs = [
        {"company": "Zeta Corp", "county": "X County", "employees": 500,
         "notice_date": "2026-02-01", "effective_date": "2026-03-01"},
        {"company": "Alpha Corp", "county": "X County", "employees": 500,
         "notice_date": "2026-05-01", "effective_date": "2026-06-01"},  # later
    ]
    assert warn_publish._compute_kpis(recs)["largest_company"] == "Alpha Corp"
    assert warn_publish._compute_kpis(
        list(reversed(recs))
    )["largest_company"] == "Alpha Corp"


def test_compute_kpis_top_county_tiebreak_is_order_independent():
    recs = [
        {"company": "A", "county": "Alameda County", "employees": 100,
         "notice_date": "2026-01-01", "effective_date": "2026-02-01"},
        {"company": "B", "county": "Butte County", "employees": 100,
         "notice_date": "2026-01-02", "effective_date": "2026-02-01"},
    ]
    assert warn_publish._compute_kpis(recs)["top_county"] == "Butte"
    assert warn_publish._compute_kpis(list(reversed(recs)))["top_county"] == "Butte"
