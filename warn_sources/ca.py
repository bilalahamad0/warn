"""
warn_sources.ca
---------------
California — the founding source, backed by the EDD WARN XLSX.

Fetch/parse delegate to the proven implementations in ``warn_monitor``; only
the plumbing moved behind the ``Source`` interface. California keeps its
historical top-level ``data/*.json`` paths (grandfathered) so the existing
dashboard, diff report, history merge, notifier, and cron pipelines are
untouched — new states get the standard ``data/states/<code>/`` layout.
"""

from pathlib import Path
from typing import Optional

import pandas as pd

import warn_monitor
from .base import BASE_DIR, DATA_DIR, Source, StatePaths


class CaliforniaEDD(Source):
    code = "ca"
    name = "California"
    agency = "California Employment Development Department"
    source_url = warn_monitor.WARN_XLSX_URL
    cadence = "twice-daily"

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        d = data_dir if data_dir is not None else DATA_DIR
        # Legacy layout: CA predates the per-state tree. The raw XLSX also
        # keeps its historical spot next to the scripts (committed as file.xlsx).
        raw = BASE_DIR / "file.xlsx" if data_dir is None else d / "file.xlsx"
        return StatePaths(
            root=d,
            latest=d / "warn_latest.json",
            snapshot=d / "warn_snapshot.json",
            cumulative=d / "warn_cumulative.json",
            meta=d / "meta.json",
            notified=d / "notified_keys.json",
            amended=d / "amended_keys.json",
            changelog=d / "changelog.jsonl",
            raw=raw,
        )

    def fetch(self, force: bool = False) -> tuple:
        return warn_monitor.download_xlsx(
            force=force,
            url=self.source_url,
            meta_file=self.paths.meta,
            local_path=self.paths.raw,
        )

    def parse(self, raw_path) -> pd.DataFrame:
        return warn_monitor.parse_warn_xlsx(str(raw_path))
