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
    assert "Front Pareto: MAE i RMSE" in source
    assert "Lewy dolny narożnik jest najlepszy" in source
    assert "Wielowymiarowy profil kandydatów" in source
    assert "go.Heatmap" in source
    assert "go.Pie" in source
    assert "go.Scatterpolar" in source
    assert "Interpretacja zapytania: parser regułowy" in source
    assert "OpenAI nie zostało" in source
    assert "podsumowanie treningu" in source
    assert "Poprawa vs persistence" in source
    assert "dane uczące i plik modelu pozostają lokalne" in source
    assert "Kiedy nowy model był lepszy — a kiedy trening nic nie zmienił" in source
    assert "Zmiana względem poprzedniego aktywnego modelu" in source
    assert "Historia pobrań i świeżości GIOŚ / IMGW" in source
    assert "Wiek najnowszych danych w kolejnych aktualizacjach" in source
    assert "Świeżość modelu" in source
    assert "🗺️ Parametr × horyzont" in source
    assert "jakość modeli w horyzoncie 1–48 h" in source
    assert "Udział zwycięstw w poszczególnych horyzontach" in source
    assert "Radar jakości modeli" in source
    assert '"openai", "openai_compatible"' in source
    assert 'SMOG_AI_OPENAI_PRICED_MODEL", "gpt-5.4-mini"' in source
    assert 'SMOG_AI_OPENAI_INPUT_USD_PER_1M", "0.75"' in source
    assert 'SMOG_AI_OPENAI_OUTPUT_USD_PER_1M", "4.50"' in source
    assert "Dzienny transfer publikacji do DigitalOcean Spaces" in source
    assert "Sumy miesięczne transferu Spaces" in source
    assert "Modele w zapisanej historii" in source
    assert "Faktyczni providerzy interpretacji" in source
    assert "Struktura prognozowanych kosztów miesięcznych" in source
    assert "Scenariusze kosztu względem liczby zapytań" in source
    assert "brak cennika" in source
    assert "Wiek pomiaru i ostatniego pobrania GIOŚ / IMGW" in source
    assert 'annotation_text=f"koniec fresh ({fresh_threshold:g} h)"' in source
    assert 'annotation_text=f"stale / blokada ({stale_threshold:g} h)"' in source
    assert "Aktualna świeżość — TERAZ" in source
    assert "Wartości są przeliczane przy każdym odświeżeniu strony" in source
    assert '"point_kind": "TERAZ"' in source
    assert "Zwycięzcy parametrów — kilka metryk obok siebie" in source
    assert 'barmode="group"' in source
    assert ".head(10)" in source
    settings_source = (ROOT / "server" / "api" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert '"SMOG_AI_LLM_ALLOW_RULE_FALLBACK", False' in settings_source


def test_serving_publication_includes_only_sanitised_model_statistics() -> None:
    source = (
        ROOT / "scripts" / "Publish-SmogAI-ServingToDigitalOcean.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "export-model-comparison --publish" in source
    assert "Sanitised model-quality statistics" in source
