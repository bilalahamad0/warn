"""
warn_sources.base
-----------------
State-agnostic building blocks for multi-state WARN monitoring.

A ``Source`` wraps one jurisdiction's feed behind a uniform interface:

    fetch(force)  -> (changed, raw_path)     download the raw feed
    parse(path)   -> pd.DataFrame            raw file -> unified-schema rows
    run(...)      -> dict                    full monitor cycle for the state

``run`` reuses the battle-tested engine in ``warn_monitor`` (change detection
against cumulative notified/amended ledgers, snapshot rotation, cumulative
union with amendment eviction) — parameterised with this state's paths, so
every state gets the same oscillation-proof alerting that California has.

Storage layout: each state owns ``data/states/<code>/`` (latest, snapshot,
cumulative, meta, ledgers, changelog, raw download). California is
grandfathered at the historical top-level ``data/*.json`` paths so the
existing dashboard, history merge, and cron pipelines keep working unchanged.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

import warn_monitor

log = logging.getLogger("warn_sources")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATES_DIR = DATA_DIR / "states"

# The unified record schema shared by every state. ``state`` is the 2-letter
# postal code; the remaining fields mirror the original CA schema. Sources that
# lack a field publish it as empty/None — never fabricated from another field
# (several states publish no notice date, and semantics differ; see
# EXPANSION_RESEARCH.md §5).
UNIFIED_FIELDS = [
    "state",
    "company",
    "notice_date",
    "effective_date",
    "employees",
    "layoff_type",
    "county",
    "city",
    "address",
    "industry",
]

# Fields that stay None when missing; the rest default to "".
_NULLABLE_FIELDS = {"notice_date", "effective_date", "employees"}


@dataclass(frozen=True)
class StatePaths:
    """Where one state's pipeline files live."""

    root: Path
    latest: Path
    snapshot: Path
    cumulative: Path
    meta: Path
    notified: Path
    amended: Path
    changelog: Path
    raw: Path

    @classmethod
    def for_state(cls, code: str, data_dir: Optional[Path] = None) -> "StatePaths":
        """Standard per-state layout under ``data/states/<code>/``."""
        base = data_dir if data_dir is not None else DATA_DIR
        root = base / "states" / code.lower()
        return cls(
            root=root,
            latest=root / "warn_latest.json",
            snapshot=root / "warn_snapshot.json",
            cumulative=root / "warn_cumulative.json",
            meta=root / "meta.json",
            notified=root / "notified_keys.json",
            amended=root / "amended_keys.json",
            changelog=root / "changelog.jsonl",
            raw=root / "raw_download",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class Source(ABC):
    """One jurisdiction's WARN feed behind the uniform pipeline interface."""

    code: str = "xx"          # 2-letter postal code, lowercase
    name: str = "Unknown"     # display name
    agency: str = ""          # publishing agency
    source_url: str = ""      # canonical public URL of the feed
    cadence: str = "daily"    # informational: how often the state updates
    enabled: bool = True

    def __init__(self, data_dir: Optional[Path] = None):
        self.paths = self.make_paths(data_dir)

    def make_paths(self, data_dir: Optional[Path] = None) -> StatePaths:
        return StatePaths.for_state(self.code, data_dir)

    # -- per-state behavior -------------------------------------------------

    @abstractmethod
    def fetch(self, force: bool = False) -> tuple:
        """Download the raw feed. Returns (changed: bool, raw_path: str)."""

    @abstractmethod
    def parse(self, raw_path) -> pd.DataFrame:
        """Parse the raw file into unified-schema rows."""

    # -- shared engine ------------------------------------------------------

    def unify(self, df: pd.DataFrame) -> pd.DataFrame:
        """Stamp the state code and guarantee every unified column exists."""
        df = df.copy()
        df["state"] = self.code.upper()
        for col in UNIFIED_FIELDS:
            if col not in df.columns:
                df[col] = None if col in _NULLABLE_FIELDS else ""
        extras = [c for c in df.columns if c not in UNIFIED_FIELDS]
        return df[UNIFIED_FIELDS + extras]

    def run(self, dry_run: bool = False, force: bool = False) -> dict:
        """Full monitor cycle for this state; mirrors ``warn_monitor.run``."""
        log.info(f"[{self.code.upper()}] {self.name} — monitor run")
        self.paths.ensure()

        changed, raw_path = self.fetch(force=force)
        df = self.unify(self.parse(raw_path))

        diff = warn_monitor.detect_changes(
            df,
            dry_run=dry_run,
            latest_file=self.paths.latest,
            notified_file=self.paths.notified,
            amended_file=self.paths.amended,
        )
        warn_monitor._log_change(
            diff, dry_run=dry_run, changelog_file=self.paths.changelog
        )

        summary = warn_monitor.save_latest(
            df,
            dry_run=dry_run,
            latest_file=self.paths.latest,
            snapshot_file=self.paths.snapshot,
            source_url=self.source_url,
        )
        cumulative = warn_monitor.update_cumulative(
            summary["records"],
            dry_run=dry_run,
            superseded=diff.get("amend_superseded"),
            cumulative_file=self.paths.cumulative,
            amended_file=self.paths.amended,
            source_url=self.source_url,
        )

        return {
            "state": self.code.upper(),
            "file_changed": changed,
            "diff": diff,
            "summary": {k: v for k, v in summary.items() if k != "records"},
            "cumulative": {k: v for k, v in cumulative.items() if k != "records"},
        }

    def record_alerted(self, diff: dict) -> None:
        """Persist alert ledgers after a notification actually sent."""
        warn_monitor.record_notified_keys(
            diff.get("new_keys", []), self.paths.notified
        )
        warn_monitor.record_amended_keys(
            diff.get("amendment_keys", []), self.paths.amended
        )
