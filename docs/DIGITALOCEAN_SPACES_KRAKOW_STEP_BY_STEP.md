# DigitalOcean Spaces — wdrożenie Kraków krok po kroku

DigitalOcean nie posiada regionu „Kraków”. Dla wdrożenia dotyczącego Krakowa używamy najbliższego praktycznego regionu Spaces: **Frankfurt `fra1`**. Nazwa Kraków występuje w nazwie projektu i prefiksie danych.

Docelowe wartości:

```text
projekt:  Smog AI Kraków
Space:    smog-ai-krakow-prod-<unikalny-sufiks>
region:   fra1
endpoint: https://fra1.digitaloceanspaces.com
prefix:   smog-ai/krakow/production
storage:  Standard
listing:  Restricted
CDN:      Disabled
CORS:     brak
```

## 1. Utwórz projekt DigitalOcean

W panelu: `New Project` → nazwa `Smog AI Kraków`. Nie twórz Dropleta, bazy ani klastra.

## 2. Utwórz Space

`Spaces Object Storage` → `Buckets` → `Create Bucket`:

1. region `Frankfurt — FRA1`;
2. `Standard Storage`;
3. CDN wyłączony;
4. nazwa, np. `smog-ai-krakow-prod-48271`;
5. przypisz do projektu `Smog AI Kraków`.

Nazwa musi być unikalna i zgodna z DNS. Po utworzeniu w `Settings` sprawdź:

```text
File Listing: Restricted
CDN: Disabled
CORS: none
```

Space pozostaje prywatny mimo że aplikacja Streamlit jest publiczna.

## 3. Utwórz klucz lokalnego pipeline’u

`Spaces Object Storage` → `Access Keys` → `Create Access Key`:

```text
scope:       Limited access
bucket:      wybrany Space
permission:  Read/Write/Delete
name:        smog-ai-krakow-local
```

Zapisz `Access Key ID` i `Secret Access Key`; sekret jest pokazywany tylko raz.

## 4. Opcjonalnie utwórz osobny klucz App Platform

Najmniejsze uprawnienia:

```text
scope:       Limited access
bucket:      ten sam Space
permission:  Read
name:        smog-ai-krakow-app-read
```

W wariancie demonstracyjnym można użyć jednego klucza, ale produkcyjnie zalecane są oddzielne klucze. Streamlit nie otrzymuje żadnego klucza; tylko FastAPI.

## 5. Przygotuj projekt lokalnie

Rozpakuj projekt w dowolnym katalogu i przejdź do niego:

```powershell
Set-Location -LiteralPath 'D:\Projekty\Smog AI Kraków'
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
Remove-Item Env:SMOG_AI_PROJECT_ROOT -ErrorAction SilentlyContinue
```

Kontrola:

```powershell
Test-Path .\pyproject.toml
Test-Path .\scripts\Setup-All.ps1
```

Oba wyniki muszą być `True`.

## 6. Uruchom automat konfiguracyjny

```powershell
.\scripts\Setup-All.ps1 `
  -ProjectRoot (Get-Location).Path `
  -SpaceName 'smog-ai-krakow-prod-48271' `
  -SpacesRegion 'fra1' `
  -SpacesPrefix 'smog-ai/krakow/production' `
  -LlmProvider 'rule_based' `
  -SkipLangfuse `
  -InstallDevelopmentDependencies `
  -SkipFirstRun
```

Skrypt bezpiecznie zapyta o klucze. Nie wpisuj sekretu bezpośrednio w komendzie.

Runtime domyślnie:

```text
%ProgramData%\SmogAI\config.yaml
%ProgramData%\SmogAI\smog-ai.env
%ProgramData%\SmogAI\server-local.env
```

## 7. Sprawdź połączenie

```powershell
$ProjectRoot = (Resolve-Path '.').Path
$RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

& $Python -m smog_ai storage-init --no-create-if-missing --config $Config --env-file $EnvFile
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
```

Przed pierwszym przebiegiem `latest_raw`, `latest_forecast` i `latest_spatial` mogą być puste. Sam status storage powinien być `ok`.

## 8. Pierwszy pełny obieg

```powershell
& $Python -m smog_ai first-run --config $Config --env-file $EnvFile
```

Kolejność:

```text
GIOŚ/IMGW → SQLite → Spaces → download do treningu → Pandera
→ trening lokalny → prognozy lokalne → mapa Polski → Spaces
```

Po zakończeniu:

```powershell
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
& $Python -m smog_ai healthcheck --json --config $Config --env-file $EnvFile
```

## 9. Sprawdź obiekty w panelu

Pod `smog-ai/krakow/production/` powinny znajdować się:

```text
datasets/bronze/latest.json
forecasts/latest.json
maps/latest.json
maps/static/poland-boundary.geojson
maps/runs/surface-set=.../manifest.json
maps/runs/surface-set=.../parameter=PM10/horizon=24/surface.json.gz
```

Nie twórz tych „katalogów” ręcznie; są prefiksami obiektów.

## 10. Lokalny podgląd

```powershell
.\scripts\Start-LocalApi.ps1
.\scripts\Start-LocalDashboard.ps1
```

Wpisz:

```text
Jutro rano będę w Krakowie. Jakie będą PM10 i PM2.5?
```

Mapa ma zaznaczyć Kraków i pokazać wartość z gotowej powierzchni.

## 11. Przygotowanie App Platform

Dopiero po lokalnym teście przejdź do `docs/DIGITALOCEAN_APP_PLATFORM.md`. GitHub Secrets muszą wskazywać ten sam bucket, region i prefix. Niezgodny prefix jest najczęstszą przyczyną pustego dashboardu.
