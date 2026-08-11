# Operations runbook 1.7.0

## 1. Zmienne pomocnicze

```powershell
$ProjectRoot = (Resolve-Path '.').Path
$RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
```

## 2. Szybka diagnostyka

```powershell
& $Python -m smog_ai healthcheck --json --config $Config --env-file $EnvFile
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
& $Python -m smog_ai hourly-readiness --config $Config --env-file $EnvFile
.\scripts\Test-SmogAiHealth.ps1 -AsJson
```

Prawidłowy storage powinien zwracać niepuste `latest_raw`, `latest_forecast`,
`latest_spatial` i `documentation`.

## 3. Dashboard zgłasza odmowę połączenia

`WinError 10061` oznacza, że Streamlit działa, ale FastAPI nie nasłuchuje na
porcie 8000. Uruchom API w osobnym terminalu:

```powershell
.\scripts\Start-LocalApi.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
Test-NetConnection 127.0.0.1 -Port 8000
```

Dopiero potem uruchom/odśwież dashboard.

## 4. Brak dokładnej godziny

Sprawdź manifest:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/spatial/manifest |
  ConvertTo-Json -Depth 20
```

W trybie 1.7.0 powinno być:

```text
forecast_mode = horizon-conditioned-hourly
exact_target_time_available = true
horizons_hours = 1..48
```

Brak pakietu dla dokładnego `target_time` nie może powodować cichego wyboru
najbliższego horyzontu. Należy ponownie wykonać lokalne `predict-hourly` i
`build-spatial-surfaces`.

## 5. Brak aktywnego modelu godzinowego

```powershell
& $Python -m smog_ai list-model-methods --config $Config --env-file $EnvFile
& $Python -m smog_ai hourly-readiness --config $Config --env-file $EnvFile
& $Python -m smog_ai train-hourly --config $Config --env-file $EnvFile
```

Najczęstsze przyczyny:

- za mało historii godzinowej;
- brak archiwum IMGW;
- błąd kontraktu Pandera;
- niekompletny pakiet Bronze w Spaces;
- zewnętrzny provider modelu nie został załadowany.

## 6. Ponowne zbudowanie wyników

```powershell
& $Python -m smog_ai predict-hourly --config $Config --env-file $EnvFile
& $Python -m smog_ai build-spatial-surfaces --config $Config --env-file $EnvFile
& $Python -m smog_ai validate-spatial-surfaces --config $Config --env-file $EnvFile
& $Python -m smog_ai publish-documentation --config $Config --env-file $EnvFile
& $Python -m smog_ai build-snapshot --config $Config --env-file $EnvFile
```

## 7. Niedostępność Internetu/Spaces

Dane pozostają w SQLite. Po powrocie łączności:

```powershell
& $Python -m smog_ai upload-operational-data --config $Config --env-file $EnvFile
& $Python -m smog_ai retry-publications --config $Config --env-file $EnvFile
```

## 8. Backup

```powershell
.\scripts\Backup-SmogAi.ps1 -Tier daily
```

Backup korzysta z SQLite Online Backup API, kompresji i SHA-256. Nie kopiuj
aktywnego `smog.db` ręcznie przy włączonym WAL.

## 9. Harmonogram zadań

```powershell
Get-ScheduledTask -TaskPath '\SmogAI\'
Get-ScheduledTaskInfo -TaskPath '\SmogAI\' -TaskName 'Hourly Pipeline'
.\scripts\Repair-ScheduledTasks.ps1 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
```

Tygodniowe zadanie wykonuje round trip `SQLite → Spaces → lokalny trening ze
Spaces`, aktywuje model tylko po spełnieniu kryteriów i publikuje nowe
powierzchnie.
