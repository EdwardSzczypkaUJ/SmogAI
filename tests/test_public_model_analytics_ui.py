from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_degrades_cleanly_without_optional_remote_services() -> None:
    source = (ROOT / "server" / "dashboard" / "app.py").read_text(encoding="utf-8")

    assert "Langfuse jest wyłączony w oszczędnym profilu testowym" in source
    assert "OpenAI jest wyłączone w oszczędnym profilu testowym" in source
    assert 'projection_basis = "OpenAI wyłączone; parser regułowy"' in source
    assert "Ranking kandydatów pojawi się po publikacji oczyszczonego" in source
    assert "comparison_error" in source
    assert "Kompromis MAE–RMSE" in source
    assert "Wielowymiarowy profil kandydatów" in source
    assert "go.Heatmap" in source
    assert "go.Pie" in source
    assert "go.Scatterpolar" in source


def test_serving_publication_includes_only_sanitised_model_statistics() -> None:
    source = (
        ROOT / "scripts" / "Publish-SmogAI-ServingToDigitalOcean.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "export-model-comparison --publish" in source
    assert "Sanitised model-quality statistics" in source
