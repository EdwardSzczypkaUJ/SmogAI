# Wdrożenie warstwy serwerowej 1.7.0

Docelową platformą publiczną jest **DigitalOcean App Platform**. Warstwa
serwerowa składa się z dwóch usług:

```text
api        FastAPI, publiczne `/api/*`, odczyt gotowych artefaktów ze Spaces
dashboard  Streamlit, publiczne `/`, prywatna komunikacja z `api`
```

## Twardy niezmiennik

App Platform nie pobiera GIOŚ/IMGW, nie przygotowuje cech, nie trenuje,
nie wykonuje `model.predict()` i nie interpoluje przestrzennie Polski. Wszystkie
prognozy godzinowe `h=1..48` oraz powierzchnie PM10, PM2.5, temperatury i opadu
powstają wcześniej na komputerze lokalnym i są publikowane do prywatnego
DigitalOcean Spaces.

## Pliki wdrożeniowe

- `.do/app.yaml` — produkcja;
- `.do/app.dev.yaml` — środowisko demonstracyjne;
- `.github/workflows/ci-deploy-digitalocean.yml` — testy i automatyczny deploy;
- `docs/DIGITALOCEAN_APP_PLATFORM.md` — konfiguracja usług i CI/CD;
- `docs/STEP_BY_STEP_LOCAL_WINDOWS_AND_DIGITALOCEAN_PL.md` — pełna procedura.

## Warunek uruchomienia

Przed deploymentem w Spaces muszą istnieć niepuste wskaźniki:

```text
datasets/bronze/latest.json
forecasts/latest.json
maps/latest.json
documentation/latest.json
```

`maps/latest.json` powinien wskazywać manifest zawierający dokładne powierzchnie
godzinowe, `exact_target_time_available=true` oraz cele:

```text
PM10, PM2.5, temperature_c, precipitation_probability, precipitation_mm
```

## Lokalny start identycznego kodu

```powershell
.\scripts\Start-LocalApi.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
.\scripts\Start-LocalDashboard.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

Adresy lokalne:

```text
FastAPI:   http://127.0.0.1:8000
Swagger:   http://127.0.0.1:8000/docs
Streamlit: http://127.0.0.1:8501
```
