from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_finops_uses_a_per_model_price_catalog_and_safe_unknown_zero() -> None:
    source = (ROOT / "server" / "dashboard" / "app.py").read_text(
        encoding="utf-8"
    )

    for model in (
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.2",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4o-mini",
    ):
        assert f'"{model}"' in source

    assert "def _model_price(" in source
    assert "def _model_token_cost(" in source
    assert "def _history_cost_estimate(" in source
    assert '"known": False' in source
    assert '"input": 0.0' in source
    assert '"output": 0.0' in source
    assert "brak stawki — przyjęto 0 USD" in source
    assert "Zero oznacza brak cennika, a nie model darmowy" in source
    assert "Porównanie kosztu modeli dla tego samego użycia" in source
    assert "Model wariantu kosztowego" in source
    assert "Stawki modeli — wejście i wyjście" in source
    assert "Koszt porównawczy dla" in source
    assert 'barmode="group"' in source
