# GIOŚ/IMGW Forecast Suite 1.7.0 — instrukcja od Windows do App Platform

**Autor:** Edward Szczypka, `edward@szczypka.guru`.

## A. Przygotowanie

Potrzebujesz:

- Windows 10/11 x64;
- Python 3.12 albo 3.13 x64;
- PowerShell 7 lub Windows PowerShell 5.1;
- prywatny DigitalOcean Space;
- Spaces Access Key ID + Secret z `Read/Write/Delete`;
- opcjonalnie klucz LLM i Langfuse.

Projekt może leżeć w dowolnym katalogu. Nie ustawiaj z góry żadnego stałego katalogu projektu.

## B. DigitalOcean Spaces

Utwórz Standard Storage w regionie `fra1`, z `Restricted` listingiem, bez CDN.
Przykład:

```text
bucket: smog-ai-krakow-prod-12345
prefix: smog-ai/krakow/production
endpoint: https://fra1.digitaloceanspaces.com
```

Szczegóły: `docs/DIGITALOCEAN_SPACES_KRAKOW_STEP_BY_STEP.md`.

## C. Instalacja kontrolowana

```powershell
Set-Location -LiteralPath 'D:\Dowolny katalog\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable'
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\scripts\Setup-All.ps1 `
  -SpaceName 'smog-ai-krakow-prod-12345' `
  -SpacesRegion 'fra1' `
  -SpacesPrefix 'smog-ai/krakow/production' `
  -LlmProvider 'rule_based' `
  -SkipLangfuse `
  -InstallDevelopmentDependencies `
  -SkipFirstRun
```

Instalator tworzy `.venv`, `%ProgramData%\SmogAI`, konfigurację, migracje i
sprawdza Space. Nie aktywuj ręcznie `.venv`.

## D. Kontrola

```powershell
$ProjectRoot = (Resolve-Path '.').Path
$RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

& $Python --version
& $Python -m pip check
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
```

## E. Pierwszy przepływ

```powershell
& $Python -m smog_ai first-run --config $Config --env-file $EnvFile
```

Ręczny wariant pozwalający prześledzić wymagany round trip:

```powershell
& $Python -m smog_ai collect-all --config $Config --env-file $EnvFile
& $Python -m smog_ai upload-operational-data --config $Config --env-file $EnvFile
& $Python -m smog_ai prepare-training-data --config $Config --env-file $EnvFile
& $Python -m smog_ai build-hourly-features --source object_store --config $Config --env-file $EnvFile
& $Python -m smog_ai train-hourly --config $Config --env-file $EnvFile
& $Python -m smog_ai predict-hourly --config $Config --env-file $EnvFile
& $Python -m smog_ai build-spatial-surfaces --config $Config --env-file $EnvFile
& $Python -m smog_ai publish-documentation --config $Config --env-file $EnvFile
```

Przepływ:

```text
GIOŚ + IMGW bieżące + archiwum IMGW
→ SQLite/Pandera
→ Bronze do Spaces
→ ponowny odczyt ze Spaces
→ ramki h1..h48
→ lokalny trening temperatury i opadu
→ cross-fitting pogody
→ lokalny trening PM10/PM2.5
→ lokalne prognozy godzinowe
→ lokalne powierzchnie Polski
→ prognozy, mapy, modele i dokumentacja do Spaces
```

Kontrola:

```powershell
& $Python -m smog_ai hourly-readiness --config $Config --env-file $EnvFile
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
& $Python -m smog_ai validate-spatial-surfaces --config $Config --env-file $EnvFile
```

## F. Lokalna aplikacja

Terminal 1:

```powershell
.\scripts\Start-LocalApi.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

Terminal 2:

```powershell
.\scripts\Start-LocalDashboard.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

Terminal 3:

```powershell
.\scripts\Test-LocalServer.ps1 -AsJson
```

Sprawdź pytanie z dokładną godziną oraz zakładki `Model i jakość` i `Jak to
działa`.

## G. Bramka wydania

```powershell
.\scripts\Test-Release.ps1
```

## H. GitHub

```powershell
git init
git branch -M main
git add .
git commit -m 'Initial release 1.7.0'
gh repo create TWOJ_LOGIN/gios-imgw-forecast --private --source . --remote origin --push
```

## I. DigitalOcean App Platform

Nadaj App Platform dostęp do repozytorium i utwórz token deploymentu.
Następnie:

```powershell
$Token = Read-Host 'DigitalOcean Personal Access Token' -AsSecureString

.\scripts\Configure-GitHubDeploy.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -AppName 'smog-ai-krakow-prod' `
  -CustomerName 'Smog AI Kraków' `
  -DigitalOceanAccessToken $Token
```

Dla App Platform ustaw osobny Spaces key tylko `Read`.

Uruchom:

```powershell
gh workflow run 'CI and deploy to DigitalOcean App Platform' --ref main
gh run watch --exit-status
```

## J. Test zdalny

```powershell
$AppUrl = 'https://TWOJA-APLIKACJA.ondigitalocean.app'
Invoke-RestMethod "$AppUrl/api/v1/health"
Invoke-RestMethod "$AppUrl/api/v1/ready"
Invoke-RestMethod "$AppUrl/api/v1/models"
Start-Process $AppUrl
```

## K. Harmonogram Windows

Dopiero po pilocie, jako administrator:

```powershell
.\scripts\Install-ScheduledTasks.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -WakeComputer `
  -RunSmokeTest
```

Powstają: Hourly Pipeline, Daily Maintenance, Weekly Training i Monthly Backup.

## L. Zmiany kodu

```powershell
git switch -c feature/zmiana
.\scripts\Test-Release.ps1
git add .
git commit -m 'Opis zmiany'
git push -u origin feature/zmiana
```

Merge do `main` uruchamia automatyczny deployment. Nowe dane i modele pojawiają
się przez Spaces bez redeployu.
