import pytest
import json
from pathlib import Path

@pytest.fixture
def mock_data_dir(tmp_path):
    """Create a temporary data directory for testing."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir

@pytest.fixture
def sample_warn_data():
    """Returns a sample list of WARN records for testing."""
    return [
        {
            "Notice Date": "2026-01-01",
            "Effective Date": "2026-02-01",
            "Company": "Test Company",
            "No. of Employees": 100,
            "County": "Test County",
            "City": "Test City",
            "Layoff Type": "Layoff",
            "Address": "123 Test St",
        },
        {
            "Notice Date": "2026-01-05",
            "Effective Date": "2026-03-01",
            "Company": "Another Co",
            "No. of Employees": 50,
            "County": "Another County",
            "City": "Another City",
            "Layoff Type": "Closure",
            "Address": "456 Side St",
        },
    ]

@pytest.fixture
def mock_env(monkeypatch):
    """Mock essential environment variables."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GMAIL_USER", "test@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test_pass")
    monkeypatch.setenv("NOTIFY_EMAIL", "notify@example.com")
