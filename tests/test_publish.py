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
