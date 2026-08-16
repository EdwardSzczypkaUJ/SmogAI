#!/usr/bin/env python3
"""Create a recoverable, secret-safe snapshot of the current Git working tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

SECRET_PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    "DigitalOcean token": re.compile(r"\bdop_v1_[A-Za-z0-9]{30,}"),
    "Langfuse secret": re.compile(r"\bsk-lf-[A-Za-z0-9_-]{16,}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
SENSITIVE_NAMES = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|.*(?:secret|credential|private[-_]?key|access[-_]?key|token).*)$"
)
ALLOWED_ENV_EXAMPLES = {
    ".env.example",
    ".env.server.example",
    ".env.server.local.example",
}
EXCLUDED_ANYWHERE_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules",
}
EXCLUDED_ROOT_PARTS = {
    "data", "logs", "backups", "snapshots", "object-store",
    "training-datasets", "mlruns", "tmp", "build", "dist", "models",
    "reports", "exports", "server-data", "server_data", "artifacts",
    "cache", "progress", "runs", "private",
}
BANNED_RUNTIME_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".joblib", ".parquet", ".feather",
    ".arrow", ".gz", ".zip", ".7z", ".tar", ".tgz", ".pkl", ".pickle",
    ".npy", ".npz", ".onnx", ".pt", ".pth", ".bin",
}
ALLOWED_SOURCE_SUFFIXES = {
    ".py", ".ps1", ".psm1", ".cmd", ".bat", ".sh", ".md", ".rst",
    ".txt", ".toml", ".yaml", ".yml", ".json", ".jsonl", ".ini",
    ".cfg", ".conf", ".example", ".sql", ".html", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".svg", ".tex", ".csv", ".geojson",
}
TRACKED_SOURCE_SUFFIXES = {".sha256", ".pdf", ".mako", ".xml"}
TRACKED_SPECIAL_FILENAMES = {".editorconfig"}
TRACKED_BINARY_ASSETS = {
    "examples/dashboard_snapshot.example.json.gz",
    "server/dashboard/resources/poland_dem_grid.json.gz",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".joblib",
    ".sqlite", ".sqlite3", ".db", ".parquet", ".woff", ".woff2", ".ico",
}


def _run(root: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(
        list(args), cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if check and process.returncode:
        raise RuntimeError(f"Command failed ({process.returncode}): {' '.join(args)}\n{process.stdout}")
    return process.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relative: str, *, tracked: bool = False) -> tuple[bool, str | None]:
    path = PurePosixPath(relative)
    if any(
        part in EXCLUDED_ANYWHERE_PARTS or part.startswith(".venv")
        for part in path.parts
    ):
        return True, "runtime/build/cache directory"
    if path.parts and path.parts[0] in EXCLUDED_ROOT_PARTS:
        return True, "runtime/build/cache root directory"
    if path.name.startswith(".env") and path.name not in ALLOWED_ENV_EXAMPLES:
        return True, "environment file"
    if SENSITIVE_NAMES.search(relative) and path.name not in ALLOWED_ENV_EXAMPLES:
        return True, "sensitive filename"
    if (
        path.suffix.lower() in BANNED_RUNTIME_SUFFIXES | {".pem", ".pfx", ".key"}
        and not (tracked and relative in TRACKED_BINARY_ASSETS)
    ):
        return True, "runtime artifact, archive, database or key file"
    if (
        not tracked
        and path.parts
        and (
            path.parts[0].startswith("_hf21_")
            or path.parts[0].startswith("SmogAI_HF21_Automation_v")
        )
    ):
        return True, "temporary extracted hotfix payload"
    return False, None


def _source_allowed(relative: str, *, tracked: bool = False) -> tuple[bool, str | None]:
    path = PurePosixPath(relative)
    if path.name == ".gitkeep":
        return True, None
    excluded, reason = _excluded(relative, tracked=tracked)
    if excluded:
        return False, reason
    if tracked and relative in TRACKED_BINARY_ASSETS:
        return True, None
    if tracked and path.name in TRACKED_SPECIAL_FILENAMES:
        return True, None
    if path.name in {
        "Dockerfile", "Procfile", "Makefile", "LICENSE", "NOTICE", "py.typed",
        ".gitignore", ".gitattributes", ".dockerignore",
    }:
        return True, None
    allowed_suffixes = ALLOWED_SOURCE_SUFFIXES | (TRACKED_SOURCE_SUFFIXES if tracked else set())
    if path.suffix.lower() not in allowed_suffixes:
        return False, "file type is not on the source/configuration allowlist"
    return True, None


def _candidates(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=False,
    )
    if output.returncode:
        raise RuntimeError(output.stderr.decode("utf-8", errors="replace"))
    return sorted({item.decode("utf-8", errors="strict") for item in output.stdout.split(b"\0") if item})


def _tracked(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached"], cwd=root,
        capture_output=True, check=False,
    )
    if output.returncode:
        raise RuntimeError(output.stderr.decode("utf-8", errors="replace"))
    return sorted(item.decode("utf-8", errors="strict") for item in output.stdout.split(b"\0") if item)


def _scan(path: Path, relative: str) -> list[dict[str, str]]:
    if path.suffix.lower() in BINARY_SUFFIXES or path.stat().st_size > 5 * 1024 * 1024:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return []
    findings = []
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append({"path": relative, "finding": label})
    return findings


def seal(root: Path, output: Path, label: str) -> dict[str, Any]:
    root = root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        raise RuntimeError(f"ProjectRoot is not a Git working tree: {root}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "release"
    base = f"SmogAI-{safe_label}-{stamp}"
    status = _run(root, "git", "status", "--short", "--untracked-files=all", check=False)
    head = _run(root, "git", "rev-parse", "HEAD")
    branch = _run(root, "git", "branch", "--show-current", check=False)

    tracked_violations = []
    for relative in _tracked(root):
        allowed, reason = _source_allowed(relative, tracked=True)
        if not allowed:
            tracked_violations.append({"path": relative, "reason": str(reason)})
    if tracked_violations:
        report = output / f"{base}-BLOCKED-git-runtime-files.json"
        report.write_text(
            json.dumps(tracked_violations, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        raise RuntimeError(
            "Git tracks runtime/data artifacts. No bundle or archive created. "
            f"Report: {report}"
        )

    included: list[str] = []
    excluded: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []
    tracked = set(_tracked(root))
    for relative in _candidates(root):
        allowed, reason = _source_allowed(relative, tracked=relative in tracked)
        source = root / Path(relative)
        if not allowed:
            excluded.append({"path": relative, "reason": str(reason)})
            continue
        if not source.is_file():
            continue
        included.append(relative)
        findings.extend(_scan(source, relative))
    if findings:
        report = output / f"{base}-BLOCKED-secrets.json"
        report.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
        raise RuntimeError(f"Potential secrets detected. No archive created. Report: {report}")

    bundle = output / f"{base}.bundle"
    _run(root, "git", "bundle", "create", str(bundle), "--all")
    _run(root, "git", "bundle", "verify", str(bundle))
    archive = output / f"{base}-working-tree.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for relative in included:
            target.write(root / Path(relative), arcname=f"SmogAI/{relative}")

    manifest = {
        "schema_version": "1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "label": label,
        "project_root": str(root),
        "git": {"head": head, "branch": branch, "status": status.splitlines()},
        "included_file_count": len(included),
        "excluded": excluded,
        "secret_scan": {"status": "passed", "patterns": sorted(SECRET_PATTERNS)},
        "artifacts": {
            "working_tree_zip": {"path": str(archive), "sha256": _sha256(archive), "bytes": archive.stat().st_size},
            "git_bundle": {"path": str(bundle), "sha256": _sha256(bundle), "bytes": bundle.stat().st_size},
        },
    }
    manifest_path = output / f"{base}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    checksums = output / f"{base}-SHA256SUMS.txt"
    checksums.write_text(
        f"{_sha256(archive)}  {archive.name}\n{_sha256(bundle)}  {bundle.name}\n{_sha256(manifest_path)}  {manifest_path.name}\n",
        encoding="ascii",
    )
    manifest["manifest_path"] = str(manifest_path)
    manifest["checksums_path"] = str(checksums)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--label", default="before-digitalocean")
    args = parser.parse_args()
    try:
        result = seal(args.project_root, args.output_root, args.label)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
