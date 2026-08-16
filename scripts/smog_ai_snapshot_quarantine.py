from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DATASET_PATTERN = re.compile(r'"dataset_id"\s*:\s*"([^"]+)"')


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return dict(value) if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def directory_size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def protected_dataset_ids(runtime: Path) -> tuple[set[str], dict[str, list[str]]]:
    root = runtime / "training-datasets"
    reasons: dict[str, list[str]] = {}

    def protect(dataset_id: str | None, reason: str) -> None:
        if not dataset_id:
            return
        reasons.setdefault(str(dataset_id), [])
        if reason not in reasons[str(dataset_id)]:
            reasons[str(dataset_id)].append(reason)

    for pointer in root.rglob("latest.json") if root.exists() else []:
        if "_quarantine" not in pointer.parts:
            protect(load_json(pointer).get("dataset_id"), f"pointer:{pointer}")

    comparison = load_json(runtime / "reports" / "mlflow" / "model-comparison.json")
    active_versions: set[str] = set()
    for model in comparison.get("models") or []:
        if not isinstance(model, dict) or not model.get("active"):
            continue
        version = model.get("version")
        if version:
            active_versions.add(str(version))
        metrics = dict(model.get("metrics") or {})
        protect(metrics.get("dataset_id"), f"active-model:{version}")

    runs = runtime / "logs" / "automation" / "runs"
    if active_versions and runs.exists():
        for run_file in runs.glob("*/run.json"):
            try:
                text = run_file.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            matched = [version for version in active_versions if version in text]
            if not matched:
                continue
            for dataset_id in sorted(set(DATASET_PATTERN.findall(text))):
                protect(dataset_id, f"active-model-run:{run_file.parent.name}")

    return set(reasons), reasons


def build_plan(runtime: Path, minimum_age_hours: float) -> dict[str, Any]:
    training = runtime / "training-datasets"
    protected, reasons = protected_dataset_ids(runtime)
    cutoff = datetime.now(UTC).timestamp() - minimum_age_hours * 3600
    candidates: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    for profile in ("quick", "full"):
        profile_root = training / profile
        if not profile_root.exists():
            continue
        for directory in profile_root.glob("dataset-*"):
            if not directory.is_dir() or directory.is_symlink():
                continue
            dataset_id = directory.name.removeprefix("dataset-")
            database = directory / "smog.db"
            manifest = directory / "manifest.json"
            row = {
                "dataset_id": dataset_id,
                "profile": profile,
                "path": str(directory),
                "size_bytes": directory_size(directory),
                "modified_at": datetime.fromtimestamp(
                    directory.stat().st_mtime, UTC
                ).isoformat(),
                "has_database": database.exists(),
                "has_manifest": manifest.exists(),
                "protected": dataset_id in protected,
                "protection_reasons": reasons.get(dataset_id, []),
                "old_enough": directory.stat().st_mtime < cutoff,
            }
            observed.append(row)
            if (
                row["has_database"]
                and not row["has_manifest"]
                and not row["protected"]
                and row["old_enough"]
            ):
                candidates.append(row)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_root": str(runtime),
        "minimum_age_hours": minimum_age_hours,
        "protected_dataset_ids": sorted(protected),
        "protection_reasons": reasons,
        "observed": observed,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(row["size_bytes"]) for row in candidates),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or quarantine unreferenced manifest-less training databases"
    )
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--minimum-age-hours", type=float, default=1.0)
    parser.add_argument("--quarantine", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    plan = build_plan(runtime, max(0.0, args.minimum_age_hours))
    moved: list[dict[str, Any]] = []
    if args.quarantine:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for row in plan["candidates"]:
            source = Path(row["path"]).resolve()
            expected_parent = runtime / "training-datasets" / str(row["profile"])
            if source.parent.resolve() != expected_parent.resolve():
                raise RuntimeError(f"Unsafe quarantine source: {source}")
            destination_root = runtime / "training-datasets" / "_quarantine" / str(row["profile"])
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / f"{source.name}-{stamp}"
            if destination.exists():
                raise FileExistsError(destination)
            source.replace(destination)
            moved.append({**row, "quarantine_path": str(destination)})
    report = {
        **plan,
        "mode": "quarantine" if args.quarantine else "plan",
        "moved": moved,
        "moved_count": len(moved),
        "moved_bytes": sum(int(row["size_bytes"]) for row in moved),
        "source_deleted": False,
        "purged": False,
    }
    report_root = runtime / "reports" / "cleanup"
    report_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_root / f"snapshot-quarantine-{stamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "mode": report["mode"],
        "candidate_count": report["candidate_count"],
        "candidate_gib": round(report["candidate_bytes"] / 1024**3, 3),
        "moved_count": report["moved_count"],
        "moved_gib": round(report["moved_bytes"] / 1024**3, 3),
        "protected_dataset_ids": report["protected_dataset_ids"],
        "report_path": str(report_path),
        "candidates": [
            {key: row[key] for key in ("dataset_id", "profile", "path", "size_bytes")}
            for row in report["candidates"]
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
