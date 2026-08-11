#!/usr/bin/env python3
"""Offline release gate for GIOŚ/IMGW Forecast Suite 1.7.0.

No external API, DigitalOcean, LLM or Langfuse request is performed.  The gate
checks the package structure, the architectural invariant (all ML prediction is
local; the server may only apply deterministic exact-point IDW/PCHIP), portable
Windows scripts, DigitalOcean specs, a seeded FastAPI query, tests and wheel
contents.
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

EXPECTED_VERSION = "1.7.0"
FIXED_CHECKOUT_PATTERNS = {
    r"C:\\SmogAI": re.compile(r"C:\\SmogAI", re.IGNORECASE),
    "C:/SmogAI": re.compile(r"C:/SmogAI", re.IGNORECASE),
    "/opt/smog-ai": re.compile(r"/opt/smog-ai", re.IGNORECASE),
}
RUNTIME_ENV_PREFIXES = ("SMOG_AI_", "SPACES_", "LANGFUSE_", "AWS_")
RUNTIME_ENV_KEYS = {
    "DISPLAY_TIMEZONE",
    "PUBLISH_API_URL",
    "PUBLISH_API_TOKEN",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "PYTHONPATH",
    "PYTHONHOME",
}
REQUIRED_FILES = (
    "smog_ai/spatial/contracts.py",
    "smog_ai/spatial/grid.py",
    "smog_ai/spatial/interpolation.py",
    "smog_ai/spatial/service.py",
    "smog_ai/places/gazetteer.py",
    "smog_ai/resources/poland_boundary.geojson",
    "smog_ai/resources/polish_places.csv",
    "server/application/spatial_source.py",
    "server/application/query.py",
    "server/dashboard/app.py",
    "docs/SPATIAL_FORECAST_MAP.md",
    "docs/DIGITALOCEAN_SPACES_KRAKOW_STEP_BY_STEP.md",
    "docs/STEP_BY_STEP_LOCAL_WINDOWS_AND_DIGITALOCEAN_PL.md",
    "docs/PYTHON_BOOTSTRAP_WINDOWS.md",
    "docs/RELEASE_NOTES_1.7.0.md",
    "docs/platform/TECHNICAL_PROCESSING_PL.md",
    "docs/platform/MATHEMATICAL_MODEL_PL.md",
    "docs/platform/MODEL_PLUGIN_GUIDE_PL.md",
    "docs/latex/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex",
    "docs/latex/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex",
    "docs/pdf/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.pdf",
    "docs/pdf/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.pdf",
    "smog_ai/resources/docs/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex",
    "smog_ai/hourly/features.py",
    "smog_ai/hourly/trainer.py",
    "smog_ai/hourly/predictor.py",
    "smog_ai/modeling/contracts.py",
    "smog_ai/modeling/registry.py",
    "smog_ai/modeling/providers.py",
    "smog_ai/collectors/imgw_archive.py",
    "smog_ai/resources/imgw_synop_terminowe_header.csv",
    "migrations/versions/0002_weather_precipitation_period.py",
    "docs/TEST_DATABASE_ISOLATION_AND_RECOVERY_PL.md",
    "scripts/Prepare-Python.ps1",
    "scripts/Diagnose-Python.ps1",
    "scripts/Test-PytestIsolated.ps1",
    "scripts/Repair-TestContaminatedDatabase.ps1",
    "scripts/audit_and_rebuild_test_contaminated_db.py",
    "examples/spatial_manifest.example.json",
    "examples/query_response_katowice.example.json",
    "examples/custom_model_plugin.py",
)


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    duration_seconds: float
    details: str


class ReleaseGate:
    def __init__(self, root: Path, *, skip_tests: bool, skip_wheel: bool) -> None:
        self.root = root.resolve()
        self.skip_tests = skip_tests
        self.skip_wheel = skip_wheel
        self.results: list[CheckResult] = []

    def check(self, name: str, action: Callable[[], object]) -> None:
        started = time.perf_counter()
        try:
            details = action()
        except Exception as exc:  # noqa: BLE001 - aggregate all release failures
            self.results.append(
                CheckResult(name, "failed", time.perf_counter() - started, str(exc))
            )
            return
        self.results.append(
            CheckResult(name, "passed", time.perf_counter() - started, str(details or "ok"))
        )

    @staticmethod
    def isolated_environment(source: dict[str, str] | None = None) -> dict[str, str]:
        candidate = dict(source or os.environ)
        for key in list(candidate):
            if key in RUNTIME_ENV_KEYS or key.startswith(RUNTIME_ENV_PREFIXES):
                candidate.pop(key, None)
        candidate["SMOG_AI_ENV"] = "test"
        candidate["PYTHONNOUSERSITE"] = "1"
        return candidate

    def run(self, command: Sequence[str], *, env: dict[str, str] | None = None) -> str:
        process = subprocess.run(
            list(command),
            cwd=self.root,
            env=self.isolated_environment(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = process.stdout.strip()
        if process.returncode != 0:
            raise RuntimeError(
                f"command failed with code {process.returncode}: {' '.join(command)}\n{output}"
            )
        return output

    def verify_metadata_and_files(self) -> str:
        pyproject = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]
        if version != EXPECTED_VERSION:
            raise RuntimeError(f"pyproject version is {version}, expected {EXPECTED_VERSION}")
        init_text = (self.root / "smog_ai" / "__init__.py").read_text(encoding="utf-8")
        if f'__version__ = "{EXPECTED_VERSION}"' not in init_text:
            raise RuntimeError("smog_ai.__version__ is inconsistent")
        missing = [item for item in REQUIRED_FILES if not (self.root / item).is_file()]
        if missing:
            raise RuntimeError(f"required files missing: {missing}")
        places_lines = (self.root / "smog_ai/resources/polish_places.csv").read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if len(places_lines) < 100:
            raise RuntimeError("Polish gazetteer is unexpectedly small")
        boundary = json.loads(
            (self.root / "smog_ai/resources/poland_boundary.geojson").read_text(
                encoding="utf-8"
            )
        )
        if boundary.get("type") != "FeatureCollection" or not boundary.get("features"):
            raise RuntimeError("invalid Poland boundary GeoJSON")
        return f"version={version}, required_files={len(REQUIRED_FILES)}, places={len(places_lines)-1}"

    def parse_configuration_files(self) -> str:
        toml_files = [self.root / "pyproject.toml", self.root / ".streamlit/config.toml"]
        for path in toml_files:
            tomllib.loads(path.read_text(encoding="utf-8"))
        yaml_files = [
            self.root / "config.example.yaml",
            self.root / ".do/app.yaml",
            self.root / ".do/app.dev.yaml",
            self.root / ".github/workflows/ci-deploy-digitalocean.yml",
        ]
        for path in yaml_files:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"YAML root is not a mapping: {path}")
        xml_files = sorted((self.root / "scheduled-tasks").glob("*.xml"))
        if len(xml_files) != 4:
            raise RuntimeError(f"expected four scheduled-task XML files, found {len(xml_files)}")
        for path in xml_files:
            ET.parse(path)
            text = path.read_text(encoding="utf-16")
            for placeholder in ("__PROJECT_ROOT__", "__RUNTIME_ROOT__", "__TASK_USER__"):
                if placeholder not in text:
                    raise RuntimeError(f"{path.name} is missing {placeholder}")
        return f"TOML={len(toml_files)}, YAML={len(yaml_files)}, XML={len(xml_files)}"

    def verify_portability(self) -> str:
        inspected = [
            *sorted((self.root / "scripts").glob("*.ps1")),
            *sorted((self.root / "scheduled-tasks").glob("*.xml")),
        ]
        violations: list[str] = []
        for path in inspected:
            encoding = "utf-16" if path.suffix.lower() == ".xml" else "utf-8-sig"
            text = path.read_text(encoding=encoding)
            for label, pattern in FIXED_CHECKOUT_PATTERNS.items():
                if pattern.search(text):
                    violations.append(f"{path.relative_to(self.root)} contains {label}")
        if violations:
            raise RuntimeError("fixed checkout paths detected:\n" + "\n".join(violations))
        common = (self.root / "scripts/SmogAi.Common.ps1").read_text(encoding="utf-8-sig")
        required = ("$PSScriptRoot", "SMOG_AI_PROJECT_ROOT", "Resolve-SmogAiProjectRoot")
        missing = [fragment for fragment in required if fragment not in common]
        if missing:
            raise RuntimeError(f"portable root resolver missing fragments: {missing}")
        return f"inspected={len(inspected)} files"


    def verify_python_bootstrap_contract(self) -> str:
        pyproject = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        if pyproject["project"].get("requires-python") != ">=3.12,<3.14":
            raise RuntimeError("requires-python must support Python 3.12 and 3.13")
        common = (self.root / "scripts/SmogAi.Common.ps1").read_text(encoding="utf-8-sig")
        installer = (self.root / "scripts/Install-Local.ps1").read_text(encoding="utf-8-sig")
        setup = (self.root / "scripts/Setup-All.ps1").read_text(encoding="utf-8-sig")
        prepare = (self.root / "scripts/Prepare-Python.ps1").read_text(encoding="utf-8-sig")
        required_common = (
            "Invoke-SmogAiPythonProbe",
            "smog-ai-python-probe-",
            "Search-SmogAiBootstrapPython",
            "Find-SmogAiBootstrapPython",
            "Test-SmogAiSupportedPythonVersion",
            "Install-SmogAiPythonWithWinget",
            "Python.Python.$VersionToInstall",
            "active Conda:",
            "py.exe inventory:",
            "where.exe:",
            "-1978335189",
            "3.13",
        )
        missing = [fragment for fragment in required_common if fragment not in common]
        if missing:
            raise RuntimeError(f"Python bootstrap is missing fragments: {missing}")
        if "Find-SmogAiPython312" in common:
            raise RuntimeError("stale Python-3.12-only resolver remains")
        required_installer = (
            "PreferredPythonVersion",
            "NoAutomaticPythonInstall",
            "RecreateVenv",
            "$VenvPath.backup-",
        )
        missing = [fragment for fragment in required_installer if fragment not in installer]
        if missing:
            raise RuntimeError(f"Install-Local is missing bootstrap options: {missing}")
        diagnose_path = self.root / "scripts/Diagnose-Python.ps1"
        if "PythonExecutable" not in setup or "Find-SmogAiBootstrapPython" not in prepare:
            raise RuntimeError("Setup/Prepare-Python do not expose the bootstrap resolver")
        if not diagnose_path.is_file():
            raise RuntimeError("Diagnose-Python.ps1 is missing")
        diagnose = diagnose_path.read_text(encoding="utf-8-sig")
        if "Get-SmogAiPythonCandidates" not in diagnose or "Invoke-SmogAiPythonProbe" not in diagnose:
            raise RuntimeError("Diagnose-Python does not enumerate and probe candidates")
        if "-c $Probe" in common:
            raise RuntimeError("legacy multiline python -c probe remains")
        return "supported=3.12,3.13; conda-first; temp-file-probe; winget-rescan; diagnostics=yes"

    def verify_powershell_encoding(self) -> str:
        scripts = sorted((self.root / "scripts").glob("*.ps1"))
        if not scripts:
            raise RuntimeError("no PowerShell scripts found")
        violations: list[str] = []
        for path in scripts:
            payload = path.read_bytes()
            relative = path.relative_to(self.root)
            if not payload.startswith(b"\xef\xbb\xbf"):
                violations.append(f"{relative}: missing UTF-8 BOM")
            try:
                payload.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                violations.append(f"{relative}: invalid UTF-8 ({exc})")
            body = payload[3:] if payload.startswith(b"\xef\xbb\xbf") else payload
            if b"\n" in body.replace(b"\r\n", b""):
                violations.append(f"{relative}: contains LF not preceded by CR")
        if violations:
            raise RuntimeError("PowerShell encoding contract failed:\n" + "\n".join(violations))
        return f"UTF-8 BOM + CRLF={len(scripts)} scripts"

    def verify_document_contract(self) -> str:
        required_docs = {
            "README.md": (
                "1.7.0",
                "DigitalOcean Spaces",
                "h=1",
                "ModelProvider",
                "interpol",
                "App Platform",
                "nie uruchamia",
                "Python 3.13",
                "winget",
            ),
            "docs/ARCHITECTURE.md": (
                "lokalnie",
                "ModelProvider",
                "target_time",
                "FastAPI",
            ),
            "docs/platform/TECHNICAL_PROCESSING_PL.md": (
                "h=1", "temperature_c", "precipitation_mm", "out-of-fold", "Spaces"
            ),
            "docs/platform/MATHEMATICAL_MODEL_PL.md": (
                "h=1..48", "hurdle", "PCHIP", "IDW"
            ),
            "docs/platform/MODEL_PLUGIN_GUIDE_PL.md": (
                "ModelProvider", "entry point", "external_factories"
            ),
            "docs/SPATIAL_FORECAST_MAP.md": (
                "IDW",
                "EPSG:2180",
                "pewność",
                "PyDeck",
            ),
            "docs/DIGITALOCEAN_SPACES_KRAKOW_STEP_BY_STEP.md": (
                "fra1",
                "smog-ai/krakow/production",
                "Restricted",
            ),
            "docs/TEST_DATABASE_ISOLATION_AND_RECOVERY_PL.md": (
                "Engine(sqlite:///C:/ProgramData/SmogAI/data/smog.db)",
                "SQLite Online Backup",
                "Test-PytestIsolated.ps1",
                "-Rebuild",
            ),
        }
        for relative, phrases in required_docs.items():
            text = (self.root / relative).read_text(encoding="utf-8")
            missing = [phrase for phrase in phrases if phrase.casefold() not in text.casefold()]
            if missing:
                raise RuntimeError(f"{relative} is missing required documentation phrases: {missing}")
        forbidden = ("DIGITALOCEAN_DB_CLUSTER_NAME", "smog-customer-acme-db")
        violations: list[str] = []
        for path in [self.root / "README.md", *sorted((self.root / "docs").glob("*.md"))]:
            text = path.read_text(encoding="utf-8")
            for phrase in forbidden:
                if phrase in text:
                    violations.append(f"{path.relative_to(self.root)} contains {phrase!r}")
        if violations:
            raise RuntimeError("stale architecture text:\n" + "\n".join(violations))
        return f"checked={len(required_docs)} critical documents"

    def verify_architecture_invariant(self) -> str:
        server_files = [
            path
            for path in (self.root / "server").rglob("*.py")
            if "__pycache__" not in path.parts
        ]
        forbidden_patterns = (
            re.compile(r"\.predict\s*\("),
            re.compile(r"from\s+sklearn"),
            re.compile(r"import\s+sklearn"),
            re.compile(r"smog_ai\.training"),
            re.compile(r"create_spatial_interpolator"),
        )
        violations: list[str] = []
        for path in server_files:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                if pattern.search(text):
                    violations.append(f"{path.relative_to(self.root)} matches {pattern.pattern}")
        if violations:
            raise RuntimeError(
                "App Platform code performs forbidden model/training work:\n"
                + "\n".join(violations)
            )
        query = (self.root / "server/application/query.py").read_text(encoding="utf-8")
        dashboard = (self.root / "server/dashboard/app.py").read_text(encoding="utf-8")
        api = (self.root / "server/api/main.py").read_text(encoding="utf-8")
        required_query = (
            "published_station_forecasts_exact_point_idw",
            "precomputed-local-results",
            "SpatialSource",
        )
        required_map = ("GeoJsonLayer", "ColumnLayer", "ScatterplotLayer", "TextLayer", "Wysokość 3D", "Nazwy miast", "Model i jakość", "Jak to działa", "temperature_c", "precipitation_mm")
        required_api = (
            "/api/v1/spatial/manifest",
            "/api/v1/spatial/surface",
            "/api/v1/places/search",
            "/api/v1/models",
            "/api/v1/docs/processing",
            "/api/v1/docs/mathematics",
            "published_station_forecasts_exact_point",
        )
        for label, text, fragments in (
            ("query", query, required_query),
            ("dashboard", dashboard, required_map),
            ("api", api, required_api),
        ):
            missing = [fragment for fragment in fragments if fragment not in text]
            if missing:
                raise RuntimeError(f"{label} is missing architecture fragments: {missing}")
        pipeline = (self.root / "smog_ai/pipeline.py").read_text(encoding="utf-8")
        if pipeline.index('"predict"') > pipeline.index('"build_spatial_surfaces"'):
            raise RuntimeError("spatial surfaces must be built after local station forecasts")
        return f"server_files={len(server_files)}, inference=published-stations-idw-pchip"

    def verify_hourly_model_platform(self) -> str:
        program = r'''
from pathlib import Path
import yaml
from smog_ai.modeling import create_model_registry
from smog_ai.config import HourlyForecastingConfig
root = Path.cwd()
payload = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
hourly = payload["hourly_forecasting"]
assert hourly["enabled"] is True
assert hourly["minimum_horizon_hours"] == 1
assert hourly["maximum_horizon_hours"] == 48
assert set(hourly["targets"]) == {"PM10", "PM2.5", "temperature_c", "precipitation_mm"}
assert hourly["exact_target_time_required"] is True
assert hourly["allow_temporal_extrapolation"] is False
settings = HourlyForecastingConfig(**hourly)
assert settings.model_horizons_hours == list(range(1, 61))
assert settings.serving_horizons_hours == list(range(1, 49))
registry = create_model_registry(load_entry_points=False)
required = {
    "persistence", "historical_mean", "ridge", "polynomial_ridge",
    "hist_gradient_boosting", "hist_gradient_boosting_quantile", "mlp",
    "hurdle_hist_gradient_boosting",
}
assert required.issubset(set(registry.names()))
print({"horizons": len(settings.horizons_hours), "targets": settings.targets, "providers": len(registry.names())})
'''
        return self.run([sys.executable, "-c", program])

    def verify_spatial_smoke(self) -> str:
        program = r'''
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pandas as pd
from shapely.geometry import Point, shape
from smog_ai.spatial.grid import create_poland_grid, load_boundary_geojson
from smog_ai.spatial.interpolation import IDWInterpolator
root = Path.cwd()
boundary = load_boundary_geojson(root / "smog_ai/resources/poland_boundary.geojson")
grid = create_poland_grid(boundary, projected_crs="EPSG:2180", resolution_km=45)
assert len(grid.frame) > 40
polygon = shape(boundary["features"][0]["geometry"])
assert all(polygon.contains(Point(row.longitude, row.latitude)) for row in grid.frame.itertuples())
stations = pd.DataFrame([
 {"station_id": 1, "latitude": 50.0614, "longitude": 19.9383, "predicted_value": 39.0},
 {"station_id": 2, "latitude": 50.2649, "longitude": 19.0238, "predicted_value": 44.0},
 {"station_id": 3, "latitude": 51.1079, "longitude": 17.0385, "predicted_value": 27.0},
 {"station_id": 4, "latitude": 52.2297, "longitude": 21.0122, "predicted_value": 33.0},
])
origin = datetime(2026, 8, 1, 6, tzinfo=UTC)
surface, metrics = IDWInterpolator(nearest_stations=4, minimum_stations=3, maximum_distance_km=700).interpolate(
 grid=grid, stations=stations, parameter="PM10", horizon_hours=24,
 origin_time=origin, target_time=origin + timedelta(hours=24),
)
assert surface["value"].dropna().ge(0).all()
assert surface["confidence"].between(0, 1).all()
assert metrics["loo_count"] == 4
print({"cells": len(surface), "loo_mae": metrics["loo_mae"]})
'''
        return self.run([sys.executable, "-c", program])

    def verify_windows_runtime_regressions(self) -> str:
        local_api = (self.root / "scripts/Start-LocalApi.ps1").read_text(encoding="utf-8-sig")
        if "--forwarded-allow-ips=127.0.0.1" not in local_api:
            raise RuntimeError("local FastAPI launcher lacks the safe single uvicorn argument")
        if re.search(r"--forwarded-allow-ips[\"\']?\s*[, ]\s*[\"\']\*[\"\']", local_api):
            raise RuntimeError("local FastAPI launcher contains a standalone wildcard argument")

        backup = (self.root / "smog_ai/monitoring/backup.py").read_text(encoding="utf-8")
        required_backup = (
            "closing(sqlite3.connect(source))",
            "closing(sqlite3.connect(uncompressed))",
            "_unlink_with_retry(uncompressed)",
        )
        missing = [fragment for fragment in required_backup if fragment not in backup]
        if missing:
            raise RuntimeError(f"Windows-safe SQLite backup fragments are missing: {missing}")

        for relative in (
            "docs/pdf/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.pdf",
            "docs/pdf/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.pdf",
        ):
            payload = (self.root / relative).read_bytes()
            if not payload.startswith(b"%PDF-") or len(payload) < 20_000:
                raise RuntimeError(f"invalid or unexpectedly small PDF: {relative}")
        return "uvicorn wildcard=guarded; SQLite handles=closed; PDFs=2"

    def verify_compile(self) -> str:
        for path in (self.root / "smog_ai", self.root / "server", self.root / "migrations"):
            if not compileall.compile_dir(path, quiet=1, force=True):
                raise RuntimeError(f"compileall failed for {path}")
        for path in (
            self.root / "scripts/validate_digitalocean_spec.py",
            self.root / "scripts/verify_release.py",
        ):
            if not compileall.compile_file(path, quiet=1, force=True):
                raise RuntimeError(f"compileall failed for {path}")
        return "Python compilation succeeded"

    def verify_fastapi_smoke(self) -> str:
        program = r'''
import gzip
import hashlib
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

with tempfile.TemporaryDirectory(prefix="smog-ai-api-") as directory:
    root = Path(directory)
    os.environ.update({
        "SMOG_AI_ENV": "development",
        "SMOG_AI_SERVER_STORAGE_BACKEND": "object_store",
        "SMOG_AI_SERVER_UPLOADS_ENABLED": "false",
        "SMOG_AI_SERVER_DOCS_ENABLED": "true",
        "SMOG_AI_SERVER_DATA_DIR": str(root / "server"),
        "SMOG_AI_OBJECT_STORE_BACKEND": "local",
        "SMOG_AI_OBJECT_STORE_LOCAL_ROOT": str(root / "objects"),
        "SMOG_AI_OBJECT_STORE_PREFIX": "release-smoke",
        "SMOG_AI_LLM_PROVIDER": "rule_based",
        "SMOG_AI_OBSERVABILITY_BACKEND": "none",
        "SMOG_AI_SPATIAL_ENABLED": "true",
    })
    from smog_ai.artifacts.repository import ArtifactRepository, canonical_json_bytes
    from smog_ai.config import ObjectStorageConfig
    from smog_ai.storage.factory import create_object_store
    repository = ArtifactRepository(create_object_store(ObjectStorageConfig(enabled=True, backend="local", local_root=root / "objects")))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    target = now + timedelta(hours=24)
    snapshot = {
      "metadata": {"publication_id":"release-smoke","generated_at":now.isoformat(),"schema_version":"1.1","model_version":"smoke-v1","checksum":"0"*64},
      "stations": [{"station_id":1,"station_name":"Katowice test","city_name":"Katowice","latitude":50.2649,"longitude":19.0238,"measurements":{"PM10":{"value":34.0,"measurement_time":now.isoformat()}},"weather":{"temperature":18.0}}],
      "forecasts": [], "metrics": [], "quality_summary": {}
    }
    body = gzip.compress(canonical_json_bytes(snapshot), mtime=0)
    repository.publish_snapshot(compressed=body, publication_id="release-smoke", checksum=hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(), metadata=snapshot["metadata"])
    surface = {
      "schema_version":"1.0","surface_id":"release-surface","parameter":"PM10","horizon_hours":24,
      "origin_time":now.isoformat(),"target_time":target.isoformat(),"generated_at":now.isoformat(),
      "model_versions":["smoke-v1"],"metadata":{"server_computation":"exact_point_idw_from_published_station_forecasts","grid_resolution_km":8.0,"spatial_method":"quality_weighted_idw","distance_power":2.0,"distance_smoothing_m":100.0,"exact_station_threshold_m":10.0,"projected_crs":"EPSG:2180"},
      "metrics":{"algorithm":"idw"},
      "stations":[{"station_id":1,"station_name":"Katowice test","city_name":"Katowice","latitude":50.2649,"longitude":19.0238,"predicted_value":41.0,"model_version":"smoke-v1"}],
      "grid":[{"cell_id":"katowice","row":0,"column":0,"latitude":50.2649,"longitude":19.0238,"value":39.5,"confidence":0.91,"nearest_station_distance_km":0.0,"stations_used":1,"local_station_spread":0.0,"quality_flag":"ok","parameter":"PM10","horizon_hours":24,"origin_time":now.isoformat(),"target_time":target.isoformat(),"color_r":255,"color_g":181,"color_b":50,"color_a":228}]
    }
    stored = repository.put_gzip_json(repository.layout.spatial_surface("release-set","PM10",24), surface, immutable=True)
    manifest = {"schema_version":"1.0","surface_set_id":"release-set","generated_at":now.isoformat(),"algorithm":"idw","grid_resolution_km":8.0,"parameters":["PM10"],"horizons_hours":[24],"surfaces":[{"surface_id":"release-surface","parameter":"PM10","horizon_hours":24,"origin_time":now.isoformat(),"target_time":target.isoformat(),"object_key":stored.key,"checksum":stored.checksum}]}
    manifest_artifact = repository.put_json(repository.layout.spatial_manifest("release-set"), manifest, immutable=True)
    repository.put_json(repository.layout.latest_spatial_pointer, {"surface_set_id":"release-set","manifest_key":manifest_artifact.key,"generated_at":now.isoformat()}, immutable=False)
    repository.put_json(repository.layout.spatial_boundary, {"type":"FeatureCollection","features":[]}, immutable=False)

    from fastapi.testclient import TestClient
    from server.api.main import app
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200, health.text
        assert health.json()["prediction_mode"] == "published_station_forecasts_exact_point"
        local_hour = target.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%H:%M")
        query = client.post(
            "/api/v1/query",
            json={
                "text": f"Jutro o {local_hour} sprawdź PM10.",
                "place_name": "Katowice test",
                "latitude": 50.2649,
                "longitude": 19.0238,
                "location_source": "exact_coordinates",
            },
        )
        assert query.status_code == 200, query.text
        result = query.json()
        assert result["place"]["name"] == "Katowice test"
        assert result["forecasts"][0]["prediction_source"] == "published_station_forecasts_exact_point_idw"
        assert result["forecasts"][0]["predicted_value"] == 41.0
        assert client.get("/api/v1/spatial/manifest").status_code == 200
        assert client.get("/api/v1/spatial/surface?parameter=PM10&horizon_hours=24").status_code == 200
        print({"health":"ok","query_value":result["forecasts"][0]["predicted_value"]})
'''
        return self.run([sys.executable, "-c", program])

    def verify_wheel(self) -> str:
        with tempfile.TemporaryDirectory(prefix="smog-ai-wheel-") as directory:
            wheel_dir = Path(directory) / "wheel"
            target_dir = Path(directory) / "installed"
            wheel_dir.mkdir()
            target_dir.mkdir()
            output = self.run([
                sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                "--wheel-dir", str(wheel_dir), ".",
            ])
            wheels = list(wheel_dir.glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError(f"expected one wheel, found {len(wheels)}\n{output}")
            self.run([
                sys.executable, "-m", "pip", "install", "--no-deps", "--target",
                str(target_dir), str(wheels[0]),
            ])
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join([str(target_dir), environment.get("PYTHONPATH", "")])
            imported = self.run([
                sys.executable,
                "-c",
                "from importlib.resources import files; "
                "from smog_ai import __version__; "
                "from smog_ai.spatial.interpolation import IDWInterpolator; "
                "from server.api.settings import ServerSettings; "
                "assert __version__ == '1.7.0'; "
                "assert files('smog_ai').joinpath('resources/poland_boundary.geojson').is_file(); "
                "assert files('smog_ai').joinpath('resources/polish_places.csv').is_file(); assert files('smog_ai').joinpath('resources/docs/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex').is_file(); assert files('smog_ai').joinpath('resources/docs/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex').is_file(); "
                "print(__version__, IDWInterpolator.__name__, ServerSettings.__name__)",
            ], env=environment)
            digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
            return f"{wheels[0].name}, sha256={digest}, import={imported}"

    def verify_test_database_isolation(self) -> str:
        with tempfile.TemporaryDirectory(prefix="smog-ai-test-db-guard-") as directory:
            guard_root = Path(directory)
            hostile_url = "sqlite:///C:/ProgramData/SmogAI/data/smog.db"
            program = r'''
from pathlib import Path
import os
from smog_ai.config import AppConfig, PathsConfig
root = Path(os.environ["SMOG_AI_GUARD_ROOT"])
paths = PathsConfig(
    data_dir=root / "data", database_path=root / "data" / "isolated.db",
    models_dir=root / "models", snapshots_dir=root / "snapshots",
    logs_dir=root / "logs", backups_dir=root / "backups", temp_dir=root / "tmp",
    imgw_metadata_csv=root / "imgw.csv",
)
config = AppConfig(environment="test", paths=paths)
assert "ProgramData/SmogAI" not in config.database_url.replace("\\", "/"), config.database_url
assert config.database_url.endswith("/isolated.db"), config.database_url
print(config.database_url)
'''
            environment = os.environ.copy()
            environment["SMOG_AI_DATABASE_URL"] = hostile_url
            environment["SMOG_AI_GUARD_ROOT"] = str(guard_root)
            # Deliberately bypass run() sanitisation for this one guard: the
            # hostile production URL must reach AppConfig(environment='test').
            process = subprocess.run(
                [sys.executable, "-c", program],
                cwd=self.root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if process.returncode != 0:
                raise RuntimeError(process.stdout.strip())
            return process.stdout.strip()

    def execute(self) -> dict[str, object]:
        self.check("metadata and required files", self.verify_metadata_and_files)
        self.check("configuration parsing", self.parse_configuration_files)
        self.check("portable project root", self.verify_portability)
        self.check("automatic Python 3.12/3.13 bootstrap", self.verify_python_bootstrap_contract)
        self.check("PowerShell UTF-8 BOM and CRLF", self.verify_powershell_encoding)
        self.check("Windows runtime regression guards", self.verify_windows_runtime_regressions)
        self.check("production database isolation for tests", self.verify_test_database_isolation)
        self.check("documentation contract", self.verify_document_contract)
        self.check("local-compute architecture invariant", self.verify_architecture_invariant)
        self.check("hourly multi-target model platform", self.verify_hourly_model_platform)
        self.check("spatial interpolation smoke", self.verify_spatial_smoke)
        self.check("DigitalOcean production app spec", lambda: self.run([sys.executable, "scripts/validate_digitalocean_spec.py", ".do/app.yaml"]))
        self.check("DigitalOcean development app spec", lambda: self.run([sys.executable, "scripts/validate_digitalocean_spec.py", ".do/app.dev.yaml", "--allow-development"]))
        self.check("Python compilation", self.verify_compile)
        self.check("CLI help", lambda: self.run([sys.executable, "-m", "smog_ai", "--help"]))
        self.check("FastAPI seeded spatial smoke", self.verify_fastapi_smoke)
        if self.skip_tests:
            self.results.append(CheckResult("pytest", "skipped", 0.0, "--skip-tests"))
        else:
            def run_pytest() -> str:
                with tempfile.TemporaryDirectory(prefix="smog-ai-pytest-") as directory:
                    return self.run([
                        sys.executable, "-m", "pytest", "-q", "--basetemp", directory
                    ])
            self.check("pytest", run_pytest)
        if self.skip_wheel:
            self.results.append(CheckResult("wheel build/import", "skipped", 0.0, "--skip-wheel"))
        else:
            self.check("wheel build/import", self.verify_wheel)

        failed = [result for result in self.results if result.status == "failed"]
        return {
            "status": "failed" if failed else "passed",
            "version": EXPECTED_VERSION,
            "python": sys.version.split()[0],
            "project_root": str(self.root),
            "checks_total": len(self.results),
            "checks_passed": sum(result.status == "passed" for result in self.results),
            "checks_skipped": sum(result.status == "skipped" for result in self.results),
            "checks_failed": len(failed),
            "results": [asdict(result) for result in self.results],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-wheel", action="store_true")
    args = parser.parse_args(argv)
    root = (args.project_root or Path(__file__).resolve().parents[1]).resolve()
    report = ReleaseGate(root, skip_tests=args.skip_tests, skip_wheel=args.skip_wheel).execute()
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
