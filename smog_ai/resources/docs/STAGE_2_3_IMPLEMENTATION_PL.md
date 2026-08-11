# Etapy 2 i 3 — model lokalny, aplikacja lokalna i DigitalOcean App Platform

## Zakres

Etap 2 obejmuje przygotowanie niezmiennego datasetu, ograniczony trening,
monitoring i bramę jakości. Etap 3 obejmuje lokalne FastAPI/Streamlit oraz
wdrożenie bez zapisu na DigitalOcean App Platform.

Import historyczny nie jest warunkiem rozpoczęcia implementacji. Można go
zatrzymać, a później wznowić. Po wdrożeniu `TrainingSnapshotBridge` może również
działać równolegle z treningiem.

## Etap 2 — pilot PM2.5

Terminal 1:

```powershell
$ProjectRoot = "C:\...\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable"
$RuntimeRoot = Join-Path $env:ProgramData "SmogAI"

Set-Location -LiteralPath $ProjectRoot

.\scripts\Run-Stage2-PM25-Pilot.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -Parameters "PM2.5" `
  -Snapshot auto
```

Terminal 2:

```powershell
.\scripts\Watch-TrainingProgress.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -Mode quick `
  -RefreshSeconds 5
```

Po treningu:

```powershell
.\scripts\Show-TrainingSnapshots.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -Profile quick `
  -VerifyChecksum
```

## Etap 3A — lokalna aplikacja

Najpierw należy posiadać gotowe, opublikowane prognozy i mapy. Następnie:

Terminal API:

```powershell
.\scripts\Start-LocalApi.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot
```

Terminal dashboardu:

```powershell
.\scripts\Start-LocalDashboard.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot
```

Terminal testowy:

```powershell
.\scripts\Test-LocalServer.ps1 -AsJson
```

## Etap 3B — preflight DigitalOcean

```powershell
.\scripts\Test-Stage2Stage3Readiness.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -VerifySnapshotChecksum
```

Po opublikowaniu wszystkich artefaktów można użyć ostrzejszego wariantu:

```powershell
.\scripts\Test-Stage2Stage3Readiness.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -VerifySnapshotChecksum `
  -StrictArtifacts
```

## Architektura DigitalOcean

```text
Windows/local:
  collect -> validate -> snapshot -> train -> predict -> ObjectStore Bridge

DigitalOcean App Platform:
  FastAPI     -> Bridge read -> dokładny punkt IDW -> PCHIP
  Streamlit   -> private FastAPI URL
  brak SQLite
  brak treningu
  brak migracji
  brak uploadu danych przez HTTP
```

Bridge jest dwukierunkowy: lokalny pipeline tym samym interfejsem zapisuje i
odczytuje dane z katalogu lokalnego albo DigitalOcean Spaces. Zmiana medium to
zmiana `object_storage.backend`, a nie zmiana kodu pipeline'u lub serwera.

Repozytorium zawiera:

```text
.do/app.yaml
.do/app.dev.yaml
.github/workflows/ci-deploy-digitalocean.yml
```

Deployment po `push` do `main` jest wykonywany dopiero po testach i lokalnej
weryfikacji kontraktu App Platform.

## Rollback

- poprzedni model pozostaje aktywny, dopóki kandydat nie przejdzie walidacji;
- każdy snapshot ma `dataset_id` i SHA-256;
- stare artefakty w Spaces są wersjonowane;
- App Platform jest stateless i można ponownie wdrożyć wcześniejszy commit.
