from __future__ import annotations

# HF21_CLEANUP_READONLY_SNAPSHOT_V1

import argparse
import html
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import psutil
except ImportError:  # pragma: no cover - installer verifies the dependency
    psutil = None


TERMINAL = {"success", "failed", "cancelled", "canceled"}
DISPLAY_TIMEZONE = ZoneInfo("Europe/Warsaw")

DEFAULT_CLEANUP_POLICY = {
    "training_quick": 2,
    "training_full": 3,
    "dashboard_snapshots": 5,
    "forecast_publications": 10,
    "map_surface_sets": 5,
    "automation_runs": 30,
    "progress_days": 30,
    "incomplete_snapshot_hours": 24,
}


def now() -> str:
    """Operational timestamp in Polish civil time with DST-aware offset."""

    return datetime.now(DISPLAY_TIMEZONE).isoformat()


def display_timestamp(value: Any) -> str | None:
    """Normalize an ISO timestamp to Europe/Warsaw for user-facing reports."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
        return parsed.astimezone(DISPLAY_TIMEZONE).isoformat()
    except (TypeError, ValueError):
        return str(value)


def process_is_alive(pid: Any) -> bool:
    """Return whether a recorded child PID still exists without raising."""

    try:
        selected = int(pid)
        if selected <= 0:
            return False
        if psutil is not None:
            return bool(psutil.pid_exists(selected))
        if os.name == "nt":
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {selected}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return str(selected) in probe.stdout
        os.kill(selected, 0)
        return True
    except Exception:
        return False


def nested_completed_models(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Read live or final completed-model lists from a progress snapshot."""

    detail = progress.get("detail") or {}
    if not isinstance(detail, dict):
        return []
    rows = detail.get("completed_models")
    if rows is None and isinstance(detail.get("details"), dict):
        rows = detail["details"].get("completed_models")
    return [dict(item) for item in (rows or []) if isinstance(item, dict)]


class ResourceSampler:
    """Periodic process/system resource telemetry with rate calculations."""

    def __init__(self, output: Path, interval_seconds: float = 5.0):
        self.output = output
        self.interval = max(0.0, float(interval_seconds))
        self.last_sample_at = 0.0
        self.last_counters: dict[str, float] | None = None
        self.samples: list[dict[str, Any]] = []
        self.error: str | None = None

    def sample(self, pid: int, stage: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.interval <= 0 or psutil is None:
            if psutil is None:
                self.error = "psutil is not installed"
            return None
        current = time.time()
        if not force and current - self.last_sample_at < self.interval:
            return None
        try:
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
            process_cpu = 0.0
            process_ram = 0
            process_read = 0
            process_write = 0
            for process in processes:
                try:
                    process_cpu += float(process.cpu_percent(interval=None))
                    process_ram += int(process.memory_info().rss)
                    io = process.io_counters()
                    process_read += int(io.read_bytes)
                    process_write += int(io.write_bytes)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            memory = psutil.virtual_memory()
            disk = psutil.disk_io_counters()
            network = psutil.net_io_counters()
            counters = {
                "time": current,
                "disk_read": float(disk.read_bytes if disk else 0),
                "disk_write": float(disk.write_bytes if disk else 0),
                "network_received": float(network.bytes_recv if network else 0),
                "network_sent": float(network.bytes_sent if network else 0),
            }
            rates = {key: 0.0 for key in (
                "disk_read_bps", "disk_write_bps", "network_received_bps", "network_sent_bps"
            )}
            if self.last_counters is not None:
                seconds = max(0.001, current - self.last_counters["time"])
                rates = {
                    "disk_read_bps": max(0.0, counters["disk_read"] - self.last_counters["disk_read"]) / seconds,
                    "disk_write_bps": max(0.0, counters["disk_write"] - self.last_counters["disk_write"]) / seconds,
                    "network_received_bps": max(0.0, counters["network_received"] - self.last_counters["network_received"]) / seconds,
                    "network_sent_bps": max(0.0, counters["network_sent"] - self.last_counters["network_sent"]) / seconds,
                }
            row = {
                "timestamp": now(),
                "stage": stage,
                "pid": pid,
                "process_count": len(processes),
                "process_cpu_percent": round(process_cpu, 2),
                "system_cpu_percent": round(float(psutil.cpu_percent(interval=None)), 2),
                "process_ram_bytes": process_ram,
                "system_ram_used_percent": round(float(memory.percent), 2),
                "system_ram_available_bytes": int(memory.available),
                "process_read_bytes": process_read,
                "process_write_bytes": process_write,
                **{key: round(value, 2) for key, value in rates.items()},
            }
            self.output.parent.mkdir(parents=True, exist_ok=True)
            with self.output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.samples.append(row)
            self.samples = self.samples[-720:]
            self.last_counters = counters
            self.last_sample_at = current
            return row
        except Exception as exc:
            self.error = str(exc)
            return None

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"sample_count": 0, "error": self.error}
        numeric = (
            "process_cpu_percent", "system_cpu_percent", "process_ram_bytes",
            "system_ram_used_percent", "system_ram_available_bytes",
            "disk_read_bps", "disk_write_bps", "network_received_bps",
            "network_sent_bps",
        )
        return {
            "sample_count": len(self.samples),
            "interval_seconds": self.interval,
            "average": {
                key: round(sum(float(row[key]) for row in self.samples) / len(self.samples), 2)
                for key in numeric
            },
            "maximum": {
                key: round(max(float(row[key]) for row in self.samples), 2)
                for key in numeric
            },
            "telemetry_path": str(self.output),
            "error": self.error,
        }


def atomic_json(path: Path, value: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # Na Windows monitor Streamlit, Defender albo indeksator mogą na moment
    # otworzyć run.json bez prawa do usunięcia/zmiany nazwy. Plik tymczasowy jest
    # już kompletny, więc ponawiamy wyłącznie atomową podmianę — bez utraty stanu.
    last_error: OSError | None = None
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(0.1 * (2 ** attempt), 2.0))
    # A dashboard, Defender or indexer must never stop training because it has
    # briefly locked a telemetry pointer.  The complete run-specific state is
    # retained under a unique emergency filename and a later save retries.
    emergency = path.with_name(
        f"{path.stem}.pending-{os.getpid()}-{int(time.time())}{path.suffix}"
    )
    try:
        os.replace(tmp, emergency)
    except OSError:
        emergency = tmp
    print(
        f"WARNING: telemetry pointer locked: {path}; state retained: {emergency}; "
        f"primary work continues ({last_error})"
    )
    return False


def load_env(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Brak pliku środowiska: {path}")
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def path_size(path: Path) -> int:
    """Return size without following directory symlinks."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        if not path.is_dir():
            return 0
        total = 0
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _make_tree_writable(path: Path) -> None:
    """Clear Windows read-only bits before deleting immutable snapshots."""
    paths: list[Path] = [path]
    if path.is_dir():
        for root_path, directory_names, file_names in os.walk(path, topdown=False):
            current_root = Path(root_path)
            paths.extend(current_root / name for name in file_names)
            paths.extend(current_root / name for name in directory_names)
    for item in paths:
        try:
            item.chmod(item.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass


def _remove_runtime_directory(path: Path) -> None:
    """Remove a retained directory with read-only recovery and short retries."""
    last_error: OSError | None = None

    def retry_readonly(function: Any, failed_path: str, _error: Any) -> None:
        selected = Path(failed_path)
        try:
            selected.chmod(selected.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
        function(failed_path)

    for attempt in range(3):
        try:
            _make_tree_writable(path)
            shutil.rmtree(path, onerror=retry_readonly)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _json_object_key(path: Path, field: str) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        value = payload.get(field)
        return str(value).replace("/", os.sep) if value else None
    except (OSError, ValueError, TypeError):
        return None


def cleanup_runtime(
    runtime_root: Path,
    *,
    apply: bool = False,
    policy: dict[str, int] | None = None,
    current_run_id: str | None = None,
) -> dict[str, Any]:
    """Plan or apply bounded retention below one explicit runtime root."""
    runtime = runtime_root.resolve()
    selected = {**DEFAULT_CLEANUP_POLICY, **(policy or {})}
    if any(int(value) < 0 for value in selected.values()):
        raise ValueError("Wartości retencji nie mogą być ujemne.")
    candidates: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    protected: list[str] = []

    def collect_dataset_ids(value: Any, result: set[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"dataset_id", "base_dataset_id"} and isinstance(item, str):
                    selected_id = item.strip()
                    if selected_id.startswith("layered:"):
                        selected_id = selected_id[len("layered:") :].split("+", 1)[0]
                    if selected_id:
                        result.add(selected_id)
                collect_dataset_ids(item, result)
        elif isinstance(value, list):
            for item in value:
                collect_dataset_ids(item, result)

    def protected_training_ids(profile: str) -> set[str]:
        result: set[str] = set()
        pointer = runtime / "training-datasets" / profile / "latest.json"
        try:
            collect_dataset_ids(json.loads(pointer.read_text(encoding="utf-8-sig")), result)
        except (OSError, ValueError, TypeError):
            pass
        protection = runtime / "training-datasets" / "_compaction" / profile / "protection.json"
        try:
            payload = json.loads(protection.read_text(encoding="utf-8-sig"))
            result.update(str(item) for item in payload.get("protected_dataset_ids") or [])
        except (OSError, ValueError, TypeError):
            pass
        database = runtime / "data" / "smog.db"
        try:
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT metrics_json FROM model_versions WHERE active=1"
                ).fetchall()
                for (raw_metrics,) in rows:
                    if isinstance(raw_metrics, str):
                        collect_dataset_ids(json.loads(raw_metrics), result)
                    elif isinstance(raw_metrics, (dict, list)):
                        collect_dataset_ids(raw_metrics, result)
            finally:
                connection.close()
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            pass
        return result

    def safe_target(path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
            return path != runtime and runtime in resolved.parents and not path.is_symlink()
        except OSError:
            return False

    def add(category: str, path: Path, *, archive_manifest: Path | None = None) -> None:
        if not safe_target(path) or not path.exists():
            return
        resolved = path.resolve(strict=False)
        # A parent directory already scheduled for deletion includes this
        # child. Avoid a second deletion attempt and a misleading error.
        for existing in candidates:
            existing_path = Path(existing["path"]).resolve(strict=False)
            if existing_path == resolved or existing_path in resolved.parents:
                return
        row: dict[str, Any] = {
            "category": category,
            "path": str(path),
            "size_bytes": path_size(path),
        }
        if archive_manifest:
            row["manifest_archive"] = str(archive_manifest)
        candidates.append(row)

    def old_items(paths: list[Path], keep: int, protected_paths: set[Path] | None = None) -> list[Path]:
        protected_resolved = {p.resolve(strict=False) for p in (protected_paths or set())}
        ordered = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
        kept = 0
        result: list[Path] = []
        for item in ordered:
            if item.resolve(strict=False) in protected_resolved:
                protected.append(str(item))
                continue
            if kept < keep:
                kept += 1
            else:
                result.append(item)
        return result

    # Immutable training databases. Their provenance manifest is archived first.
    incomplete_cutoff = time.time() - int(selected["incomplete_snapshot_hours"]) * 3600
    for profile, keep_key in (("quick", "training_quick"), ("full", "training_full")):
        root = runtime / "training-datasets" / profile
        if root.is_dir():
            directories = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
            datasets = [
                p for p in directories
                if (p / "smog.db").exists() and (p / "manifest.json").exists()
            ]
            protected_ids = protected_training_ids(profile)
            protected_paths: set[Path] = set()
            for dataset in datasets:
                try:
                    dataset_id = str(
                        json.loads((dataset / "manifest.json").read_text(encoding="utf-8-sig"))
                        .get("dataset_id")
                        or ""
                    )
                    if dataset_id in protected_ids:
                        protected_paths.add(dataset)
                except (OSError, ValueError, TypeError):
                    pass
            for old in old_items(datasets, int(selected[keep_key]), protected_paths):
                archive = runtime / "training-datasets" / "manifests" / profile / f"{old.name}.json"
                add(f"training_dataset_{profile}", old, archive_manifest=archive)
            for incomplete in directories:
                if incomplete in datasets:
                    continue
                try:
                    if incomplete.stat().st_mtime < incomplete_cutoff:
                        add(f"incomplete_training_snapshot_{profile}", incomplete)
                except OSError:
                    pass

    snapshots = runtime / "snapshots"
    if snapshots.is_dir():
        files = [p for p in snapshots.glob("dashboard_snapshot_*.json.gz") if p.is_file() and not p.is_symlink()]
        for old in old_items(files, int(selected["dashboard_snapshots"])):
            add("dashboard_snapshot", old)
            sidecar = old.with_suffix(old.suffix + ".metadata.json")
            if sidecar.exists():
                add("dashboard_snapshot_metadata", sidecar)

    store = runtime / "object-store"
    forecast_pointer = store / "forecasts" / "latest.json"
    forecast_key = _json_object_key(forecast_pointer, "object_key")
    forecast_protected = {store / forecast_key} if forecast_key else set()
    forecast_root = store / "forecasts" / "runs"
    if forecast_root.is_dir():
        files = [p for p in forecast_root.glob("publication=*.json.gz") if p.is_file() and not p.is_symlink()]
        for old in old_items(files, int(selected["forecast_publications"]), forecast_protected):
            add("forecast_publication", old)

    map_pointer = store / "maps" / "latest.json"
    manifest_key = _json_object_key(map_pointer, "manifest_key")
    map_protected: set[Path] = set()
    if manifest_key:
        manifest_path = store / manifest_key
        map_protected.add(manifest_path.parent)
    map_root = store / "maps" / "runs"
    if map_root.is_dir():
        dirs = [p for p in map_root.glob("surface-set=*") if p.is_dir() and not p.is_symlink()]
        for old in old_items(dirs, int(selected["map_surface_sets"]), map_protected):
            add("map_surface_set", old)

    # Serving v2: one immutable release contains only compressed map payloads
    # and small metadata.  Protect the release selected by the atomic pointer.
    serving_pointer = store / "serving" / "latest.json"
    serving_manifest_key = _json_object_key(serving_pointer, "manifest_key")
    serving_protected: set[Path] = set()
    if serving_manifest_key:
        serving_protected.add((store / serving_manifest_key).parent)
    serving_root = store / "serving" / "releases"
    if serving_root.is_dir():
        releases = [
            p for p in serving_root.glob("release=*")
            if p.is_dir() and not p.is_symlink()
        ]
        for old in old_items(
            releases,
            int(selected["map_surface_sets"]),
            serving_protected,
        ):
            add("serving_release", old)

    run_roots = (
        (runtime / "logs" / "automation" / "runs", "automation_log_run"),
        (runtime / "reports" / "automation", "automation_report"),
    )
    for root, category in run_roots:
        if not root.is_dir():
            continue
        dirs = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        run_protected = {root / current_run_id} if current_run_id else set()
        pointer = root.parent / "current.json" if category == "automation_log_run" else root / "latest.json"
        try:
            pointer_payload = json.loads(pointer.read_text(encoding="utf-8-sig"))
            pointer_target = pointer_payload.get("status_path") if category == "automation_log_run" else pointer_payload.get("json")
            if pointer_target:
                run_protected.add(Path(pointer_target).resolve(strict=False).parent)
        except (OSError, ValueError, TypeError):
            pass
        for old in old_items(dirs, int(selected["automation_runs"]), run_protected):
            add(category, old)

    progress_root = runtime / "logs" / "progress"
    cutoff = time.time() - int(selected["progress_days"]) * 86400
    if progress_root.is_dir():
        for item in progress_root.iterdir():
            if (item.is_file() and not item.is_symlink() and item.stat().st_mtime < cutoff
                    and item.name != "duration-history.json" and "-current." not in item.name):
                add("old_progress_log", item)

    # Stale atomic-write leftovers and interrupted partial snapshots. Only
    # known runtime subtrees are inspected, and only files older than the
    # incomplete-snapshot grace period are eligible.
    temporary_roots = (
        runtime / "logs" / "progress",
        runtime / "snapshots",
        runtime / "training-datasets",
    )
    for temporary_root in temporary_roots:
        if not temporary_root.is_dir():
            continue
        for item in temporary_root.rglob("*"):
            try:
                if (
                    item.is_file()
                    and not item.is_symlink()
                    and (item.name.endswith(".tmp") or item.name.endswith(".partial"))
                    and item.stat().st_mtime < incomplete_cutoff
                ):
                    add("stale_temporary_file", item)
            except OSError:
                pass

    if apply:
        for row in candidates:
            target = Path(row["path"])
            try:
                archive_text = row.get("manifest_archive")
                if archive_text:
                    source_manifest = target / "manifest.json"
                    if source_manifest.exists():
                        archive = Path(archive_text)
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_manifest, archive)
                if target.is_dir():
                    _remove_runtime_directory(target)
                else:
                    target.unlink()
                deleted.append(row)
            except Exception as exc:
                errors.append({"path": str(target), "error": str(exc)})

    report = {
        "schema_version": "1.0",
        "generated_at": now(),
        "status": "warning" if errors else "ok",
        "mode": "apply" if apply else "dry_run",
        "runtime_root": str(runtime),
        "policy": selected,
        "protected": protected,
        "candidates": candidates,
        "deleted": deleted,
        "errors": errors,
        "candidate_count": len(candidates),
        "deleted_count": len(deleted),
        "bytes_reclaimable": sum(int(row["size_bytes"]) for row in candidates),
        "bytes_freed": sum(int(row["size_bytes"]) for row in deleted),
    }
    report["gib_reclaimable"] = round(report["bytes_reclaimable"] / (1024 ** 3), 3)
    report["gib_freed"] = round(report["bytes_freed"] / (1024 ** 3), 3)
    report_root = runtime / "reports" / "cleanup"
    stamp = datetime.now(DISPLAY_TIMEZONE).strftime("%Y%m%dT%H%M%S%z")
    report_path = report_root / f"cleanup-{stamp}.json"
    atomic_json(report_path, report)
    atomic_json(report_root / "latest.json", {"report": str(report_path), **{k: report[k] for k in ("generated_at", "status", "mode", "candidate_count", "deleted_count", "bytes_freed")}})
    report["report_path"] = str(report_path)
    return report


def compact_result(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep run.json and the final report small even for multi-megabyte audits."""
    if not payload:
        return None
    result: dict[str, Any] = {}
    for key in ("status", "downloaded", "inserted", "skipped", "warnings", "errors"):
        if key in payload:
            result[key] = payload[key]
    details = payload.get("details")
    if isinstance(details, dict):
        allowed = {
            "publication_id", "surface_set_id", "record_count", "path", "dataset_id",
            "stations", "sensors", "measurements", "selected_sensors",
            "parameters_requested", "parameters_collected", "quality_flags_created",
            "verified_by_parameter", "awaiting_by_parameter", "reason",
        }
        selected = {key: value for key, value in details.items() if key in allowed}
        if selected:
            result["details"] = selected
    for key in ("publication_id", "surface_set_id", "dataset_id", "profile"):
        if key in payload:
            result[key] = payload[key]
    return result


def split_training_results(
    payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return durable selected models and failed attempts from E10 output.

    Candidate fits are not durable models.  Only an entry with ``selected``
    and ``model_version`` proves that registration completed.  This distinction
    prevents a console/MLflow failure from being reported as a saved model.
    """
    details = (payload or {}).get("details") or {}
    completed = list(details.get("completed_models") or [])
    successful = [
        item for item in completed
        if item.get("selected") is True
        and item.get("model_version")
        and str(item.get("status") or "").startswith("success")
    ]
    failed = [
        item for item in completed
        if item.get("status") in {"candidate_failed", "failed"}
    ]
    return successful, failed


def failure_diagnostic(
    stage: "Stage", code: int | None, payload: dict[str, Any] | None,
    stderr: str, log_path: Path,
) -> dict[str, Any]:
    """Create a bounded, operator-friendly diagnosis without copying huge logs."""
    data = payload or {}
    diagnostic: dict[str, Any] = {
        "stage": stage.name,
        "command": stage.command,
        "exit_code": code,
        "meaning": "polecenie nie zakończyło się pełnym sukcesem",
        "log_path": str(log_path),
        "report_path": data.get("report_path") or (data.get("details") or {}).get("report_path"),
        "status": data.get("status"),
        "decision": data.get("decision"),
        "hard_failures": (data.get("hard_failures") or [])[:20],
        "quality_failures": (data.get("quality_failures") or [])[:20],
        "approved_targets": data.get("approved_targets") or [],
        "experimental_targets": data.get("experimental_targets") or [],
        "errors": data.get("errors"),
        "warnings": data.get("warnings"),
    }
    if not payload and stderr.strip():
        diagnostic["stderr_tail"] = "\n".join(stderr.strip().splitlines()[-25:])[-6000:]
    if stage.command == "audit-hourly-serving-contract":
        diagnostic["meaning"] = "audyt kontraktu prognoz wykrył niespełnione warunki"
        diagnostic["recommended_action"] = (
            "Napraw hard_failures przed publikacją; jeśli hard_failures jest puste, kontrakt przeszedł "
            "i decyzja brzmi continue_without_experimental_targets, proces może pominąć cele eksperymentalne."
        )
    elif code == 4:
        diagnostic["meaning"] = "częściowy sukces — część rekordów lub kontroli wymaga uwagi"
        diagnostic["recommended_action"] = "Sprawdź wskazany raport i oceń błędy według parametrów oraz stacji."
    else:
        diagnostic["recommended_action"] = "Sprawdź stderr_tail i pełny log, usuń przyczynę, następnie użyj -Resume."
    return {key: value for key, value in diagnostic.items() if value not in (None, [], "")}


def diagnostic_message(diagnostic: dict[str, Any]) -> str:
    parts = [
        f"Etap: {diagnostic.get('stage')}",
        f"Polecenie: {diagnostic.get('command')}",
        f"Kod zakończenia: {diagnostic.get('exit_code')}",
        f"Znaczenie: {diagnostic.get('meaning')}",
    ]
    if diagnostic.get("decision"):
        parts.append(f"Decyzja audytu: {diagnostic['decision']}")
    if diagnostic.get("approved_targets"):
        parts.append("Cele zatwierdzone: " + ", ".join(map(str, diagnostic["approved_targets"])))
    if diagnostic.get("experimental_targets"):
        parts.append("Cele eksperymentalne: " + ", ".join(map(str, diagnostic["experimental_targets"])))
    if diagnostic.get("hard_failures"):
        parts.append("Twarde błędy: " + json.dumps(diagnostic["hard_failures"], ensure_ascii=False))
    if diagnostic.get("quality_failures"):
        parts.append("Problemy jakości: " + json.dumps(diagnostic["quality_failures"], ensure_ascii=False))
    if diagnostic.get("report_path"):
        parts.append(f"Raport szczegółowy: {diagnostic['report_path']}")
    parts.append(f"Pełny log: {diagnostic.get('log_path')}")
    parts.append(f"Dalsze działanie: {diagnostic.get('recommended_action')}")
    return "\n".join(parts)


@dataclass(frozen=True)
class Stage:
    name: str
    command: str
    args: tuple[str, ...] = ()
    weight: float = 1.0
    timeout_minutes: int = 60
    progress_hint: str | None = None
    description: str = ""
    accepted_exit_codes: tuple[int, ...] = (0,)


AFTER_MODEL = [
    Stage("Generowanie prognoz stacyjnych", "predict", weight=4, timeout_minutes=180, progress_hint="predict"),
    Stage("Weryfikacja prognoz", "verify", weight=2, timeout_minutes=120, progress_hint="verif"),
    Stage(
        "Generowanie i publikacja danych dashboardu — Serving v2",
        "build-spatial-surfaces",
        weight=4,
        timeout_minutes=180,
        progress_hint="spatial",
        description=(
            "Budowa powierzchni parametr × horyzont, kompresja JSON.GZ, publikacja "
            "manifestu wydania i atomowa aktualizacja serving/latest.json."
        ),
    ),
    Stage(
        "Walidacja danych dashboardu — Serving v2",
        "validate-spatial-surfaces",
        weight=1,
        timeout_minutes=60,
        description="Kontrola wszystkich opublikowanych powierzchni i ich sum kontrolnych.",
    ),
    Stage(
        "Kontrola gotowości publikacji dashboardu",
        "storage-health",
        weight=1,
        timeout_minutes=30,
        description=(
            "Sprawdza serving/latest.json i skompresowane powierzchnie. "
            "Pełny dashboard_snapshot i baza treningowa nie są publikowane."
        ),
    ),
]


def stages_for(
    profile: str,
    targets: str | None = None,
    fill_missing_ranges: bool = False,
    parameters: str | None = None,
    data_start: str | None = None,
    data_end: str | None = None,
    skip_gios_current: bool = False,
    skip_imgw_current: bool = False,
    experimental_targets: str | None = None,
    training_start: str | None = None,
    training_end: str | None = None,
) -> list[Stage]:
    selected = parameters or "parametry z aktywną rolą collect_current"
    scope_args: tuple[str, ...] = ()
    if data_start:
        scope_args += ("--start", data_start)
    if data_end:
        scope_args += ("--end", data_end)
    if parameters:
        scope_args += ("--parameters", parameters)
    stages = [Stage("Katalog parametrów przed aktualizacją", "parameter-catalog", weight=0.5, timeout_minutes=30,
                    description="Odczyt ról parametrów: current, history, feature, target i publish.")]
    if not skip_gios_current:
        args = ("--parameters", parameters) if parameters else ()
        stages.append(Stage("GIOŚ — pomiary bieżące", "collect-gios", args, 2.5, 120, "collect-gios",
                            f"Źródło: GIOŚ API; parametry: {selected}; zakres: najnowsze dane udostępnione przez endpoint."))
    if not skip_imgw_current:
        stages.append(Stage("IMGW — pomiary bieżące", "collect-imgw", (), 1.5, 45, "collect-imgw",
                            "Źródło: IMGW SYNOP; parametry pogodowe: wszystkie obsługiwane; zakres: najnowszy pakiet."))
    stages += [
        Stage("Walidacja danych", "validate", weight=2, timeout_minutes=120, progress_hint="validate",
              description="Kontrola zakresów, braków, współrzędnych, świeżości i skoków wartości.",
              accepted_exit_codes=(0, 4)),
        Stage("Dopasowanie stacji", "match-stations", weight=1, timeout_minutes=30,
              description="Łączenie stacji jakości powietrza z najbliższymi stacjami pogodowymi."),
        Stage("Audyt pokrycia", "data-range-audit", scope_args, 1, 45,
              description=f"Zakres audytu: {data_start or 'domyślny'} — {data_end or 'teraz'}; parametry: {selected}."),
        Stage("Plan brakujących zakresów (bez pobierania)", "plan-missing-ranges", scope_args, 1, 30,
              description="Wyliczanie brakujących przedziałów; ten etap nie pobiera danych."),
    ]
    if fill_missing_ranges:
        stages.append(Stage("Uzupełnianie zatwierdzonych braków historii", "fill-missing-ranges", scope_args, 6, 720, "missing",
                            f"Pobieranie historii: {data_start or 'zakres domyślny'} — {data_end or 'teraz'}; parametry: {selected}; cache: lokalny."))
        stages.append(Stage("Ponowny audyt pokrycia", "data-range-audit", scope_args, 1, 60,
                            description="Kontrola pokrycia po uzupełnieniu historii."))
    training_profile = "full" if profile == "full" else "quick"
    target_args = ("--targets", targets) if targets else ()
    training_window_args: tuple[str, ...] = ()
    if training_start:
        training_window_args += ("--training-start", training_start)
    if training_end:
        training_window_args += ("--training-end", training_end)
    # "*" is the operator-facing shorthand for all active experimental
    # targets.  Passing a bare asterisk to a Windows child process may expand
    # it into every file and directory in ProjectRoot before Typer parses the
    # command.  Omitting the option already means "all", so never forward the
    # wildcard itself.
    experimental_value = str(experimental_targets or "").strip()
    experimental_args = (
        ("--allow-experimental-targets", experimental_value)
        if experimental_value and experimental_value != "*"
        else ()
    )
    stages += [
        Stage("Budowa cech", "build-features", weight=4, timeout_minutes=240, progress_hint="feature"),
        Stage(
            "Plan przyrostowej warstwy treningowej",
            "training-delta-plan",
            ("--profile", training_profile),
            1,
            30,
            description=(
                "Sprawdza dziennik zmian i limit kompaktowania. Nie kopiuje bazy. "
                "Po osiągnięciu limitu zatrzymuje przebieg przed treningiem."
            ),
        ),
        Stage(
            "Budowa przyrostowej delty treningowej",
            "training-delta-build",
            (
                "--profile", training_profile,
                "--confirmation", "BUILD VERIFIED TRAINING DELTA",
            ),
            2,
            120,
            description=(
                "Atomowo zapisuje wyłącznie nowe, zmienione i usunięte rekordy "
                "oraz aktualne małe tabele wymiarów."
            ),
        ),
        Stage(
            "Szybka weryfikacja base + deltas",
            "training-delta-preflight",
            ("--profile", training_profile),
            2,
            240,
            description=(
                "Kontroluje sumy SHA-256 delt i granice dziennika zmian. "
                "Pełna kontrola logiczna pozostaje dostępna jako training-delta-verify."
            ),
        ),
        Stage(
            "Trening modeli godzinowych",
            "snapshot-train-hourly",
            ("--profile", training_profile, "--snapshot", "layered", *target_args, *training_window_args),
            8,
            900 if training_profile == "quick" else 1440,
            "snapshot-train",
            description=(
                "Trening na zweryfikowanym widoku base + deltas. Wyłącznie modele "
                "approved mogą zostać aktywowane; experimental pozostają nieaktywne."
            ),
            accepted_exit_codes=(0, 4),
        ),
        Stage("Audyt artefaktów modeli", "audit-hourly-models", weight=1, timeout_minutes=90),
        Stage("Audyt kontraktu serving", "audit-hourly-serving-contract", experimental_args, weight=1, timeout_minutes=60,
              description="Kontrola kompletności serving; aktywne cele eksperymentalne są domyślnie publikowane z ostrzeżeniem. Wartość 'none' jawnie je wyłącza.",
              accepted_exit_codes=(0, 4)),
        Stage("Katalog parametrów po treningu", "parameter-catalog", weight=0.5, timeout_minutes=30),
    ]
    stages += AFTER_MODEL
    return stages


class Runner:
    def __init__(self, ns: argparse.Namespace):
        # Źródłem prawdy jest katalog, z którego uruchomiono automat.
        # Parametr jest tylko jawnym nadpisaniem używanym m.in. przez Harmonogram zadań.
        self.project = Path(ns.project_root or Path.cwd()).resolve()
        self.runtime = Path(ns.runtime_root).resolve()
        self.config = Path(ns.config or self.runtime / "config.yaml").resolve()
        self.env_file = Path(ns.env_file or self.runtime / "smog-ai.env").resolve()
        self.python = self.project / ".venv" / "Scripts" / "python.exe"
        self.resume = ns.resume
        self.root = self.runtime / "logs" / "automation"
        self.current_path = self.root / "current.json"
        self.run_id = ns.run_id
        if self.resume and not self.run_id:
            self.run_id = self.find_latest_resumable_run()
        self.run_id = self.run_id or f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:10]}"
        self.run_dir = self.root / "runs" / self.run_id
        self.resource_sample_seconds = float(
            getattr(ns, "resource_sample_seconds", 5.0)
        )
        self.resource_sampler = ResourceSampler(
            self.run_dir / "resources.jsonl",
            self.resource_sample_seconds,
        )
        self.status_path = self.run_dir / "run.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.lock_path = self.root / "automation.lock"
        self.resume_state: dict[str, Any] | None = None
        if self.resume:
            if not self.status_path.exists():
                raise RuntimeError(f"Nie znaleziono checkpointu run_id={self.run_id}: {self.status_path}")
            self.resume_state = json.loads(self.status_path.read_text(encoding="utf-8-sig"))
        contract = (self.resume_state or {}).get("input_contract") or {}
        legacy_plan = (self.resume_state or {}).get("download_plan") or {}
        self.profile = contract.get("profile", (self.resume_state or {}).get("profile", ns.profile))
        self.targets = contract.get("targets", (self.resume_state or {}).get("targets", ns.targets))
        self.experimental_targets = contract.get(
            "experimental_targets", getattr(ns, "experimental_targets", None)
        )
        self.fill_missing_ranges = bool(contract.get("fill_missing_ranges", (self.resume_state or {}).get("fill_missing_ranges", ns.fill_missing_ranges)))
        self.parameters = contract.get("parameters", legacy_plan.get("parameters") if legacy_plan.get("parameters") != "z aktywną rolą collect_current" else ns.parameters)
        self.data_start = contract.get("data_start", legacy_plan.get("start", ns.data_start))
        self.data_end = contract.get("data_end", legacy_plan.get("end", ns.data_end))
        self.training_start = contract.get(
            "training_start", getattr(ns, "training_start", None)
        )
        self.training_end = contract.get(
            "training_end", getattr(ns, "training_end", None)
        )
        self.skip_gios_current = bool(contract.get("skip_gios_current", not legacy_plan.get("gios_current", not ns.skip_gios_current)))
        self.skip_imgw_current = bool(contract.get("skip_imgw_current", not legacy_plan.get("imgw_current", not ns.skip_imgw_current)))
        self.max_validation_errors = int(contract.get("max_validation_errors", (self.resume_state or {}).get("max_validation_errors", ns.max_validation_errors)))
        self.skip_cleanup = bool(contract.get("skip_cleanup", getattr(ns, "skip_cleanup", False)))
        self.cleanup_policy = {
            "training_quick": int(contract.get("keep_training_quick", getattr(ns, "keep_training_quick", 2))),
            "training_full": int(contract.get("keep_training_full", getattr(ns, "keep_training_full", 3))),
            "dashboard_snapshots": int(contract.get("keep_dashboard_snapshots", getattr(ns, "keep_dashboard_snapshots", 5))),
            "forecast_publications": int(contract.get("keep_forecast_publications", getattr(ns, "keep_forecast_publications", 10))),
            "map_surface_sets": int(contract.get("keep_map_surface_sets", getattr(ns, "keep_map_surface_sets", 5))),
            "automation_runs": int(contract.get("keep_automation_runs", getattr(ns, "keep_automation_runs", 30))),
            "progress_days": int(contract.get("progress_retention_days", getattr(ns, "progress_retention_days", 30))),
            "incomplete_snapshot_hours": int(contract.get("incomplete_snapshot_hours", getattr(ns, "incomplete_snapshot_hours", 24))),
        }
        self.stages = stages_for(
            self.profile, self.targets, self.fill_missing_ranges, self.parameters,
            self.data_start, self.data_end, self.skip_gios_current, self.skip_imgw_current,
            self.experimental_targets, self.training_start, self.training_end,
        )
        self.state: dict[str, Any] = {}

    def find_latest_resumable_run(self) -> str:
        runs_root = self.root / "runs"
        if not runs_root.exists():
            raise RuntimeError("Brak wcześniejszych przebiegów do wznowienia.")
        candidates: list[tuple[float, str]] = []
        for path in runs_root.glob("*/run.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if payload.get("status") in {"failed", "running", "cancelled", "canceled"}:
                    candidates.append((path.stat().st_mtime, str(payload.get("run_id") or path.parent.name)))
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("Nie znaleziono przerwanego ani nieudanego przebiegu do wznowienia.")
        return max(candidates)[1]

    def reconcile_resume_stages(self, prior: dict[str, Any]) -> list[dict[str, Any]]:
        # Display names are translated Polish text and legacy checkpoints may
        # contain mojibake after being read/written by Windows PowerShell 5.1.
        # They must never be stage identity.  Command occurrence is stable and
        # also distinguishes repeated commands such as parameter-catalog and
        # data-range-audit.
        old_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        old_occurrences: dict[str, int] = {}
        for old in prior.get("stages", []):
            command = str(old.get("command") or "")
            occurrence = old_occurrences.get(command, 0) + 1
            old_occurrences[command] = occurrence
            old_by_key[(command, occurrence)] = old
        reconciled: list[dict[str, Any]] = []
        new_occurrences: dict[str, int] = {}
        for stage in self.stages:
            occurrence = new_occurrences.get(stage.command, 0) + 1
            new_occurrences[stage.command] = occurrence
            key = (stage.command, occurrence)
            old = old_by_key.get(key)
            if old and old.get("status") in {"success", "warning", "partial_success"}:
                item = dict(old)
                item["restored_from_checkpoint"] = True
                # Refresh presentation metadata from current UTF-8 source while
                # preserving the completed execution evidence.
                item["name"] = stage.name
                item["description"] = stage.description
                item["stage_key"] = f"{stage.command}#{occurrence}"
            else:
                item = {"name": stage.name, "command": stage.command,
                        "description": stage.description, "status": "pending",
                        "stage_key": f"{stage.command}#{occurrence}"}
                if old:
                    item["previous_status"] = old.get("status")
                    item["previous_log_path"] = old.get("log_path")
                    item["previous_started_at"] = old.get("started_at")
                    item["attempt_count"] = int(
                        old.get("attempt_count") or (1 if old.get("log_path") else 0)
                    )
                    item["child_attempts"] = list(old.get("child_attempts") or [])
                    if old.get("child_run_id"):
                        item["child_attempts"].append(
                            {
                                "run_id": old.get("child_run_id"),
                                "path": old.get("child_progress_path"),
                                "status": old.get("status"),
                            }
                        )
            reconciled.append(item)
        return reconciled

    def related_child_progress(
        self,
        stage: Stage,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Find child runs created during this automation run, newest first."""

        hint = stage.progress_hint
        directory = self.runtime / "logs" / "progress"
        if not hint or not directory.exists():
            return []
        started_text = str(self.state.get("started_at") or "")
        try:
            automation_started = datetime.fromisoformat(
                started_text.replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            automation_started = 0.0
        rows: list[tuple[Path, dict[str, Any]]] = []
        for path in directory.glob(f"*{hint}*.json"):
            if path.name.endswith("-current.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                run_type = str(payload.get("run_type") or "")
                child_started = datetime.fromisoformat(
                    str(payload.get("started_at") or "").replace("Z", "+00:00")
                ).timestamp()
                if hint not in run_type or child_started < automation_started - 5:
                    continue
                rows.append((path, payload))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(
            rows,
            key=lambda row: str(row[1].get("updated_at") or ""),
            reverse=True,
        )

    def mark_stage_from_child_success(
        self,
        record: dict[str, Any],
        stage: Stage,
        path: Path,
        child: dict[str, Any],
        completed_weight: float,
        total_weight: float,
    ) -> None:
        completed_models = nested_completed_models(child)
        record.update(
            status="success",
            finished_at=child.get("finished_at") or now(),
            exit_code=0,
            child_run_id=child.get("run_id"),
            child_progress_path=str(path),
            recovered_from_child=True,
            recovery_note=(
                "Etap odzyskano z zakończonego procesu podrzędnego; "
                "nie uruchamiano ponownego treningu."
            ),
        )
        record.pop("failure_diagnostic", None)
        if completed_models:
            record["completed_models"] = completed_models
            self.state["completed_models"] = completed_models
        self.state.pop("failure_diagnostic", None)
        self.state.pop("error", None)
        self.state.update(
            stage_percent=100.0,
            overall_percent=round(
                (completed_weight + stage.weight) / total_weight * 100,
                2,
            ),
            current_task="odzyskano zakończony proces podrzędny",
            current_detail=record["recovery_note"],
        )
        self.event(
            "INFO",
            "Odzyskano zakończony etap z procesu podrzędnego",
            stage=stage.name,
            child_run_id=child.get("run_id"),
        )
        self.save()

    def adopt_or_recover_child(
        self,
        record: dict[str, Any],
        stage: Stage,
        completed_weight: float,
        total_weight: float,
    ) -> bool:
        """Adopt a surviving child or recover its completed result on resume."""

        candidates = self.related_child_progress(stage)
        active = next(
            (
                (path, child)
                for path, child in candidates
                if child.get("status") not in TERMINAL
                and child.get("status") != "skipped_locked"
                and process_is_alive(child.get("pid"))
            ),
            None,
        )
        if active is not None:
            path, child = active
            record.update(
                status="running",
                adopted_child=True,
                child_run_id=child.get("run_id"),
                child_progress_path=str(path),
            )
            self.state.update(
                current_stage=stage.name,
                current_task=child.get("current_task") or "oczekiwanie na istniejący trening",
                current_detail=(
                    "Przejęto monitoring istniejącego procesu; nowy trening "
                    "nie został uruchomiony."
                ),
            )
            self.event(
                "INFO",
                "Przejęto monitoring żywego procesu podrzędnego",
                stage=stage.name,
                child_run_id=child.get("run_id"),
                pid=child.get("pid"),
            )
            self.save()
            deadline = time.time() + stage.timeout_minutes * 60
            missing_since: float | None = None
            while time.time() < deadline:
                try:
                    child = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, TypeError):
                    time.sleep(2)
                    continue
                fraction = float(child.get("overall_fraction", 0) or 0)
                self.state["stage_percent"] = round(fraction * 100, 2)
                self.state["overall_percent"] = round(
                    (completed_weight + stage.weight * fraction) / total_weight * 100,
                    2,
                )
                self.state["current_task"] = child.get("current_task") or stage.command
                models = nested_completed_models(child)
                if models:
                    self.state["completed_models"] = models
                resource = self.resource_sampler.sample(
                    int(child.get("pid") or 0), stage.name
                )
                if resource is not None:
                    self.state["resource_current"] = resource
                self.save()
                if child.get("status") == "success":
                    self.mark_stage_from_child_success(
                        record, stage, path, child, completed_weight, total_weight
                    )
                    return True
                if child.get("status") in {"failed", "cancelled", "canceled"}:
                    return False
                if process_is_alive(child.get("pid")):
                    missing_since = None
                else:
                    missing_since = missing_since or time.time()
                    if time.time() - missing_since > 15:
                        break
                time.sleep(2)

        successful = next(
            (
                (path, child)
                for path, child in candidates
                if child.get("status") == "success"
                and float(child.get("overall_percent", 0) or 0) >= 100
            ),
            None,
        )
        if successful is not None:
            self.mark_stage_from_child_success(
                record,
                stage,
                successful[0],
                successful[1],
                completed_weight,
                total_weight,
            )
            return True
        return False

    def event(self, level: str, message: str, **data: Any) -> None:
        row = {"timestamp": now(), "level": level, "message": message, **data}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def save(self) -> None:
        self.state["updated_at"] = now()
        atomic_json(self.status_path, self.state)
        atomic_json(self.current_path, {"run_id": self.run_id, "status_path": str(self.status_path), "updated_at": self.state["updated_at"]})

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                old = json.loads(self.lock_path.read_text(encoding="utf-8"))
                pid = int(old.get("pid", 0))
                if pid and os.name == "nt":
                    probe = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
                    if str(pid) in probe.stdout:
                        raise RuntimeError(f"Inny automat już działa: PID={pid}, run_id={old.get('run_id')}")
            except RuntimeError:
                raise
            except Exception:
                pass
        atomic_json(self.lock_path, {"pid": os.getpid(), "run_id": self.run_id, "created_at": now()})

    def preflight(self) -> None:
        required = [self.project, self.runtime, self.config, self.env_file, self.python,
                    self.project / "smog_ai" / "resources" / "poland_boundary.geojson"]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise RuntimeError("Brak wymaganych ścieżek:\n- " + "\n- ".join(missing))
        if self.resume_state and self.resume_state.get("project_root"):
            previous_root = Path(str(self.resume_state["project_root"])).resolve()
            if previous_root != self.project:
                raise RuntimeError(
                    "Checkpoint należy do innego katalogu projektu. "
                    f"Checkpoint: {previous_root}; bieżący katalog: {self.project}. "
                    "Przejdź do właściwej gałęzi albo rozpocznij nowy przebieg."
                )
        if bool(self.data_start) != bool(self.data_end):
            raise RuntimeError("Zakres historii wymaga jednocześnie --data-start i --data-end.")
        if self.data_start and self.data_end:
            start = datetime.fromisoformat(self.data_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.data_end.replace("Z", "+00:00"))
            if start >= end:
                raise RuntimeError("Początek zakresu danych musi być wcześniejszy niż koniec.")
        if bool(self.training_start) != bool(self.training_end):
            raise RuntimeError(
                "Zakres treningu wymaga jednocześnie --training-start i --training-end."
            )
        if self.training_start and self.training_end:
            training_start = datetime.fromisoformat(
                self.training_start.replace("Z", "+00:00")
            )
            training_end = datetime.fromisoformat(
                self.training_end.replace("Z", "+00:00")
            )
            if training_start >= training_end:
                raise RuntimeError(
                    "Początek zakresu treningu musi być wcześniejszy niż koniec."
                )
        load_env(self.env_file)
        # Proces automatu zawsze publikuje do lokalnego Bridge. Nie modyfikuje pliku .env.
        os.environ["SMOG_AI_PROJECT_ROOT"] = str(self.project)
        os.environ["SMOG_AI_DATA_ROOT"] = str(self.runtime)
        os.environ["SMOG_AI_OBJECT_STORE_BACKEND"] = "local"
        os.environ["SMOG_AI_OBJECT_STORE_LOCAL_ROOT"] = str(self.runtime / "object-store")
        os.environ["SMOG_AI_OBJECT_STORE_PREFIX"] = ""
        os.environ["SMOG_AI_SERVER_STORAGE_BACKEND"] = "object_store"
        os.environ.setdefault("SMOG_AI_OBSERVABILITY_BACKEND", "none")
        if self.experimental_targets:
            os.environ["SMOG_AI_EXPERIMENTAL_TARGETS"] = self.experimental_targets
        else:
            os.environ.pop("SMOG_AI_EXPERIMENTAL_TARGETS", None)
        stale = os.environ.get("SMOG_AI_PROJECT_ROOT", "")
        if Path(stale).resolve() != self.project:
            raise RuntimeError(f"Niespójny SMOG_AI_PROJECT_ROOT: {stale}")
        self.check_mlflow_ready("Preflight przed rozpoczęciem automatu")

    def check_mlflow_ready(self, context: str) -> dict[str, Any]:
        """Fail fast when configured HTTP MLflow is unavailable."""

        helper = self.project / "scripts" / "mlflow_preflight.py"
        report_path = self.run_dir / "00-mlflow-preflight.json"
        command = [
            str(self.python), str(helper),
            "--project-root", str(self.project),
            "--config", str(self.config),
            "--env-file", str(self.env_file),
            "--timeout-seconds", "2.0",
        ]
        probe = subprocess.run(
            command,
            cwd=self.project,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        try:
            report = json.loads(probe.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            report = {
                "status": "invalid_configuration",
                "detail": probe.stderr.strip() or probe.stdout.strip(),
            }
        report.update({"context": context, "checked_at": now()})
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(report_path, report)
        if probe.returncode not in {0, 2}:
            raise RuntimeError(
                f"{context}: MLflow nie jest gotowy ({report.get('status')}). "
                f"{report.get('message_pl') or report.get('detail') or ''} "
                "Uruchom w osobnym PowerShellu: "
                ".\\scripts\\Start-LocalMLflow.ps1 -ProjectRoot (Get-Location).Path "
                "-RuntimeRoot 'C:\\ProgramData\\SmogAI' -Port 5000. "
                f"Raport: {report_path}"
            )
        return report

    def progress_candidate(self, started: float, hint: str | None) -> Path | None:
        directory = self.runtime / "logs" / "progress"
        if not directory.exists():
            return None
        candidates = []
        for path in directory.glob("*-current.json"):
            try:
                if path.stat().st_mtime >= started - 2 and (not hint or hint in path.name):
                    candidates.append(path)
            except OSError:
                pass
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    def run_stage(self, index: int, stage: Stage, completed_weight: float, total_weight: float) -> None:
        record = self.state["stages"][index]
        if self.resume and record.get("status") in {"success", "warning", "partial_success"}:
            self.event("INFO", "Etap pominięty przy wznowieniu", stage=stage.name)
            return
        if self.resume and self.adopt_or_recover_child(
            record, stage, completed_weight, total_weight
        ):
            return
        if stage.command == "snapshot-train-hourly":
            self.check_mlflow_ready("Kontrola bezpośrednio przed treningiem modeli")
        attempt = int(record.get("attempt_count") or 0) + 1
        record["attempt_count"] = attempt
        log_name = f"{index+1:02d}-{stage.command}"
        if attempt > 1:
            log_name += f"-attempt-{attempt}"
        record.update(status="running", started_at=now(), description=stage.description,
                      log_path=str(self.run_dir / f"{log_name}.log"))
        self.state.update(current_stage=stage.name, current_stage_index=index + 1, stage_percent=0.0,
                          overall_percent=round(completed_weight / total_weight * 100, 2), current_task=stage.command,
                          current_detail=stage.description)
        self.save()
        log_path = Path(record["log_path"])
        out_path, err_path = log_path.with_suffix(".stdout"), log_path.with_suffix(".stderr")
        args = [str(self.python), "-m", "smog_ai", stage.command, "--config", str(self.config), "--env-file", str(self.env_file), *stage.args]
        started = time.time()
        self.event("INFO", "Start etapu", stage=stage.name, command=stage.command)
        child_env = os.environ.copy()
        # Scheduled Windows PowerShell commonly exposes cp1250 to redirected
        # children.  MLflow prints a runner emoji when it closes a run; under
        # cp1250 that harmless message raised UnicodeEncodeError and made every
        # successfully fitted candidate look failed.  Make the byte contract
        # explicit for every automation child, not only for the wrapper.
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = subprocess.Popen(args, cwd=self.project, env=child_env, stdout=out, stderr=err,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            record["pid"] = proc.pid
            self.resource_sampler.sample(proc.pid, stage.name, force=True)
            terminal_seen_at: float | None = None
            while proc.poll() is None:
                elapsed = time.time() - started
                candidate = self.progress_candidate(started, stage.progress_hint)
                child: dict[str, Any] = {}
                if candidate:
                    try:
                        child = json.loads(candidate.read_text(encoding="utf-8-sig"))
                        fraction = float(child.get("overall_fraction", 0) or 0)
                        record["child_progress_path"] = str(candidate)
                        record["child_run_id"] = child.get("run_id")
                        attempts = record.setdefault("child_attempts", [])
                        if child.get("run_id") and not any(
                            item.get("run_id") == child.get("run_id")
                            for item in attempts
                            if isinstance(item, dict)
                        ):
                            attempts.append(
                                {
                                    "run_id": child.get("run_id"),
                                    "path": str(candidate),
                                    "status": child.get("status"),
                                    "pid": child.get("pid"),
                                }
                            )
                        self.state["stage_percent"] = round(max(0, min(1, fraction)) * 100, 2)
                        self.state["current_task"] = child.get("current_task") or stage.command
                        self.state["child_progress_detail"] = child.get("detail") or {}
                        record["child_progress_detail"] = child.get("detail") or {}
                        detail_payload = child.get("detail") or {}
                        if isinstance(detail_payload, dict):
                            if detail_payload.get("candidate_plan") is not None:
                                self.state["candidate_plan"] = list(
                                    detail_payload.get("candidate_plan") or []
                                )
                            if detail_payload.get("completed_models") is not None:
                                self.state["completed_models"] = detail_payload.get("completed_models")
                            self.state["current_model"] = {
                                key: detail_payload.get(key)
                                for key in ("target", "provider", "phase", "candidate_index", "candidate_total")
                                if detail_payload.get(key) is not None
                            }
                        detail_parts = [stage.description]
                        if child.get("run_type"):
                            detail_parts.append(f"zadanie podrzędne: {child['run_type']}")
                        if child.get("current_stage"):
                            detail_parts.append(f"podetap: {child['current_stage']}")
                        if child.get("current_task"):
                            detail_parts.append(str(child["current_task"]))
                        self.state["current_detail"] = " · ".join(x for x in detail_parts if x)
                        self.state["eta_seconds"] = child.get("current_task_eta_seconds")
                        self.state["overall_percent"] = round((completed_weight + stage.weight * fraction) / total_weight * 100, 2)
                        if child.get("status") in TERMINAL:
                            terminal_seen_at = terminal_seen_at or time.time()
                    except Exception:
                        pass
                else:
                    # Heartbeat, nawet jeśli dana komenda nie wystawia własnego progress JSON.
                    self.state["stage_percent"] = min(95.0, round(elapsed / max(30, stage.timeout_minutes * 45) * 100, 2))
                self.state["elapsed_seconds"] = round(time.time() - self.state["started_epoch"], 1)
                record["elapsed_seconds"] = round(elapsed, 1)
                record["stdout_bytes"] = out_path.stat().st_size if out_path.exists() else 0
                record["stderr_bytes"] = err_path.stat().st_size if err_path.exists() else 0
                resource = self.resource_sampler.sample(proc.pid, stage.name)
                if resource is not None:
                    self.state["resource_current"] = resource
                    history = self.state.setdefault("resource_history", [])
                    history.append(resource)
                    self.state["resource_history"] = history[-240:]
                    self.state["resource_summary"] = self.resource_sampler.summary()
                self.save()
                if elapsed > stage.timeout_minutes * 60:
                    self.stop_tree(proc.pid)
                    raise TimeoutError(f"Etap przekroczył limit {stage.timeout_minutes} min: {stage.name}")
                if terminal_seen_at and time.time() - terminal_seen_at > 20:
                    # Znany przypadek: zadanie zapisało success, ale biblioteka zostawiła żywy wątek.
                    self.event("WARNING", "Zakończono osierocony proces po zapisanym sukcesie", stage=stage.name, pid=proc.pid)
                    self.stop_tree(proc.pid)
                    record["forced_shutdown_after_success"] = True
                    break
                time.sleep(2)
        code = proc.poll()
        if record.get("forced_shutdown_after_success"):
            code = 0
        stdout = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        stderr = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
        log_path.write_text(stdout + ("\n--- STDERR ---\n" + stderr if stderr else ""), encoding="utf-8")
        result_payload: dict[str, Any] | None = None
        if stdout.strip():
            # Standardowy stdout CLI jest JSON-em. Zachowujemy też zgodność z logiem,
            # który może mieć kilka linii przed obiektem.
            candidates = [stdout.strip()]
            starts = [pos for pos, char in enumerate(stdout) if char == "{"]
            candidates.extend(stdout[pos:].strip() for pos in reversed(starts))
            for candidate_text in candidates:
                try:
                    parsed = json.loads(candidate_text)
                    if isinstance(parsed, dict):
                        result_payload = parsed
                        break
                except Exception:
                    continue
        record["result_summary"] = compact_result(result_payload)
        result_details = (result_payload or {}).get("details") or {}
        if isinstance(result_details, dict) and result_details.get("completed_models"):
            completed_models = list(result_details.get("completed_models") or [])
            self.state["completed_models"] = completed_models
            record["completed_models"] = completed_models
        if stage.command == "snapshot-train-hourly" and isinstance(result_details, dict):
            trained_models = list(result_details.get("models") or [])
            classifications = {
                "approved": [],
                "experimental": [],
                "rejected": [],
            }
            for model in trained_models:
                if not isinstance(model, dict):
                    continue
                quality = str(model.get("quality_status") or "rejected").lower()
                if quality not in classifications:
                    quality = "rejected"
                classifications[quality].append(
                    {
                        "target": model.get("target"),
                        "provider": model.get("selected_provider"),
                        "version": model.get("model_version"),
                        "mae": model.get("score_mae"),
                        "activated": bool(model.get("activated")),
                        "reasons": (
                            model.get("quality_classification") or {}
                        ).get("reasons") or [],
                    }
                )
            record["model_classifications"] = classifications
            record["approved_targets"] = [
                item.get("target") for item in classifications["approved"]
            ]
            record["experimental_targets"] = [
                item.get("target") for item in classifications["experimental"]
            ]
            record["rejected_targets"] = [
                item.get("target") for item in classifications["rejected"]
            ]
            self.state["model_classifications"] = classifications
        if code == 5 and self.adopt_or_recover_child(
            record, stage, completed_weight, total_weight
        ):
            return
        if code not in stage.accepted_exit_codes and code is not None:
            diagnostic = failure_diagnostic(stage, code, result_payload, stderr, log_path)
            record.update(status="failed", finished_at=now(), exit_code=code,
                          duration_seconds=round(time.time() - started, 1),
                          failure_diagnostic=diagnostic)
            self.state["failure_diagnostic"] = diagnostic
            self.save()
            raise RuntimeError(diagnostic_message(diagnostic))
        final_status = "success"
        if code == 4:
            payload = result_payload or {}
            if stage.command == "audit-hourly-serving-contract":
                hard_failures = payload.get("hard_failures") or []
                decision = payload.get("decision")
                contract_passed = payload.get("serving_contract_passed") is True
                safe_partial = (
                    contract_passed
                    and not hard_failures
                    and decision == "continue_without_experimental_targets"
                )
                if not safe_partial:
                    diagnostic = failure_diagnostic(stage, code, payload, stderr, log_path)
                    record.update(status="failed", finished_at=now(), exit_code=code,
                                  duration_seconds=round(time.time() - started, 1),
                                  failure_diagnostic=diagnostic)
                    self.state["failure_diagnostic"] = diagnostic
                    self.save()
                    raise RuntimeError(diagnostic_message(diagnostic))
                approved = payload.get("approved_targets") or []
                experimental = payload.get("experimental_targets") or []
                quality_failures = payload.get("quality_failures") or []
                record["serving_decision"] = decision
                record["approved_targets"] = approved
                record["experimental_targets"] = experimental
                record["quality_failures"] = quality_failures
                record["warning"] = (
                    "Kontrakt serving jest technicznie poprawny, ale cele "
                    f"{', '.join(map(str, experimental)) or 'eksperymentalne'} nie przeszły bramy jakości. "
                    f"Proces jest kontynuowany tylko dla zatwierdzonych celów: {', '.join(map(str, approved))}."
                )
            elif stage.command == "snapshot-train-hourly":
                successful_models, failed_models = split_training_results(payload)
                record["successful_models"] = successful_models
                record["failed_models"] = failed_models
                record["warning"] = (
                    "Trening zakończył się częściowo. Zachowano "
                    f"{len(successful_models)} nowych modeli; "
                    f"{len(failed_models)} prób nie powiodło się. "
                    "Proces przechodzi do audytu artefaktów i kontraktu serving; "
                    "publikacja pozostaje zablokowana, jeżeli aktywne modele "
                    "wymaganych celów nie są kompletne i ważne."
                )
                final_status = "partial_success"
            else:
                errors = int(payload.get("errors", 0) or 0)
                record["quality_errors"] = errors
                record["warning"] = (
                    f"Walidacja danych zakończyła się kodem częściowym 4 i zgłosiła {errors} błędów jakości. "
                    "Dane pozostają w bazie z flagami; modele powinny korzystać z polityki jakości."
                )
                if self.max_validation_errors >= 0 and errors > self.max_validation_errors:
                    raise RuntimeError(
                        f"Walidacja wykryła {errors} błędów jakości, czyli więcej niż dozwolony próg "
                        f"{self.max_validation_errors}. Log: {log_path}"
                    )
            if stage.command != "snapshot-train-hourly":
                final_status = "warning"
            warnings = self.state.setdefault("warnings", [])
            if record["warning"] not in warnings:
                warnings.append(record["warning"])
            self.event("WARNING", record["warning"], stage=stage.name)
        record.update(status=final_status, finished_at=now(), duration_seconds=round(time.time() - started, 1), exit_code=code or 0)
        self.state["resource_summary"] = self.resource_sampler.summary()
        self.state.update(stage_percent=100.0, overall_percent=round((completed_weight + stage.weight) / total_weight * 100, 2))
        self.event("INFO", "Koniec etapu", stage=stage.name, duration_seconds=record["duration_seconds"])
        self.save()

    @staticmethod
    def stop_tree(pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass

    def publication_check(self) -> None:
        store = self.runtime / "object-store"
        required = [store / "serving" / "latest.json"]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise RuntimeError(
                "Publikacja nie utworzyła atomowego wskaźnika Serving v2: "
                + ", ".join(missing)
            )
        pointer = json.loads(required[0].read_text(encoding="utf-8-sig"))
        manifest_key = str(pointer.get("manifest_key") or "")
        manifest_path = store / Path(*manifest_key.split("/"))
        if not manifest_key or not manifest_path.exists():
            raise RuntimeError(
                f"Serving v2 wskazuje nieistniejący manifest: {manifest_key or '<brak>'}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        surfaces = list(manifest.get("surfaces") or [])
        if not surfaces:
            raise RuntimeError("Manifest Serving v2 nie zawiera żadnych powierzchni.")
        self.state["publication"] = {
            "contract": manifest.get("contract"),
            "release_id": manifest.get("release_id") or pointer.get("release_id"),
            "surface_set_id": manifest.get("surface_set_id"),
            "surface_count": len(surfaces),
            "parameters": manifest.get("parameters"),
            "horizons_hours": manifest.get("horizons_hours"),
            "manifest_path": str(manifest_path),
            "store_root": str(store),
        }

    def final_report_data(self) -> dict[str, Any]:
        counters = {key: 0 for key in ("downloaded", "inserted", "skipped", "warnings", "errors")}
        stage_rows: list[dict[str, Any]] = []
        source_stages = [dict(item) for item in self.state.get("stages", [])]
        running_indexes = [
            index for index, item in enumerate(source_stages)
            if item.get("status") == "running"
        ]
        active_running_index = (
            running_indexes[-1]
            if running_indexes and self.state.get("status") == "running"
            else None
        )
        for zero_index, item in enumerate(source_stages):
            index = zero_index + 1
            effective_status = item.get("status")
            if effective_status == "running" and zero_index != active_running_index:
                effective_status = "interrupted"
            summary = item.get("result_summary") or {}
            for key in counters:
                try:
                    counters[key] += int(summary.get(key, 0) or 0)
                except (TypeError, ValueError):
                    pass
            stage_rows.append({
                "marker": f"E{index}",
                "name": item.get("name"), "command": item.get("command"),
                "status": effective_status,
                "started_at": display_timestamp(item.get("started_at")),
                "finished_at": display_timestamp(item.get("finished_at")),
                "duration_seconds": item.get("duration_seconds"),
                "quality_errors": item.get("quality_errors"),
                "warning": item.get("warning"),
                "description": item.get("description"),
                "serving_decision": item.get("serving_decision"),
                "approved_targets": item.get("approved_targets"),
                "experimental_targets": item.get("experimental_targets"),
                "restored_from_checkpoint": bool(item.get("restored_from_checkpoint", False)),
                "stdout_bytes": item.get("stdout_bytes", 0),
                "stderr_bytes": item.get("stderr_bytes", 0),
                "summary": summary, "log_path": item.get("log_path"),
            })
        resource_samples: list[dict[str, Any]] = []
        resource_path = self.resource_sampler.output
        if resource_path.exists():
            try:
                with resource_path.open("r", encoding="utf-8-sig") as handle:
                    for raw_line in handle:
                        try:
                            resource_samples.append(json.loads(raw_line))
                        except (TypeError, ValueError):
                            continue
            except OSError:
                resource_samples = list(self.state.get("resource_history") or [])
        if not resource_samples:
            resource_samples = list(self.state.get("resource_history") or [])
        event_records: list[dict[str, Any]] = []
        if self.events_path.exists():
            try:
                with self.events_path.open("r", encoding="utf-8-sig") as handle:
                    for raw_line in handle:
                        try:
                            event = json.loads(raw_line)
                            if isinstance(event, dict):
                                event_records.append(event)
                        except (TypeError, ValueError):
                            continue
            except OSError:
                event_records = []
        model_comparison = self.runtime / "reports" / "mlflow" / "model-comparison.json"
        model_comparison_payload: dict[str, Any] | None = None
        if model_comparison.exists():
            try:
                parsed_comparison = json.loads(
                    model_comparison.read_text(encoding="utf-8-sig")
                )
                if isinstance(parsed_comparison, dict):
                    model_comparison_payload = parsed_comparison
            except (OSError, ValueError, TypeError):
                model_comparison_payload = None
        recommendations: list[str] = []
        if self.state.get("status") != "success":
            recommendations.append("Usuń przyczynę błędu i uruchom automat z -Resume; zakończone etapy nie zostaną powtórzone.")
        if self.state.get("warnings"):
            recommendations.append("Przejrzyj ostrzeżenia i flagi jakości przed zatwierdzeniem nowych modeli.")
        if counters["errors"]:
            recommendations.append("Sprawdź błędy jakości według parametrów i stacji; nie usuwaj surowych rekordów bez analizy.")
        if self.state.get("status") == "success":
            recommendations.append("Sprawdź health/ready API oraz wykonaj jedno zapytanie kontrolne dla dokładnych współrzędnych.")
        return {
            "schema_version": "1.0", "generated_at": now(), "run_id": self.run_id,
            "status": self.state.get("status"), "profile": self.profile,
            "started_at": display_timestamp(self.state.get("started_at")),
            "finished_at": display_timestamp(self.state.get("finished_at")),
            "elapsed_seconds": self.state.get("elapsed_seconds"), "resume_count": self.state.get("resume_count", 0),
            "project_root": str(self.project), "runtime_root": str(self.runtime),
            "input_contract": self.state.get("input_contract"),
            "download_plan": self.state.get("download_plan"),
            "totals": counters, "stages": stage_rows,
            "warnings": self.state.get("warnings", []), "fatal_error": self.state.get("error"),
            "failure_diagnostic": self.state.get("failure_diagnostic"),
            "publication": self.state.get("publication"),
            "cleanup": self.state.get("cleanup"),
            "resource_summary": self.state.get("resource_summary"),
            "resource_samples": resource_samples,
            "trained_models": list(self.state.get("completed_models") or []),
            "model_comparison": model_comparison_payload,
            "events": event_records,
            # Pełny checkpoint jest dowodem przebiegu. Uproszczenie tabeli w UI
            # nigdy nie może usuwać danych źródłowych z raportu końcowego.
            "full_run_state": dict(self.state),
            "artifacts": {
                "run_status": str(self.status_path), "events": str(self.events_path),
                "model_comparison": str(model_comparison) if model_comparison.exists() else None,
                "serving_pointer": str(self.runtime / "object-store" / "serving" / "latest.json"),
                "resource_samples": str(resource_path),
            },
            "recommendations": recommendations,
        }

    @staticmethod
    def render_report_markdown(report: dict[str, Any]) -> str:
        totals = report["totals"]
        lines = [
            "# SmogAI — raport końcowy automatu", "",
            f"- **Run ID:** `{report['run_id']}`",
            f"- **Status:** `{report['status']}`",
            f"- **Profil:** `{report['profile']}`",
            f"- **Start:** {report.get('started_at')}",
            f"- **Koniec:** {report.get('finished_at')}",
            f"- **Wznowienia:** {report.get('resume_count', 0)}", "",
            "## Zakres", "", "```json",
            json.dumps(report.get("input_contract"), ensure_ascii=False, indent=2), "```", "",
            "## Łączne statystyki", "",
            "| downloaded | inserted | skipped | warnings | errors |", "|---:|---:|---:|---:|---:|",
            f"| {totals['downloaded']} | {totals['inserted']} | {totals['skipped']} | {totals['warnings']} | {totals['errors']} |", "",
            "## Etapy", "", "| E / etap i polecenie | Status | Start / koniec / trwanie | Jakość | Cele | Log |",
            "|---|---|---|---|---|---|",
        ]
        for item in report["stages"]:
            lines.append(
                f"| {item.get('marker')} {item['name']} / `{item['command']}` | {item['status']} | "
                f"{item.get('started_at') or '—'} / {item.get('finished_at') or '—'} / "
                f"{item.get('duration_seconds') or '—'} s | błędy: {item.get('quality_errors') or '—'}; "
                f"{item.get('warning') or '—'} | zatwierdzone: {', '.join(item.get('approved_targets') or []) or '—'}; "
                f"eksperymentalne: {', '.join(item.get('experimental_targets') or []) or '—'} | "
                f"`{item.get('log_path') or '—'}` |"
            )
        lines += ["", "## Publikacja", "", "```json",
                  json.dumps(report.get("publication"), ensure_ascii=False, indent=2), "```", ""]
        lines += ["## Modele szkolone i wybrane", "", "```json",
                  json.dumps({
                      "trained_models": report.get("trained_models"),
                      "model_comparison": report.get("model_comparison"),
                  }, ensure_ascii=False, indent=2), "```", ""]
        lines += ["## Sprzątanie i retencja", "", "```json",
                  json.dumps(report.get("cleanup"), ensure_ascii=False, indent=2), "```", ""]
        lines += ["## Telemetria zasobów", "", "```json",
                  json.dumps(report.get("resource_summary"), ensure_ascii=False, indent=2), "```", ""]
        if report.get("warnings"):
            lines += ["## Ostrzeżenia", ""] + [f"- {value}" for value in report["warnings"]] + [""]
        if report.get("fatal_error"):
            lines += ["## Błąd końcowy", "", f"`{report['fatal_error']}`", ""]
        if report.get("failure_diagnostic"):
            lines += ["## Szczegółowa diagnostyka błędu", "", "```json",
                      json.dumps(report["failure_diagnostic"], ensure_ascii=False, indent=2), "```", ""]
        lines += ["## Artefakty", ""] + [f"- **{key}:** `{value}`" for key, value in report["artifacts"].items() if value]
        lines += ["", "## Zalecane dalsze działania", ""] + [f"1. {value}" for value in report["recommendations"]]
        return "\n".join(lines) + "\n"

    @staticmethod
    def render_report_html(report: dict[str, Any]) -> str:
        e = lambda value: html.escape("" if value is None else str(value))

        def duration(value: Any) -> str:
            try:
                seconds = max(0, int(round(float(value))))
            except (TypeError, ValueError):
                return "—"
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        def joined(values: Any) -> str:
            return ", ".join(map(str, values or [])) or "—"

        rows = ""
        for item in report["stages"]:
            log_bytes = int(item.get("stdout_bytes", 0) or 0) + int(
                item.get("stderr_bytes", 0) or 0
            )
            checkpoint = (
                "<br><span class='checkpoint'>Wznowiono z checkpointu</span>"
                if item.get("restored_from_checkpoint")
                else ""
            )
            summary = e(json.dumps(item.get("summary") or {}, ensure_ascii=False, indent=2))
            rows += (
                "<tr>"
                f"<td><b>{e(item.get('marker'))} &nbsp; {e(item.get('name'))}</b>"
                f"<br><code>{e(item.get('command'))}</code>{checkpoint}</td>"
                f"<td><span class='status {e(item.get('status'))}'>{e(item.get('status'))}</span></td>"
                f"<td class='time'>Start: {e(item.get('started_at') or '—')}<br>"
                f"Koniec: {e(item.get('finished_at') or '—')}<br>"
                f"Trwanie: {e(duration(item.get('duration_seconds')))}</td>"
                f"<td>Błędy jakości: {e(item.get('quality_errors') if item.get('quality_errors') is not None else '—')}<br>"
                f"Ostrzeżenie: {e(item.get('warning') or '—')}</td>"
                f"<td>Zatwierdzone: {e(joined(item.get('approved_targets')))}<br>"
                f"Eksperymentalne: {e(joined(item.get('experimental_targets')))}</td>"
                f"<td>{e(item.get('description') or '—')}"
                f"<details><summary>Pełny wynik etapu</summary><pre>{summary}</pre></details></td>"
                f"<td>{log_bytes / 1024:.1f} KB<br><code>{e(item.get('log_path') or '—')}</code></td>"
                "</tr>"
            )

        trained_models = list(report.get("trained_models") or [])
        comparison = report.get("model_comparison") or {}
        active_models = [
            model for model in list(comparison.get("models") or [])
            if model.get("active")
        ]
        selected_current = [model for model in trained_models if model.get("selected")]
        selected_models = selected_current or active_models

        def model_table(models: list[dict[str, Any]], empty_text: str) -> str:
            if not models:
                return f"<p>{e(empty_text)}</p>"
            body = ""
            for model in models:
                metrics = model.get("metrics") or {}
                score = model.get("score")
                if score is None:
                    score = metrics.get("mae")
                version = model.get("model_version") or model.get("version") or "—"
                details = e(json.dumps(model, ensure_ascii=False, indent=2))
                body += (
                    "<tr>"
                    f"<td>{e(model.get('target') or '—')}</td>"
                    f"<td>{e(model.get('provider') or '—')}</td>"
                    f"<td>{e(model.get('status') or ('active' if model.get('active') else '—'))}</td>"
                    f"<td>{'tak' if model.get('selected') or model.get('active') else 'nie'}</td>"
                    f"<td>{e(score if score is not None else '—')}</td>"
                    f"<td><code>{e(version)}</code></td>"
                    f"<td>{e(model.get('quality_status') or metrics.get('quality_status') or '—')}"
                    f"<details><summary>Pełne dane</summary><pre>{details}</pre></details></td>"
                    "</tr>"
                )
            return (
                "<div class='table-scroll'><table class='models'><thead><tr>"
                "<th>Cel</th><th>Provider</th><th>Status</th><th>Wybrany</th>"
                "<th>Wynik/MAE</th><th>Wersja</th><th>Jakość i szczegóły</th>"
                f"</tr></thead><tbody>{body}</tbody></table></div>"
            )

        trained_models_html = model_table(
            trained_models,
            "Brak zapisanych kandydatów dla tego przebiegu.",
        )
        selected_models_html = model_table(
            selected_models,
            "Nie znaleziono informacji o wybranych lub aktywnych modelach.",
        )

        def parse_epoch(value: Any) -> float | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                return None

        def chart_svg(
            title: str,
            series: list[tuple[str, str, str, float]],
        ) -> str:
            samples = list(report.get("resource_samples") or [])
            points: list[tuple[float, dict[str, Any]]] = []
            for sample in samples:
                stamp = parse_epoch(sample.get("timestamp"))
                if stamp is not None:
                    points.append((stamp, sample))
            if len(points) < 2:
                return f"<section class='chart'><h3>{e(title)}</h3><p>Brak wystarczającej liczby próbek.</p></section>"
            points.sort(key=lambda value: value[0])
            # SVG remains readable and small even after a multi-day full run.
            if len(points) > 600:
                step = (len(points) - 1) / 599
                points = [points[round(index * step)] for index in range(600)]
            start, end = points[0][0], points[-1][0]
            span = max(1.0, end - start)
            width, height = 1100, 260
            left, right, top, bottom = 54, 18, 24, 38
            plot_width = width - left - right
            plot_height = height - top - bottom
            elements = [
                f"<rect x='{left}' y='{top}' width='{plot_width}' height='{plot_height}' fill='#091a28' stroke='#294052'/>",
            ]
            for tick in range(5):
                y = top + plot_height * tick / 4
                label = 100 - tick * 25
                elements.append(
                    f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_width}' y2='{y:.1f}' stroke='#294052'/>"
                    f"<text x='{left - 8}' y='{y + 4:.1f}' text-anchor='end' fill='#9fb5c7' font-size='11'>{label}%</text>"
                )
            legend: list[str] = []
            for label, key, color, divisor in series:
                raw_values: list[float] = []
                for _, sample in points:
                    try:
                        raw_values.append(float(sample.get(key, 0) or 0) / divisor)
                    except (TypeError, ValueError):
                        raw_values.append(0.0)
                maximum = max(raw_values) if raw_values else 0.0
                normalized = [value / maximum * 100 if maximum > 0 else 0 for value in raw_values]
                coordinates = []
                for (stamp, _), value in zip(points, normalized):
                    x = left + (stamp - start) / span * plot_width
                    y = top + (100 - value) / 100 * plot_height
                    coordinates.append(f"{x:.1f},{y:.1f}")
                elements.append(
                    f"<polyline points='{' '.join(coordinates)}' fill='none' stroke='{color}' stroke-width='2.2'/>"
                )
                legend.append(
                    f"<span><i style='background:{color}'></i>{e(label)} (max {maximum:.2f})</span>"
                )
            for stage in report.get("stages", []):
                stamp = parse_epoch(stage.get("started_at"))
                if stamp is None or stamp < start or stamp > end:
                    continue
                x = left + (stamp - start) / span * plot_width
                elements.append(
                    f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_height}' stroke='#cbd5e1' stroke-dasharray='3 4' opacity='.55'/>"
                    f"<text x='{x + 3:.1f}' y='{top + 12}' fill='#f8fafc' font-size='10'>{e(stage.get('marker'))}</text>"
                )
            return (
                f"<section class='chart'><h3>{e(title)}</h3>"
                f"<div class='legend'>{''.join(legend)}</div>"
                f"<svg viewBox='0 0 {width} {height}' role='img'>{''.join(elements)}</svg></section>"
            )

        charts = "".join(
            [
                chart_svg("CPU — względnie do maksimum serii", [
                    ("CPU procesu [%]", "process_cpu_percent", "#38bdf8", 1.0),
                    ("CPU systemu [%]", "system_cpu_percent", "#ef4444", 1.0),
                ]),
                chart_svg("Pamięć RAM — względnie do maksimum serii", [
                    ("RAM procesu [MB]", "process_ram_bytes", "#f59e0b", 1024**2),
                    ("RAM systemu [%]", "system_ram_used_percent", "#fb7185", 1.0),
                ]),
                chart_svg("Transfer dyskowy — względnie do maksimum serii", [
                    ("Odczyt [MB/s]", "disk_read_bps", "#06b6d4", 1024**2),
                    ("Zapis [MB/s]", "disk_write_bps", "#eab308", 1024**2),
                ]),
                chart_svg("Transfer sieciowy — względnie do maksimum serii", [
                    ("Pobieranie [MB/s]", "network_received_bps", "#22c55e", 1024**2),
                    ("Wysyłanie [MB/s]", "network_sent_bps", "#a78bfa", 1024**2),
                ]),
            ]
        )
        totals = report["totals"]
        warnings = "".join(f"<li>{e(value)}</li>" for value in report.get("warnings", [])) or "<li>brak</li>"
        recommendations = "".join(f"<li>{e(value)}</li>" for value in report["recommendations"])
        return f"""<!doctype html><html lang='pl'><head><meta charset='utf-8'><title>SmogAI {e(report['run_id'])}</title>
<style>body{{font:16px Segoe UI,Arial;background:#071827;color:#eaf4ff;margin:0}}main{{max-width:1500px;margin:auto;padding:32px}}.hero{{background:linear-gradient(135deg,#0d3348,#0b5c5c);padding:28px;border-radius:18px}}.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:18px 0}}.card{{background:#10283a;padding:16px;border-radius:12px}}.table-scroll{{overflow-x:auto;max-width:100%;border:1px solid #294052;border-radius:10px}}table{{min-width:1450px;width:100%;border-collapse:collapse;background:#0c2030}}th,td{{padding:10px;border:1px solid #294052;text-align:left;vertical-align:top}}th{{background:#17364a;position:sticky;top:0;z-index:2;white-space:nowrap}}td.time,td code{{white-space:nowrap}}code,pre{{background:#06121d;padding:3px 6px;border-radius:5px}}pre{{white-space:pre-wrap;padding:16px;overflow:auto}}h2{{margin-top:32px}}.status{{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700;white-space:nowrap;background:#334155}}.status.success,.status.passed,.status.ok{{background:#14532d;color:#dcfce7}}.status.running{{background:#1e3a8a;color:#dbeafe}}.status.warning,.status.partial{{background:#713f12;color:#fef3c7}}.status.failed,.status.error{{background:#7f1d1d;color:#fee2e2}}.checkpoint{{color:#facc15}}.chart{{background:#0c2030;border:1px solid #294052;border-radius:12px;padding:14px;margin:16px 0;overflow-x:auto}}.chart svg{{width:100%;min-width:760px;height:auto}}.legend{{display:flex;gap:18px;flex-wrap:wrap;color:#cbd5e1;font-size:13px}}.legend i{{display:inline-block;width:16px;height:4px;margin-right:6px;vertical-align:middle}}details{{margin-top:8px}}@media(max-width:800px){{.cards{{grid-template-columns:1fr 1fr}}}}</style></head><body><main>
<section class='hero'><h1>SmogAI — raport końcowy</h1><p>Run ID: <code>{e(report['run_id'])}</code></p><h2 class='{e(report['status'])}'>{e(report['status'])}</h2><p>Profil: {e(report['profile'])} · {e(report.get('started_at'))} → {e(report.get('finished_at'))}</p></section>
<section class='cards'>{''.join(f"<div class='card'><b>{e(k)}</b><br><big>{e(v)}</big></div>" for k,v in totals.items())}</section>
<h2>Kontrakt wejściowy</h2><pre>{e(json.dumps(report.get('input_contract'),ensure_ascii=False,indent=2))}</pre>
<h2>Plan pobierania danych</h2><pre>{e(json.dumps(report.get('download_plan'),ensure_ascii=False,indent=2))}</pre>
<h2>Etapy</h2><p>Znaczniki E1, E2… są wspólne dla tabeli i wykresów.</p><div class='table-scroll'><table><thead><tr><th>Etap / polecenie</th><th>Status</th><th>Czas</th><th>Jakość / ostrzeżenia</th><th>Cele</th><th>Szczegóły</th><th>Log</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Modele szkolone i kandydaci</h2>{trained_models_html}
<h2>Modele wybrane do użycia</h2>{selected_models_html}
<h2>Wykresy obciążenia</h2>{charts}
<h2>Publikacja</h2><pre>{e(json.dumps(report.get('publication'),ensure_ascii=False,indent=2))}</pre>
<h2>Sprzątanie i retencja</h2><pre>{e(json.dumps(report.get('cleanup'),ensure_ascii=False,indent=2))}</pre>
<h2>Telemetria zasobów</h2><pre>{e(json.dumps(report.get('resource_summary'),ensure_ascii=False,indent=2))}</pre>
<details><summary>Pełne próbki telemetrii ({len(report.get('resource_samples') or [])})</summary><pre>{e(json.dumps(report.get('resource_samples') or [],ensure_ascii=False,indent=2))}</pre></details>
<details><summary>Pełny dziennik zdarzeń ({len(report.get('events') or [])})</summary><pre>{e(json.dumps(report.get('events') or [],ensure_ascii=False,indent=2))}</pre></details>
<details><summary>Pełny stan checkpointu przebiegu</summary><pre>{e(json.dumps(report.get('full_run_state') or {{}},ensure_ascii=False,indent=2))}</pre></details>
<h2>Artefakty</h2><pre>{e(json.dumps(report.get('artifacts'),ensure_ascii=False,indent=2))}</pre>
<h2>Ostrzeżenia</h2><ul>{warnings}</ul>
<h2>Błąd końcowy</h2><p>{e(report.get('fatal_error') or 'brak')}</p>
<h2>Szczegółowa diagnostyka błędu</h2><pre>{e(json.dumps(report.get('failure_diagnostic'),ensure_ascii=False,indent=2))}</pre>
<h2>Dalsze działania</h2><ol>{recommendations}</ol>
</main></body></html>"""

    def write_final_report(self) -> dict[str, str]:
        report = self.final_report_data()
        report_dir = self.runtime / "reports" / "automation" / self.run_id
        json_path = report_dir / "summary.json"
        md_path = report_dir / "summary.md"
        html_path = report_dir / "summary.html"
        atomic_json(json_path, report)
        md_path.write_text(self.render_report_markdown(report), encoding="utf-8")
        html_path.write_text(self.render_report_html(report), encoding="utf-8")
        pointer = {"run_id": self.run_id, "status": report["status"], "generated_at": report["generated_at"],
                   "json": str(json_path), "markdown": str(md_path), "html": str(html_path)}
        atomic_json(self.runtime / "reports" / "automation" / "latest.json", pointer)
        return pointer

    def run(self) -> int:
        self.acquire()
        try:
            self.preflight()
            self.run_dir.mkdir(parents=True, exist_ok=True)
            prior = self.resume_state
            self.state = prior or {
                "schema_version": "1.3", "run_id": self.run_id, "profile": self.profile, "targets": self.targets,
                "fill_missing_ranges": self.fill_missing_ranges, "max_validation_errors": self.max_validation_errors,
                "status": "running",
                "started_at": now(), "started_epoch": time.time(), "project_root": str(self.project), "runtime_root": str(self.runtime),
                "current_stage": "Preflight", "current_stage_index": 0, "stage_count": len(self.stages), "overall_percent": 0.0,
                "stage_percent": 0.0, "current_task": "kontrola konfiguracji", "eta_seconds": None,
                "download_plan": {
                    "gios_current": not self.skip_gios_current,
                    "imgw_current": not self.skip_imgw_current,
                    "history_enabled": self.fill_missing_ranges,
                    "parameters": self.parameters or "z aktywną rolą collect_current",
                    "start": self.data_start,
                    "end": self.data_end,
                    "note": "Zakres dat dotyczy audytu i historii; endpointy bieżące zwracają najnowszy dostępny pakiet.",
                },
                "stages": [{"name": s.name, "command": s.command, "description": s.description, "status": "pending"} for s in self.stages],
            }
            if prior:
                self.state["stages"] = self.reconcile_resume_stages(prior)
                self.state.update(
                    status="running", resumed_at=now(), finished_at=None, error=None,
                    current_stage="Wznowienie z checkpointu", current_task="wyszukiwanie pierwszego niezakończonego etapu",
                    stage_count=len(self.stages),
                )
                self.state["resume_count"] = int(self.state.get("resume_count", 0)) + 1
                self.event("INFO", "Wznowiono przebieg z checkpointu", run_id=self.run_id,
                           resume_count=self.state["resume_count"])
            self.state["input_contract"] = {
                "profile": self.profile, "targets": self.targets,
                "experimental_targets": self.experimental_targets,
                "training_start": self.training_start,
                "training_end": self.training_end,
                "fill_missing_ranges": self.fill_missing_ranges,
                "parameters": self.parameters, "data_start": self.data_start, "data_end": self.data_end,
                "skip_gios_current": self.skip_gios_current,
                "skip_imgw_current": self.skip_imgw_current,
                "max_validation_errors": self.max_validation_errors,
                "skip_cleanup": self.skip_cleanup,
                "keep_training_quick": self.cleanup_policy["training_quick"],
                "keep_training_full": self.cleanup_policy["training_full"],
                "keep_dashboard_snapshots": self.cleanup_policy["dashboard_snapshots"],
                "keep_forecast_publications": self.cleanup_policy["forecast_publications"],
                "keep_map_surface_sets": self.cleanup_policy["map_surface_sets"],
                "keep_automation_runs": self.cleanup_policy["automation_runs"],
                "progress_retention_days": self.cleanup_policy["progress_days"],
                "incomplete_snapshot_hours": self.cleanup_policy["incomplete_snapshot_hours"],
                "resource_sample_seconds": self.resource_sample_seconds,
            }
            self.save()
            total = sum(s.weight for s in self.stages)
            done = 0.0
            for i, stage in enumerate(self.stages):
                self.run_stage(i, stage, done, total)
                done += stage.weight
            self.publication_check()
            if not self.skip_cleanup:
                self.state["current_stage"] = "Sprzątanie po poprawnym przebiegu"
                self.state["current_task"] = "retencja snapshotów, publikacji, map i logów"
                self.save()
                cleanup = cleanup_runtime(
                    self.runtime, apply=True, policy=self.cleanup_policy,
                    current_run_id=self.run_id,
                )
                self.state["cleanup"] = {
                    key: cleanup.get(key) for key in (
                        "status", "mode", "policy", "candidate_count", "deleted_count",
                        "bytes_reclaimable", "bytes_freed", "gib_reclaimable", "gib_freed",
                        "errors", "report_path",
                    )
                }
                if cleanup.get("errors"):
                    self.state.setdefault("warnings", []).append(
                        f"Sprzątanie zakończono częściowo: {len(cleanup['errors'])} błędów."
                    )
            self.state.update(status="success", finished_at=now(), overall_percent=100.0, stage_percent=100.0, current_stage="Zakończono", current_task="pełny proces zakończony")
            self.state["final_report"] = self.write_final_report()
            self.save()
            print(f"Raport HTML: {self.state['final_report']['html']}")
            print(f"Raport JSON: {self.state['final_report']['json']}")
            return 0
        except Exception as exc:
            if not self.state:
                self.state = {"schema_version": "1.0", "run_id": self.run_id, "profile": self.profile, "started_at": now()}
            self.state.update(status="failed", finished_at=now(), error=str(exc))
            self.event("ERROR", str(exc))
            try:
                self.state["final_report"] = self.write_final_report()
                print(f"Raport częściowy HTML: {self.state['final_report']['html']}")
                print(f"Raport częściowy JSON: {self.state['final_report']['json']}")
            except Exception as report_exc:
                self.state["report_error"] = str(report_exc)
            self.save()
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="SmogAI HF21 local automation")
    ap.add_argument("--project-root", help="Domyślnie bieżący katalog roboczy")
    ap.add_argument("--runtime-root", default=r"C:\ProgramData\SmogAI")
    ap.add_argument("--config")
    ap.add_argument("--env-file")
    ap.add_argument("--profile", choices=("quick", "normal", "medium", "full"), default="quick")
    ap.add_argument("--targets", help="Lista celów oddzielona przecinkami, np. PM10,PM2.5,NO2")
    ap.add_argument(
        "--experimental-targets",
        help=(
            "Opcjonalna lista aktywnych celów eksperymentalnych. Domyślnie "
            "publikowane są wszystkie; podaj 'none', aby je jawnie wyłączyć."
        ),
    )
    ap.add_argument("--fill-missing-ranges", action="store_true", help="Jawnie uruchom pobieranie brakującej historii")
    ap.add_argument("--parameters", help="Parametry danych, np. PM10,PM2.5,NO2,temperature_c")
    ap.add_argument("--data-start", help="Początek audytu/historii w ISO 8601")
    ap.add_argument("--data-end", help="Koniec audytu/historii w ISO 8601")
    ap.add_argument("--training-start", help="Włączny początek treningu w ISO 8601")
    ap.add_argument("--training-end", help="Wyłączny koniec treningu w ISO 8601")
    ap.add_argument(
        "--resource-sample-seconds",
        type=float,
        default=5.0,
        help="Interwał telemetrii CPU/RAM/dysk/sieć; 0 wyłącza",
    )
    ap.add_argument("--skip-gios-current", action="store_true")
    ap.add_argument("--skip-imgw-current", action="store_true")
    ap.add_argument("--max-validation-errors", type=int, default=100,
                    help="Maksymalna liczba błędów jakości przy kodzie 4; -1 = bez limitu")
    ap.add_argument("--run-id")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-cleanup", action="store_true", help="Pomiń automatyczną retencję po pełnym sukcesie")
    ap.add_argument("--keep-training-quick", type=int, default=2)
    ap.add_argument("--keep-training-full", type=int, default=3)
    ap.add_argument("--keep-dashboard-snapshots", type=int, default=5)
    ap.add_argument("--keep-forecast-publications", type=int, default=10)
    ap.add_argument("--keep-map-surface-sets", type=int, default=5)
    ap.add_argument("--keep-automation-runs", type=int, default=30)
    ap.add_argument("--progress-retention-days", type=int, default=30)
    ap.add_argument("--incomplete-snapshot-hours", type=int, default=24)
    ap.add_argument("--cleanup-only", action="store_true")
    ap.add_argument("--cleanup-dry-run", action="store_true")
    ns = ap.parse_args()
    if ns.cleanup_only:
        policy = {
            "training_quick": ns.keep_training_quick,
            "training_full": ns.keep_training_full,
            "dashboard_snapshots": ns.keep_dashboard_snapshots,
            "forecast_publications": ns.keep_forecast_publications,
            "map_surface_sets": ns.keep_map_surface_sets,
            "automation_runs": ns.keep_automation_runs,
            "progress_days": ns.progress_retention_days,
            "incomplete_snapshot_hours": ns.incomplete_snapshot_hours,
        }
        result = cleanup_runtime(
            Path(ns.runtime_root), apply=not ns.cleanup_dry_run, policy=policy
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("errors") else 4
    return Runner(ns).run()


if __name__ == "__main__":
    raise SystemExit(main())
