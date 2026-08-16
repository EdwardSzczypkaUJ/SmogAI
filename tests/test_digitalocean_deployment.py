from __future__ import annotations

from pathlib import Path

import yaml

from scripts.validate_digitalocean_spec import validate

ROOT = Path(__file__).resolve().parents[1]


def _envs(component: dict) -> dict[str, dict]:
    return {str(item["key"]): item for item in component.get("envs", [])}


def test_production_app_spec_uses_spaces_without_database_or_jobs() -> None:
    path = ROOT / ".do" / "app.yaml"
    validate(path)
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not spec.get("disable_edge_cache")
    assert not spec.get("databases")
    assert not spec.get("jobs")
    services = {service["name"]: service for service in spec["services"]}
    assert set(services) == {"api", "dashboard"}

    api_env = _envs(services["api"])
    assert api_env["SMOG_AI_SERVER_STORAGE_BACKEND"]["value"] == "spaces"
    assert api_env["SMOG_AI_SERVER_UPLOADS_ENABLED"]["value"] == "false"
    assert api_env["SMOG_AI_OBJECT_STORE_BACKEND"]["value"] == "spaces"
    assert api_env["SPACES_ACCESS_KEY_ID"]["type"] == "SECRET"
    assert api_env["SPACES_SECRET_ACCESS_KEY"]["type"] == "SECRET"
    assert api_env["ANALYTICS_SPACES_ACCESS_KEY_ID"]["type"] == "SECRET"
    assert api_env["ANALYTICS_SPACES_SECRET_ACCESS_KEY"]["type"] == "SECRET"
    assert api_env["ANALYTICS_SPACES_BUCKET"]["value"] == "${ANALYTICS_SPACES_BUCKET}"
    assert api_env["SMOG_AI_SPATIAL_CACHE_MAX_ITEMS"]["value"] == "64"
    assert api_env["SMOG_AI_LLM_PROVIDER"]["value"] == "${SMOG_AI_LLM_PROVIDER}"
    assert api_env["SMOG_AI_OBSERVABILITY_BACKEND"]["value"] == "${SMOG_AI_OBSERVABILITY_BACKEND}"

    dashboard_env = _envs(services["dashboard"])
    assert dashboard_env["SMOG_AI_DASHBOARD_API_URL"]["value"] == "${api.PRIVATE_URL}/api/v1"
    assert "SPACES_ACCESS_KEY_ID" not in dashboard_env
    assert "SPACES_SECRET_ACCESS_KEY" not in dashboard_env
    assert "LLM_API_KEY" not in dashboard_env
    assert "ANALYTICS_SPACES_ACCESS_KEY_ID" not in dashboard_env
    assert "ANALYTICS_SPACES_SECRET_ACCESS_KEY" not in dashboard_env


def test_development_spec_uses_separate_spaces_prefix_and_no_database() -> None:
    path = ROOT / ".do" / "app.dev.yaml"
    validate(path, allow_development=True)
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not spec.get("databases")
    assert not spec.get("jobs")
    services = {service["name"]: service for service in spec["services"]}
    api_env = _envs(services["api"])
    assert api_env["SMOG_AI_SERVER_STORAGE_BACKEND"]["value"] == "spaces"
    assert api_env["SMOG_AI_SERVER_DOCS_ENABLED"]["value"] == "true"
    assert api_env["SMOG_AI_OBJECT_STORE_PREFIX"]["value"] == "${SPACES_PREFIX}"


def test_github_workflow_runs_tests_before_spaces_deploy() -> None:
    text = (ROOT / ".github" / "workflows" / "ci-deploy-digitalocean.yml").read_text(
        encoding="utf-8"
    )
    assert "needs: test" in text
    assert "digitalocean/app_action/deploy@v2" in text
    assert "app_spec_location: .do/app.yaml" in text
    assert "python -m pytest -q" in text
    assert "DIGITALOCEAN_ACCESS_TOKEN" in text
    assert "SPACES_ACCESS_KEY_ID" in text
    assert "SPACES_SECRET_ACCESS_KEY" in text
    assert "SPACES_BUCKET" in text
    assert "ANALYTICS_SPACES_ACCESS_KEY_ID" in text
    assert "ANALYTICS_SPACES_SECRET_ACCESS_KEY" in text
    assert "ANALYTICS_SPACES_BUCKET" in text
    assert "fromJson(steps.deploy.outputs.app).live_url" in text
    assert "if: steps.deploy.outcome == 'success'" in text
    assert "APP_JSON: ${{ steps.deploy.outputs.app }}" in text
    assert 'echo "DEPLOYED_APP_URL=$APP_URL" >> "$GITHUB_ENV"' in text
    assert "concurrency:" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "inputs.deploy_production == true" in text
    assert "if: github.event_name != 'pull_request'" not in text
    assert "SMOG_AI_OBSERVABILITY_BACKEND || 'langfuse'" in text
    assert "ANALYTICS_RETENTION_DAYS || '90'" in text


def test_local_fastapi_windows_helpers_are_present_and_portable() -> None:
    expected = {
        "Setup-All.ps1",
        "Start-LocalApi.ps1",
        "Start-LocalDashboard.ps1",
        "Test-LocalServer.ps1",
        "Configure-GitHubDeploy.ps1",
    }
    scripts = {path.name for path in (ROOT / "scripts").glob("*.ps1")}
    assert expected <= scripts

    for path in (ROOT / "scripts").glob("*.ps1"):
        text = path.read_text(encoding="utf-8")
        assert r"C:\SmogAI" not in text, path.name
        assert "C:/SmogAI" not in text, path.name

    api_script = (ROOT / "scripts" / "Start-LocalApi.ps1").read_text(encoding="utf-8")
    assert "Resolve-SmogAiProjectRoot" in api_script
    assert "server.api.main:app" in api_script
    dashboard_script = (ROOT / "scripts" / "Start-LocalDashboard.ps1").read_text(
        encoding="utf-8"
    )
    assert "Resolve-SmogAiProjectRoot" in dashboard_script
    assert "server\\dashboard\\app.py" in dashboard_script
    assert "SMOG_AI_DASHBOARD_API_URL" in dashboard_script


def test_step_by_step_guide_covers_automated_spaces_roundtrip() -> None:
    guide = (
        ROOT / "docs" / "STEP_BY_STEP_LOCAL_WINDOWS_AND_DIGITALOCEAN_PL.md"
    ).read_text(encoding="utf-8")
    assert "Setup-All.ps1" in guide
    assert "Configure-GitHubDeploy.ps1" in guide
    assert "DigitalOcean Spaces" in guide
    assert "upload-operational-data" in guide
    assert "prepare-training-data" in guide
    assert "Start-LocalApi.ps1" in guide
    assert "Start-LocalDashboard.ps1" in guide
    assert "Install-ScheduledTasks.ps1" in guide
    assert "C:\\SmogAI" not in guide


def test_app_platform_is_read_only_and_dashboard_uses_precomputed_pydeck_surface() -> None:
    server_tree = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "server").rglob("*.py")
    )
    assert ".predict(" not in server_tree
    dashboard = (ROOT / "server" / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "pydeck" in dashboard
    assert "ColumnLayer" in dashboard
    assert "GeoJsonLayer" in dashboard
    assert "TextLayer" in dashboard
    assert "locally" not in dashboard.lower() or "lokalnie" in dashboard.lower()
    api = (ROOT / "server" / "api" / "main.py").read_text(encoding="utf-8")
    assert "/api/v1/spatial/surface" in api
    assert "^[A-Za-z0-9_.-]{1,64}$" in api
    assert "published_parameters" in api
    assert "/api/v1/spatial/manifest" in api
