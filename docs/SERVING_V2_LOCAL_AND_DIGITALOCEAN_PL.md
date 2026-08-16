# Serving v2 — test lokalny i przygotowanie DigitalOcean

## Co jest publikowane

Do Object Store/Spaces trafiają wyłącznie:

- `serving/latest.json` — mały atomowy wskaźnik;
- `serving/releases/release=<ID>/manifest.json` — manifest wydania;
- `serving/releases/release=<ID>/surfaces/<PARAMETR>/hNNN.json.gz` — gotowe powierzchnie;
- małe, skompresowane zasoby granicy i miejscowości w `serving/static/`;
- opcjonalna dokumentacja i raport porównania modeli.

Nie są potrzebne: baza SQLite, historia pomiarów, snapshot treningowy ani ciężki
`dashboard_snapshot_*.json.gz`. App Platform nie trenuje i nie interpoluje całej
Polski. Rozpakowuje tylko powierzchnię potrzebną do konkretnego zapytania.

## 1. Jednorazowa budowa Serving v2 lokalnie

Uruchom w katalogu aktualnej gałęzi projektu:

```powershell
$ProjectRoot = (Get-Location).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RuntimeRoot = 'C:\ProgramData\SmogAI'
$RuntimeConfig = Join-Path $RuntimeRoot 'config.yaml'
$RuntimeEnv = Join-Path $RuntimeRoot 'smog-ai.env'

$env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
$env:SMOG_AI_OBJECT_STORE_BACKEND = 'local'
$env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT = Join-Path $RuntimeRoot 'object-store'
$env:SMOG_AI_OBJECT_STORE_PREFIX = ''

& $Python -m smog_ai build-spatial-surfaces --config $RuntimeConfig --env-file $RuntimeEnv
if ($LASTEXITCODE -ne 0) { throw "build-spatial-surfaces: $LASTEXITCODE" }

& $Python -m smog_ai validate-spatial-surfaces --config $RuntimeConfig --env-file $RuntimeEnv
if ($LASTEXITCODE -ne 0) { throw "validate-spatial-surfaces: $LASTEXITCODE" }

.\scripts\Test-ServingV2-Local.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -SkipApi
```

## 2. API — pierwsze okno PowerShell

```powershell
$ProjectRoot = (Get-Location).Path
.\scripts\Start-LocalApi.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot 'C:\ProgramData\SmogAI' `
  -EnvFile 'C:\ProgramData\SmogAI\server-local.env' `
  -ListenAddress 127.0.0.1 `
  -Port 8000 `
  -UseLocalServingStore
```

Przełącznik `-UseLocalServingStore` jest celowy: po wczytaniu starego pliku
`.env` wymusza właściwy katalog i pusty prefix, więc API nie przełączy się na
inną gałąź ani dawny prefix Spaces.

## 3. Dashboard — drugie okno PowerShell

```powershell
$ProjectRoot = (Get-Location).Path
$env:SMOG_AI_DASHBOARD_API_URL = 'http://127.0.0.1:8000/api/v1'
.\scripts\Start-LocalDashboard.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot 'C:\ProgramData\SmogAI' `
  -EnvFile 'C:\ProgramData\SmogAI\server-local.env' `
  -ListenAddress 127.0.0.1 `
  -Port 8503
```

Otwórz `http://127.0.0.1:8503`.

## 4. Kontrola — trzecie okno PowerShell

```powershell
$ProjectRoot = (Get-Location).Path
.\scripts\Test-ServingV2-Local.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot 'C:\ProgramData\SmogAI'
```

Oczekiwane: `status=ok`, `spatial_ready=true`, ten sam `release_id` w pliku i
w API oraz `publication_count >= 1`.

## 5. Dopiero po teście lokalnym

Następny etap to skonfigurowanie prywatnego DigitalOcean Space i dwóch usług
App Platform (`api`, `dashboard`). Lokalny pipeline dostanie klucz zapisu,
natomiast API w chmurze wyłącznie klucz odczytu. Przed pierwszym uploadem należy
zmierzyć sumę `compressed_mb` z testu i ustawić retencję trzech wydań.

## 6. Promocja sprawdzonego wydania do Spaces

Nie uruchamiaj ponownie interpolacji i nie używaj do tego celu starego
`upload-operational-data`. Po ustawieniu konfiguracji Spaces opublikuj dokładnie
to wydanie, które przeszło test lokalny:

```powershell
$ProjectRoot = (Get-Location).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RuntimeRoot = 'C:\ProgramData\SmogAI'

Remove-Item Env:SMOG_AI_OBJECT_STORE_BACKEND -ErrorAction SilentlyContinue
Remove-Item Env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:SMOG_AI_OBJECT_STORE_PREFIX -ErrorAction SilentlyContinue

& $Python -m smog_ai publish-serving-release `
  --source-root (Join-Path $RuntimeRoot 'object-store') `
  --config (Join-Path $RuntimeRoot 'config.yaml') `
  --env-file (Join-Path $RuntimeRoot 'smog-ai.env')
if ($LASTEXITCODE -ne 0) { throw "publish-serving-release: $LASTEXITCODE" }
```

Polecenie wysyła tylko Serving v2, weryfikuje sumy SHA-256 i publikuje
`serving/latest.json` na samym końcu. Ponowne wykonanie jest idempotentne.
