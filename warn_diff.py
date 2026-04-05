"""
warn_diff.py
------------
Detailed change detection:
  1. Compares live data vs the saved snapshot (new/removed rows, employee count delta).
  2. Compares local file.xlsx vs the version committed in the git repo.
  3. Outputs a human-readable Markdown diff report to data/diff_report.md.

Usage:
    python3 warn_diff.py
"""

import hashlib
import json
import subprocess
from typing import Optional
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LATEST_FILE = DATA_DIR / "warn_latest.json"
SNAPSHOT_FILE = DATA_DIR / "warn_snapshot.json"
CHANGELOG_FILE = DATA_DIR / "changelog.jsonl"
DIFF_REPORT = DATA_DIR / "diff_report.md"
LOCAL_XLSX = BASE_DIR / "file.xlsx"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_show_hash(relative_path: str) -> Optional[str]:
    """Return MD5 of the file as committed in the git HEAD."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{relative_path}"],
            cwd=str(BASE_DIR),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        content = result.stdout
        return hashlib.md5(content).hexdigest()
    except Exception:
        return None


def _git_log_summary() -> str:
    """Return the last 5 git commit messages."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "(git log unavailable)"


def _load_json_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("records", []) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Data diff
# ---------------------------------------------------------------------------

def diff_data() -> dict:
    """Compare LATEST vs SNAPSHOT and return structured diff."""
    latest = _load_json_records(LATEST_FILE)
    snapshot = _load_json_records(SNAPSHOT_FILE)

    def key(r):
        return (
            str(r.get("company", "")).strip().lower(),
            str(r.get("effective_date", "")),
            str(r.get("employees", "")),
        )

    latest_keys = {key(r): r for r in latest}
    snapshot_keys = {key(r): r for r in snapshot}

    added = [r for k, r in latest_keys.items() if k not in snapshot_keys]
    removed = [r for k, r in snapshot_keys.items() if k not in latest_keys]

    # Employee count delta
    latest_total = sum(r.get("employees", 0) for r in latest)
    snapshot_total = sum(r.get("employees", 0) for r in snapshot)

    return {
        "latest_count": len(latest),
        "snapshot_count": len(snapshot),
        "added_records": len(added),
        "removed_records": len(removed),
        "added_entries": sorted(added, key=lambda r: r.get("employees", 0), reverse=True)[:20],
        "removed_entries": sorted(removed, key=lambda r: r.get("employees", 0), reverse=True)[:20],
        "employees_latest": latest_total,
        "employees_snapshot": snapshot_total,
        "employees_delta": latest_total - snapshot_total,
    }


# ---------------------------------------------------------------------------
# File diff vs git
# ---------------------------------------------------------------------------

def diff_file_vs_git() -> dict:
    local_hash = _file_hash(LOCAL_XLSX)
    committed_hash = _git_show_hash("file.xlsx")
    changed = (local_hash != committed_hash) if (local_hash and committed_hash) else None

    # git status summary
    try:
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=10,
        )
        git_status = status_result.stdout.strip()
    except Exception:
        git_status = "(unavailable)"

    return {
        "local_xlsx_hash": local_hash,
        "committed_xlsx_hash": committed_hash,
        "file_changed_vs_git": changed,
        "git_status": git_status,
        "git_log": _git_log_summary(),
    }


# ---------------------------------------------------------------------------
# Changelog summary
# ---------------------------------------------------------------------------

def changelog_summary() -> list[dict]:
    if not CHANGELOG_FILE.exists():
        return []
    entries = []
    for line in CHANGELOG_FILE.read_text().strip().splitlines()[-10:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def generate_report() -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    data_diff = diff_data()
    file_diff = diff_file_vs_git()
    changelog = changelog_summary()

    lines = [
        "# WARN Layoff Monitor — Diff Report",
        f"\n**Generated:** {now}\n",
        "---",
        "## 📊 Data Comparison (Latest vs Snapshot)",
        "",
        f"| Metric | Snapshot | Latest | Δ |",
        f"|--------|----------|--------|---|",
        f"| Total records | {data_diff['snapshot_count']:,} | {data_diff['latest_count']:,} "
        f"| {data_diff['added_records']:+}/{data_diff['removed_records']:+} |",
        f"| Total employees | {data_diff['employees_snapshot']:,} | {data_diff['employees_latest']:,} "
        f"| {data_diff['employees_delta']:+,} |",
        "",
    ]

    if data_diff["added_entries"]:
        lines += [
            f"### ✅ New Entries ({data_diff['added_records']} records)",
            "",
            "| Company | Employees | Effective Date | County |",
            "|---------|-----------|----------------|--------|",
        ]
        for r in data_diff["added_entries"]:
            lines.append(
                f"| {r.get('company','?')} | {r.get('employees','?'):,} | "
                f"{r.get('effective_date','?')} | {r.get('county','?')} |"
            )
        lines.append("")
    else:
        lines.append("### ✅ No new entries.\n")

    if data_diff["removed_entries"]:
        lines += [
            f"### ❌ Removed Entries ({data_diff['removed_records']} records)",
            "",
            "| Company | Employees | Effective Date |",
            "|---------|-----------|----------------|",
        ]
        for r in data_diff["removed_entries"]:
            lines.append(
                f"| {r.get('company','?')} | {r.get('employees','?'):,} | "
                f"{r.get('effective_date','?')} |"
            )
        lines.append("")

    lines += [
        "---",
        "## 📁 File vs Git Comparison",
        "",
        f"- **Local `file.xlsx` hash:** `{file_diff['local_xlsx_hash'] or 'N/A'}`",
        f"- **Committed hash:**          `{file_diff['committed_xlsx_hash'] or 'N/A'}`",
    ]

    if file_diff["file_changed_vs_git"] is True:
        lines.append("- 🔴 **Local file differs from committed version**")
    elif file_diff["file_changed_vs_git"] is False:
        lines.append("- ✅ Local file matches committed version")
    else:
        lines.append("- ⚠️  Cannot compare (file not yet committed to git)")

    if file_diff["git_status"]:
        lines += ["", "**Git status:**", f"```\n{file_diff['git_status']}\n```"]

    lines += [
        "",
        "**Recent commits:**",
        f"```\n{file_diff['git_log']}\n```",
        "",
        "---",
        "## 📋 Recent Changelog (last 10 runs)",
        "",
    ]

    if changelog:
        for entry in reversed(changelog):
            ts = entry.get("timestamp", "?")
            new_c = entry.get("new_count", 0)
            rem_c = entry.get("removed_count", 0)
            emp = entry.get("total_employees_new", 0)
            lines.append(f"- `{ts}` — +{new_c} added, -{rem_c} removed, {emp:,} employees (new)")
    else:
        lines.append("*No changelog entries yet.*")

    report = "\n".join(lines)
    DIFF_REPORT.write_text(report)
    return report


if __name__ == "__main__":
    report = generate_report()
    print(report)
