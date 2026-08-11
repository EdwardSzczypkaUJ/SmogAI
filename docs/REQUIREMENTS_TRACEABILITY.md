# Mapowanie wymagań na implementację — 1.7.0

| Wymaganie | Implementacja |
|---|---|
| Aktualne GIOŚ JSON-LD | `smog_ai/collectors/gios.py`, `http_client.py`, `docs/DATA_SOURCES.md` |
| IMGW bieżące i archiwa | `collectors/imgw.py`, `collectors/imgw_archive.py` |
| Lokalna historia SQLite | `smog_ai/database`, migracje `0001`, `0002` |
| Idempotencja | constraints DB, repozytoria, immutable IDs i wskaźniki `latest.json` |
| Pandera | `smog_ai/data_validation/contracts.py` |
| Dopasowanie Haversine | `smog_ai/processing/matching.py` |
| Wymagany round trip przez Spaces | `artifacts/datasets.py`, `first-run`, `weekly-maintenance` |
| Dokładne horyzonty h1–h48 | `smog_ai/hourly/features.py`, `trainer.py`, `predictor.py` |
| PM10/PM2.5/temperatura/opad | `hourly/*`, tabela `forecasts`, konfiguracja `hourly_forecasting` |
| Hurdle model opadu | `modeling/providers.py`, `hourly/trainer.py` |
| Brak wycieku pogody | chronologiczny cross-fitting w `hourly/trainer.py` |
| Otwarta platforma modeli | `modeling/contracts.py`, `registry.py`, `providers.py` |
| Provider zewnętrzny | moduły `register_models`, entry points, `external_factories` |
| Prognoza przed wynikiem | tabela `forecasts`, `hourly/predictor.py` |
| Weryfikacja prognoz | `prediction/verifier.py` |
| Mapy 5 parametrów | `smog_ai/spatial/service.py`, `colors.py` |
| IDW/RBF Bridge | `spatial/contracts.py`, `interpolation.py`, `factory.py` |
| EPSG:2180 i maska Polski | `spatial/grid.py`, `poland_boundary.geojson` |
| Dokładny punkt interpolacji | `server/application/query.py`, dashboard |
| Textbox i czas docelowy | `smog_ai/nlp`, `server/application/query.py` |
| Brak najbliższego horyzontu | `exact_target_time_available`, testy query/spatial |
| Brak ML w App Platform | `.do/app*.yaml`, release gate, test deploymentu |
| Dokumentacja na platformie | `documentation/service.py`, endpointy `/api/v1/docs/*` |
| LaTeX techniczny i matematyczny | `docs/latex/*.tex` |
| FastAPI | `server/api/main.py` |
| Streamlit/PyDeck/Plotly | `server/dashboard/app.py` |
| Langfuse | `smog_ai/observability` |
| Portable Windows | `scripts/SmogAi.Common.ps1` i brak stałego checkout path |
| Task Scheduler | `Install-ScheduledTasks.ps1`, cztery XML-e |
| Backup SQLite | `monitoring/backup.py`, `Backup-SmogAi.ps1` |
| Izolacja testów | `tests/conftest.py`, `Test-PytestIsolated.ps1`, release gate |
| CI/CD | `.github/workflows/ci-deploy-digitalocean.yml` |
| DigitalOcean App Platform | `.do/app.yaml`, `.do/app.dev.yaml` |
