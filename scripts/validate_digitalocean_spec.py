#!/usr/bin/env python3
"""Deterministic validation of the DigitalOcean App Platform contract.

The validator intentionally performs no network calls. DigitalOcean validates the
platform schema again during ``digitalocean/app_action/deploy@v2``. This local check
protects the project-specific architecture: local ML -> Spaces -> App Platform readers.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
EXPECTED_SERVICES = {"api", "dashboard"}
SPACES_KEYS = {
    "SMOG_AI_OBJECT_STORE_BACKEND",
    "SMOG_AI_OBJECT_STORE_BUCKET",
    "SMOG_AI_OBJECT_STORE_REGION",
    "SMOG_AI_OBJECT_STORE_ENDPOINT",
    "SMOG_AI_OBJECT_STORE_PREFIX",
    "SPACES_ACCESS_KEY_ID",
    "SPACES_SECRET_ACCESS_KEY",
}
ANALYTICS_SPACES_KEYS = {
    "ANALYTICS_SPACES_BUCKET",
    "ANALYTICS_SPACES_REGION",
    "ANALYTICS_SPACES_ENDPOINT_URL",
    "ANALYTICS_SPACES_PREFIX",
    "ANALYTICS_SPACES_ACCESS_KEY_ID",
    "ANALYTICS_SPACES_SECRET_ACCESS_KEY",
}
NLP_KEYS = {
    "SMOG_AI_LLM_PROVIDER",
    "SMOG_AI_LLM_MODEL",
    "SMOG_AI_LLM_BASE_URL",
    "SMOG_AI_LLM_ALLOW_RULE_FALLBACK",
    "LLM_API_KEY",
}
OBSERVABILITY_KEYS = {
    "SMOG_AI_OBSERVABILITY_BACKEND",
    "SMOG_AI_OBSERVABILITY_ENVIRONMENT",
    "SMOG_AI_OBSERVABILITY_RELEASE",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
}
SECRET_KEYS = {
    "SPACES_ACCESS_KEY_ID",
    "SPACES_SECRET_ACCESS_KEY",
    "ANALYTICS_SPACES_ACCESS_KEY_ID",
    "ANALYTICS_SPACES_SECRET_ACCESS_KEY",
    "LLM_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
}


def _mapping_by_name(items: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Every {label} entry must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not APP_NAME.fullmatch(name):
            raise ValueError(f"Invalid {label} name: {name!r}")
        if name in result:
            raise ValueError(f"Duplicate {label} name: {name}")
        result[name] = item
    return result


def _env_map(component: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in component.get("envs", []):
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise ValueError(f"{component.get('name')}: invalid env entry")
        key = str(row["key"])
        if key in result:
            raise ValueError(f"{component.get('name')}: duplicate env key {key}")
        result[key] = row
    return result


def _validate_service_common(name: str, service: dict[str, Any]) -> None:
    github = service.get("github") or {}
    if github.get("repo") != "${REPOSITORY_SLUG}":
        raise ValueError(f"{name}: repository must be injected by GitHub Actions")
    if github.get("branch") != "main":
        raise ValueError(f"{name}: GitHub branch must be main")
    if github.get("deploy_on_push") is not False:
        raise ValueError(
            f"{name}: deploy_on_push must be false; tested GitHub Actions is the sole deploy driver"
        )
    if service.get("http_port") != 8080:
        raise ValueError(f"{name}: http_port must be 8080")
    command = str(service.get("run_command") or "")
    if "0.0.0.0" not in command or "8080" not in command:
        raise ValueError(f"{name}: run command must bind to 0.0.0.0:8080")
    if not (service.get("health_check") or {}).get("http_path"):
        raise ValueError(f"{name}: HTTP health check is required")
    source = service.get("source_dir")
    if source != "/":
        raise ValueError(f"{name}: source_dir must be repository root")


def validate(path: Path, *, allow_development: bool = False) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("App spec must contain a YAML object")
    if document.get("name") != "${DIGITALOCEAN_APP_NAME}":
        raise ValueError("App name must be injected by the deployment workflow")
    if document.get("region") != "fra":
        raise ValueError("App Platform must default to the FRA region")
    if document.get("databases"):
        raise ValueError("App Platform must not define a database; Spaces is canonical")
    if document.get("jobs"):
        raise ValueError("App Platform must not train or migrate data; Windows performs ML")

    services = _mapping_by_name(document.get("services", []), "service")
    if set(services) != EXPECTED_SERVICES:
        raise ValueError("App spec must define exactly api and dashboard services")
    for name, service in services.items():
        _validate_service_common(name, service)

    api_env = _env_map(services["api"])
    required_api = {
        "SMOG_AI_ENV",
        "SMOG_AI_APP_VERSION",
        "SMOG_AI_COMMIT_SHA",
        "SMOG_AI_CUSTOMER_NAME",
        "SMOG_AI_SERVER_STORAGE_BACKEND",
        "SMOG_AI_SERVER_UPLOADS_ENABLED",
        "SMOG_AI_SERVER_DOCS_ENABLED",
        "SMOG_AI_SERVER_RATE_LIMIT_PER_MINUTE",
        "SMOG_AI_SPATIAL_ENABLED",
        "SMOG_AI_SPATIAL_CACHE_TTL_SECONDS",
        "SMOG_AI_SPATIAL_CACHE_MAX_ITEMS",
        *SPACES_KEYS,
        *ANALYTICS_SPACES_KEYS,
        "ANALYTICS_RETENTION_DAYS",
        *NLP_KEYS,
        *OBSERVABILITY_KEYS,
    }
    missing = required_api - api_env.keys()
    if missing:
        raise ValueError(f"api: missing environment variables: {sorted(missing)}")
    if api_env["SMOG_AI_SERVER_STORAGE_BACKEND"].get("value") != "spaces":
        raise ValueError("api must read published artifacts through the Spaces Bridge")
    if api_env["SMOG_AI_OBJECT_STORE_BACKEND"].get("value") != "spaces":
        raise ValueError("api object store implementation must be spaces")
    if str(api_env["SMOG_AI_SERVER_UPLOADS_ENABLED"].get("value")).lower() != "false":
        raise ValueError("HTTP uploads must be disabled; local pipeline publishes directly to Spaces")
    if api_env["SMOG_AI_APP_VERSION"].get("value") != "1.7.0":
        raise ValueError("api: app version must be 1.7.0")
    if str(api_env["SMOG_AI_SPATIAL_ENABLED"].get("value")).lower() != "true":
        raise ValueError("api must expose locally precomputed spatial surfaces")
    if int(api_env["SMOG_AI_SPATIAL_CACHE_MAX_ITEMS"].get("value") or 0) < 1:
        raise ValueError("api spatial cache must be explicitly bounded")
    if api_env["ANALYTICS_SPACES_BUCKET"].get("value") == api_env["SMOG_AI_OBJECT_STORE_BUCKET"].get("value"):
        raise ValueError("analytics and serving must use separate Spaces buckets")
    if api_env["ANALYTICS_RETENTION_DAYS"].get("value") != "${ANALYTICS_RETENTION_DAYS}":
        raise ValueError("analytics retention must be injected as ANALYTICS_RETENTION_DAYS")
    api_command = str(services["api"].get("run_command") or "").lower()
    for forbidden in (" train", " predict", "build-spatial-surfaces", "weekly-maintenance"):
        if forbidden in api_command:
            raise ValueError(f"api run command performs forbidden local computation: {forbidden.strip()}")
    if not allow_development:
        if api_env["SMOG_AI_ENV"].get("value") != "production":
            raise ValueError("production app spec must set SMOG_AI_ENV=production")
        if str(api_env["SMOG_AI_SERVER_DOCS_ENABLED"].get("value")).lower() != "false":
            raise ValueError("production API documentation must be disabled")

    for key in SECRET_KEYS:
        if api_env[key].get("type") != "SECRET":
            raise ValueError(f"api: {key} must be an encrypted SECRET")
        value = str(api_env[key].get("value") or "")
        if value and not (value.startswith("${") and value.endswith("}")):
            raise ValueError(f"api: {key} must not contain a literal credential")

    dashboard_env = _env_map(services["dashboard"])
    required_dashboard = {
        "SMOG_AI_ENV",
        "SMOG_AI_APP_VERSION",
        "SMOG_AI_COMMIT_SHA",
        "SMOG_AI_CUSTOMER_NAME",
        "SMOG_AI_DASHBOARD_TITLE",
        "SMOG_AI_DASHBOARD_API_URL",
    }
    missing_dashboard = required_dashboard - dashboard_env.keys()
    if missing_dashboard:
        raise ValueError(
            f"dashboard: missing environment variables: {sorted(missing_dashboard)}"
        )
    if dashboard_env["SMOG_AI_DASHBOARD_API_URL"].get("value") != "${api.PRIVATE_URL}/api/v1":
        raise ValueError("dashboard must call FastAPI over api.PRIVATE_URL")
    leaked = (SPACES_KEYS | ANALYTICS_SPACES_KEYS | SECRET_KEYS) & dashboard_env.keys()
    if leaked:
        raise ValueError(
            f"dashboard must not receive storage/LLM credentials; FastAPI is the adapter: {sorted(leaked)}"
        )
    if dashboard_env["SMOG_AI_APP_VERSION"].get("value") != "1.7.0":
        raise ValueError("dashboard: app version must be 1.7.0")

    rules = (document.get("ingress") or {}).get("rules") or []
    routes = {
        rule.get("match", {}).get("path", {}).get("prefix"): rule.get("component", {}).get("name")
        for rule in rules
        if isinstance(rule, dict)
    }
    if routes.get("/api") != "api" or routes.get("/") != "dashboard":
        raise ValueError("Ingress must route /api to api and / to dashboard")

    return {
        "status": "ok",
        "path": str(path),
        "services": sorted(services),
        "canonical_storage": "DigitalOcean Spaces",
        "database_components": 0,
        "jobs": 0,
        "http_snapshot_upload": False,
        "dashboard_storage_credentials": False,
        "prediction_mode": "published_station_forecasts",
        "app_platform_computation": "read_and_exact_point_interpolate",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=".do/app.yaml", type=Path)
    parser.add_argument("--allow-development", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate(args.path, allow_development=args.allow_development)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"DigitalOcean app spec validation failed: {exc}", file=sys.stderr)
        return 1
    print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
