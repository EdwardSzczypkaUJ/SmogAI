# Architektura 1.7.0 — Hourly Multi-Target & Pluggable Models

## Niezmiennik wdrożeniowy

Wszystkie ciężkie obliczenia odbywają się lokalnie: pobieranie, walidacja,
trening i prognozowanie. DigitalOcean App Platform odczytuje gotowe artefakty
przez ten sam Bridge co instalacja lokalna. Nie ładuje modelu ML, ale może
wykonać deterministyczne IDW w dokładnym punkcie i PCHIP dla wskazanej minuty.

## Warstwy

```text
collectors → database → Pandera → artifacts/Spaces → hourly features
→ ModelProvider registry → models → hourly forecasts → spatial surfaces
→ documentation → FastAPI read adapters → Streamlit
```

## Porty i adaptery

- `ObjectStore`: dwukierunkowy Bridge (`put/get/head/list/delete`) dla local,
  memory, S3/Spaces/MinIO;
- `ModelProvider`: wbudowany lub plugin;
- `SpatialInterpolator`: IDW/RBF i kolejne implementacje;
- `IntentInterpreter`: regułowy lub OpenAI-compatible;
- `Observability`: no-op lub Langfuse;
- źródła serwera: snapshot, spatial, model cards i dokumentacja.

Backend storage wybiera wyłącznie konfiguracja. Repozytoria artefaktów są
stroną abstrakcji Bridge i nie zawierają warunków zależnych od DigitalOcean.
Zapis surowych danych, datasetów, modeli i prognoz oraz ich późniejszy odczyt
przechodzą przez ten sam kontrakt.

## Dokładny punkt

API rozwiązuje zapytanie do WGS84 `latitude/longitude`, odczytuje opublikowane
prognozy stacyjne przez Bridge i liczy quality-weighted IDW (`p=2`) w EPSG:2180.
Dla czasu pomiędzy godzinami najpierw liczy ten sam punkt dla godzin źródłowych,
a dopiero potem PCHIP. Odpowiedź zawiera współrzędne, precyzję lokalizacji,
metodę, godziny źródłowe i udziały stacji.

## Model godzinowy

Jeden aktywny model na cel jest przechowywany z sentinelowym horyzontem 0.
Horyzont 1–48 jest cechą wejściową. W bazie prognozy mają rzeczywisty
`forecast_horizon` i dokładny `target_time = forecast_origin_time + h`.

## Kolejność treningu

1. temperatura;
2. opad;
3. chronologiczne prognozy out-of-fold pogody;
4. PM10 i PM2.5 z prognozowaną pogodą;
5. kwantyle i metryki per horyzont;
6. porównanie z baseline;
7. atomowa aktywacja oraz upload model card do Spaces.

Pełny opis: `docs/platform/TECHNICAL_PROCESSING_PL.md` oraz źródła LaTeX.
