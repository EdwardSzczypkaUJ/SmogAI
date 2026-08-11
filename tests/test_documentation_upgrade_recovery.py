from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.documentation import load_documentation_bundle
from smog_ai.hourly.recovery import recover_hourly_models_from_object_store


def test_documentation_falls_back_to_packaged_resources(app_config, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    stale = tmp_path / "GIOS_IMGW_Forecast_Suite_1.5.2_Poland_Spatial_Map_AutoPython"
    app_config.documentation.processing_markdown = stale / "docs/platform/TECHNICAL_PROCESSING_PL.md"
    app_config.documentation.processing_latex = stale / "docs/latex/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex"
    app_config.documentation.mathematics_markdown = stale / "docs/platform/MATHEMATICAL_MODEL_PL.md"
    app_config.documentation.model_plugin_markdown = stale / "docs/platform/MODEL_PLUGIN_GUIDE_PL.md"
    app_config.documentation.mathematics_latex = stale / "docs/latex/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex"

    bundle = load_documentation_bundle(app_config)

    assert bundle.processing_markdown
    assert bundle.processing_latex
    assert bundle.mathematics_markdown
    assert bundle.model_plugin_markdown
    assert bundle.mathematics_latex
    assert bundle.metadata["fallback_used"] is True
    assert all(
        item["packaged_fallback_used"] is True
        for item in bundle.metadata["documents"].values()
    )
    assert all(
        "smog_ai" in item["resolved_path"] and "resources" in item["resolved_path"]
        for item in bundle.metadata["documents"].values()
    )


def test_recover_active_hourly_models_from_local_object_store(
    engine, app_config, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "object-store"
    app_config.object_storage.prefix = "recovery-test"
    app_config.hourly_forecasting.enabled = True
    app_config.hourly_forecasting.targets = ["PM10", "temperature_c"]

    repository = create_artifact_repository(app_config)
    repository.store.ensure_container(create_if_missing=True)

    for target in app_config.hourly_forecasting.targets:
        version = f"2026.08.04.010000-test-{target.replace('.', '_')}"
        provider = "persistence"
        artifact = {
            "schema_version": "2.0",
            "forecast_mode": "horizon-conditioned-hourly",
            "target": target,
            "provider": provider,
            "feature_columns": ["horizon_hours", "current_value"],
            "provider_artifact": {"provider": provider, "target": target},
        }
        stored = repository.put_joblib(
            repository.layout.hourly_model_binary(target, version),
            artifact,
            immutable=True,
        )
        card_key = repository.layout.hourly_model_card(target, version)
        repository.put_json(
            card_key,
            {
                "schema_version": "2.0",
                "model_version": version,
                "target": target,
                "provider": provider,
                "feature_columns": artifact["feature_columns"],
                "metrics": {"mae": 1.0},
                "training_data_start": "2026-01-01T00:00:00Z",
                "training_data_end": "2026-06-30T23:00:00Z",
                "artifact": {
                    "object_key": stored.key,
                    "checksum": stored.checksum,
                },
            },
            immutable=True,
        )
        repository.put_json(
            repository.layout.active_hourly_model_pointer(target),
            {
                "schema_version": "2.0",
                "forecast_mode": "horizon-conditioned-hourly",
                "target": target,
                "model_version": version,
                "provider": provider,
                "artifact_object_key": stored.key,
                "artifact_checksum": stored.checksum,
                "model_card_object_key": card_key,
                "activated_at": datetime.now(UTC).isoformat(),
                "source_host_id": "pytest-host",
            },
        )

    with session_scope(engine) as session:
        result = recover_hourly_models_from_object_store(session, app_config)

    assert result.errors == 0
    assert result.inserted == 2

    with session_scope(engine) as session:
        rows = session.scalars(
            select(ModelVersion).where(
                ModelVersion.forecast_horizon == 0,
                ModelVersion.active.is_(True),
            )
        ).all()

    assert {row.parameter for row in rows} == {"PM10", "temperature_c"}
    assert all(Path(row.artifact_path or "").exists() for row in rows)
    assert all((row.metrics_json or {}).get("recovered_from_object_storage") is True for row in rows)
