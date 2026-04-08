import pytest
import json
from unittest.mock import MagicMock, patch
import warn_publish

@patch("warn_publish.warn_monitor.run")
@patch("warn_publish.warn_charts.run")
@patch("warn_publish.build_site")
@patch("warn_publish.git_commit_push")
def test_run_full_pipeline(mock_push, mock_site, mock_charts, mock_monitor):
    # Mock return values
    mock_monitor.return_value = {"diff": {"new_count": 0}, "summary": {}}
    mock_charts.return_value = {}
    
    # Run with no_push=True
    warn_publish.run(no_push=True)
    
    # Verify monitor and charts were called
    assert mock_monitor.called
    assert mock_charts.called
    # verify push was NOT called
    assert not mock_push.called

def test_format_number():
    assert warn_publish._format_number(1234) == "1,234"
    assert warn_publish._format_number("invalid") == "invalid"
