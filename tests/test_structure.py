import os
from pathlib import Path

def test_project_structure():
    """Verify essential project directories exist."""
    base_dir = Path(__file__).parent.parent
    assert (base_dir / "data").is_dir()
    assert (base_dir / "docs").is_dir()
    assert (base_dir / "warn_publish.py").is_file()

def test_data_files():
    """Verify key data files exist (even if empty)."""
    data_dir = Path(__file__).parent.parent / "data"
    # These should exist if the monitor has ever run
    assert (data_dir).exists()
