from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.seal_current_release import _source_allowed

ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    ]


def test_every_tracked_repository_file_is_accepted_by_release_seal() -> None:
    rejected = {
        relative: reason
        for relative in _tracked_files()
        if not (allowed := _source_allowed(relative, tracked=True))[0]
        for reason in [allowed[1]]
    }

    assert rejected == {}


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        "data/live.sqlite3",
        "models/current.joblib",
        "reports/private.json",
        "server/dashboard/resources/unapproved.json.gz",
        "_hf21_unpacked/apply.py",
        ".venv-ci/lib/python3.12/site-packages/example.py",
    ],
)
def test_untracked_runtime_and_sensitive_files_remain_excluded(relative: str) -> None:
    allowed, reason = _source_allowed(relative, tracked=False)

    assert allowed is False
    assert reason


@pytest.mark.parametrize(
    "relative",
    [
        "data/live.sqlite3",
        "models/current.joblib",
        ".env",
        "private-key.pem",
        "server/dashboard/resources/unapproved.json.gz",
    ],
)
def test_tracking_does_not_whitelist_runtime_or_secret_files(relative: str) -> None:
    allowed, reason = _source_allowed(relative, tracked=True)

    assert allowed is False
    assert reason
