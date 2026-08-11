# GIOŚ/IMGW Forecast Suite 1.7.0 — Hourly Multi-Target & Pluggable Models

**Autor:** Edward Szczypka · `edward@szczypka.guru`  
**Komponent lokalny:** Windows 10/11, Python 3.12 lub 3.13 x64, PowerShell 7 / Windows PowerShell 5.1  
**Chmura:** prywatny DigitalOcean Spaces oraz publiczne FastAPI + Streamlit na DigitalOcean App Platform

## Najważniejsza zmiana w 1.7.0

Wersja 1.7.0 nie wybiera już po cichu najbliższej prognozy 6/12/24 h. Lokalny
pipeline trenuje modele warunkowane dokładnym horyzontem i przygotowuje wyniki
co godzinę:

```text
h=1, h=2, ..., h=48 godzin
```

Cele modelu:

```text
PM10
PM2.5
temperature_c
precipitation_mm
```

Opad jest modelowany dwuetapowo: prawdopodobieństwo wystąpienia oraz warunkowa
wielkość opadu. Dla PM10 i PM2.5 prognozowana pogoda dla dokładnego czasu
docelowego jest używana jako cecha. Dla minut pomiędzy pełnymi godzinami
możliwa jest lokalna interpolacja liniowa/PCHIP; ekstrapolacja jest domyślnie
zabroniona.

## Otwarta platforma modeli

Pipeline nie zależy od konkretnej biblioteki ML. Neutralny interfejs
`ModelProvider` pozwala podłączyć inną metodę bez zmiany SQLite, Spaces,
FastAPI lub Streamlit. Provider może zostać dodany przez:

- moduł z `register_models(registry)`;
- entry point `smog_ai.model_providers`;
- import string `module:object` w konfiguracji.

Wbudowane metody obejmują persistence, średnią historyczną, ridge, ograniczoną
regresję wielomianową horyzontu, HistGradientBoosting, regresję kwantylową,
MLP i hurdle model opadu. Szczegóły: `docs/platform/MODEL_PLUGIN_GUIDE_PL.md`.

## Przepływ danych wymagany w zadaniu

```text
GIOŚ JSON-LD + IMGW bieżące + oficjalne archiwa IMGW
        ↓
lokalna SQLite, UTC, WAL, idempotencja, Pandera
        ↓
kompletny pakiet danych → Bridge → lokalny katalog lub DigitalOcean Spaces
        ↓
ponowny odczyt danych przez ten sam Bridge
        ↓
ramki godzinowe i lokalny trening modeli
        ↓
lokalne prognozy PM10, PM2.5, temperatury i opadu h1–h48
        ↓
lokalna publikacja prognoz stacyjnych i powierzchni dla każdej godziny
        ↓
modele, metryki, prognozy, mapy i dokumentacja → Spaces
        ↓
FastAPI + Streamlit odczytują artefakty przez Bridge i liczą dokładny punkt
```

`ObjectStore` jest portem odczytu i zapisu. Ta sama logika działa z backendem
`local`, `memory`, `s3` lub `spaces`; kod pipeline'u, API i dashboardu nie zna
wybranego medium. App Platform nie uruchamia `model.predict`. Może natomiast
wykonać lekką, deterministyczną interpolację dokładnego punktu z opublikowanych
prognoz stacyjnych: quality-weighted IDW w EPSG:2180, a dla minut PCHIP po
wcześniejszej interpolacji przestrzennej. Dzięki temu aplikacja publiczna działa
także wtedy, gdy komputer treningowy jest wyłączony.

## Źródła danych

Aktualne endpointy i formaty są opisane w `docs/DATA_SOURCES.md`:

- GIOŚ v1 JSON-LD: `https://api.gios.gov.pl/pjp-api/v1/rest`;
- IMGW SYNOP: `https://danepubliczne.imgw.pl/api/data/synop`;
- oficjalne miesięczne archiwa terminowe/SYNOP IMGW.

## Dokumentacja dostępna również w aplikacji

Dashboard zawiera zakładkę **Jak to działa** z dokumentacją techniczną,
matematyczną i przewodnikiem pluginów. FastAPI udostępnia:

```text
GET /api/v1/docs/manifest
GET /api/v1/docs/processing
GET /api/v1/docs/processing/source
GET /api/v1/docs/mathematics
GET /api/v1/docs/mathematics/source
GET /api/v1/docs/model-plugins
GET /api/v1/models
```

Źródła LaTeX:

```text
docs/latex/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex
docs/latex/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex
```

## Instalacja lokalna — wariant kontrolowany

Projekt może znajdować się w dowolnym katalogu. Dane i sekrety trafiają
poza repozytorium, domyślnie do `%ProgramData%\\SmogAI`.

```powershell
Set-Location -LiteralPath "D:\\Dowolny katalog\\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\scripts\Setup-All.ps1 `
    -SpaceName "NAZWA-SPACE" `
    -SpacesRegion "fra1" `
    -SpacesPrefix "smog-ai/production" `
    -LlmProvider "rule_based" `
    -SkipLangfuse `
    -InstallDevelopmentDependencies `
    -SkipFirstRun
```

Instalator wykrywa Python 3.12/3.13, tworzy własne `.venv`, generuje
konfigurację, wykonuje migracje i sprawdza Spaces.
Obsługiwane są zarówno Python 3.12, jak i Python 3.13. Jeżeli nie ma zgodnego interpretera, instalator może opcjonalnie użyć `winget`; można to wyłączyć parametrem `-NoAutomaticPythonInstall`.

Po kontroli storage:

```powershell
$ProjectRoot = (Resolve-Path '.').Path
$RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
& $Python -m smog_ai first-run --config $Config --env-file $EnvFile
```

Pierwszy przebieg może pobrać wiele miesięcznych archiwów IMGW. Zakres jest
konfigurowalny przez sekcję `imgw_archive`.

## Najważniejsze polecenia CLI

```text
python -m smog_ai collect-gios
python -m smog_ai collect-imgw
python -m smog_ai backfill-imgw-archive
python -m smog_ai collect-all
python -m smog_ai validate
python -m smog_ai match-stations
python -m smog_ai upload-operational-data
python -m smog_ai build-hourly-features
python -m smog_ai list-model-methods
python -m smog_ai train-hourly
python -m smog_ai predict-hourly
python -m smog_ai hourly-readiness
python -m smog_ai build-spatial-surfaces
python -m smog_ai validate-spatial-surfaces
python -m smog_ai publish-documentation
python -m smog_ai build-snapshot
python -m smog_ai storage-health
python -m smog_ai report
python -m smog_ai healthcheck
python -m smog_ai first-run
```

## Lokalny FastAPI i dashboard

W dwóch oddzielnych terminalach:

```powershell
.\scripts\Start-LocalApi.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

```powershell
.\scripts\Start-LocalDashboard.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

Adresy:

```text
FastAPI:   http://127.0.0.1:8000
Swagger:   http://127.0.0.1:8000/docs
Dashboard: http://127.0.0.1:8501
```

Dashboard pokazuje czytelną mapę 2D, opcjonalny tryb 3D z wysokością zależną
od wartości, nazwy miast, stacje, dokładny punkt zapytania, środek komórki
interpolacji, temperaturę, prawdopodobieństwo opadu i oczekiwaną sumę opadu.

## DigitalOcean App Platform i automatyczny deploy

Repozytorium zawiera:

```text
.do/app.yaml
.do/app.dev.yaml
.github/workflows/ci-deploy-digitalocean.yml
```

Workflow testuje projekt, waliduje App Spec i po pushu/merge do `main`
wdraża FastAPI oraz Streamlit przez `digitalocean/app_action/deploy@v2`.
Dokładna instrukcja: `docs/STEP_BY_STEP_LOCAL_WINDOWS_AND_DIGITALOCEAN_PL.md`.

## Harmonogram Windows

Po poprawnym pilocie lokalnym i zdalnym:

```powershell
.\scripts\Install-ScheduledTasks.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -WakeComputer `
    -RunSmokeTest
```

Powstają cztery zadania w folderze `\\SmogAI\\`: godzinowy pipeline,
codzienna konserwacja, tygodniowy trening i miesięczny backup.

## Kontrola wydania

```powershell
.\scripts\Test-Release.ps1
```

albo:

```powershell
.\.venv\Scripts\python.exe scripts\verify_release.py
```

Bramka działa offline i nie używa sekretów klienta. Rzeczywiste połączenia z
GIOŚ, IMGW, Spaces i App Platform są osobnym testem integracyjnym.

## Ważne ograniczenia naukowe

Techniczny sukces pipeline’u nie oznacza automatycznie wysokiej jakości
prognoz. Model powinien zostać oceniony na chronologicznym backteście,
osobno dla każdego celu, horyzontu, stacji i sezonu. Nowa wersja modelu jest
aktywowana dopiero po spełnieniu kryteriów jakości względem modeli bazowych.
Semantyka opadu jest jawna: domyślnie `precipitation_mm` oznacza akumulację
w okresie 6 h kończącym się w `target_time`, a nie sztucznie wyliczone `mm/h`.

## Historyczne PM10 i PM2.5 z GIOŚ

Pełny importer danych archiwalnych działa niezależnie od krótkiego polecenia
`backfill` (maksymalnie 31 dni). Najszybszy import wieloletni całej Polski:

```powershell
.\scripts\Run-GiosHistoricalBackfill.ps1 `
    -StartYear 2022 `
    -EndYear 2024 `
    -Source prepared `
    -Voivodeships ALL `
    -Pollutants "PM10,PM2.5"
```

Dla nowszych lat dostępny jest wznawialny, ograniczony zgodnie z limitem GIOŚ
import przez API rok/województwo/zanieczyszczenie. Szczegóły i procedura
ponownego treningu: `docs/GIOS_HISTORICAL_BACKFILL_PL.md`.


## HF19: równoległy import, reprodukowalny trening i serving

Do treningu quick/full zalecane jest obecnie polecenie
`snapshot-train-hourly`. Tworzy ono spójną kopię żywej SQLite, nadaje jej
`dataset_id` i SHA-256, a następnie dopasowuje model bez blokowania długim
odczytem bazy ingestu. Szczegóły:

- `docs/platform/TRAINING_SNAPSHOT_BRIDGE_PL.md`;
- `docs/platform/STAGE_2_3_IMPLEMENTATION_PL.md`.
