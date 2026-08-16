from __future__ import annotations

import json
from pathlib import Path

from server.application.model_source import ObjectStoreModelSource
from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.storage.local import LocalObjectStore


def test_active_models_fall_back_to_safe_serving_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = ArtifactRepository(LocalObjectStore(tmp_path))
    manifest_key = "serving/releases/release=test/manifest.json"
    repository.put_json(
        manifest_key,
        {
            "operations": {
                "models": [
                    {
                        "parameter": "PM10",
                        "algorithm": "hist_gradient_boosting",
                        "version": "safe-version",
                        "activated_at": "2026-08-16T12:00:00Z",
                        "quality_status": "accepted",
                        "artifact_path": r"C:\private\model.joblib",
                    }
                ]
            }
        },
    )
    repository.put_json(
        repository.layout.latest_spatial_pointer,
        {"manifest_key": manifest_key},
    )

    models = ObjectStoreModelSource(repository).active_models()

    assert models[0]["target"] == "PM10"
    assert models[0]["model_version"] == "safe-version"
    assert models[0]["source"] == "serving_v2_manifest"
    encoded = json.dumps(models)
    assert "artifact_path" not in encoded
    assert "private" not in encoded


def test_dashboard_clears_previous_place_before_new_preview() -> None:
    source = Path("server/dashboard/app.py").read_text(encoding="utf-8")

    clear_position = source.index('st.session_state.pop(state_key, None)')
    preview_position = source.index('"query/preview"', clear_position)
    assert clear_position < preview_position
    assert 'st.session_state["hf21_reset_exact_point_mode"] = True' in source
