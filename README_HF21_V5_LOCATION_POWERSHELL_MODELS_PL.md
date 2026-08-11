# HF21 v5 — lokalizacja kontekstowa, PowerShell i wykresy modeli

Hotfix nie zmienia bazy pomiarów, modeli ani opublikowanych prognoz.

## Naprawione problemy

- fraza `lotnisko Witków koło Mieroszowa` wykorzystuje Mieroszów jako punkt
  kontekstu i szuka POI w jego otoczeniu;
- ranking Nominatim uwzględnia zgodność nazwy, typ `aerodrome` i odległość;
- POI zachowuje nazwę `Lądowisko Witków`, zamiast nazwy pobliskiej wsi;
- dodano zweryfikowany fallback `Lotnisko Witków EPDS`:
  `50.79686, 16.11448`;
- odpowiedzi `/query` i `/timeline` nie zawierają kolizji kluczy różniących się
  tylko wielkością liter, których nie obsługuje Windows PowerShell 5.1;
- dokładny punkt jest widoczny również wtedy, gdy dla wybranego parametru brakuje
  prognozy;
- dashboard rysuje MAE/RMSE aktywnych modeli bez uzależniania wykresu od MLflow.

## Instalacja

Rozpakuj paczkę bezpośrednio do katalogu projektu z nadpisaniem plików. Następnie:

```powershell
$ProjectRoot = 'C:\..Work\..GotoIT\Works\..Projects\Weather\..Work\GIOS_IMGW_Forecast_Suite_1.7.0_HF21_ExactPoint_StorageBridge_PCHIP'
Set-Location -LiteralPath $ProjectRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $PythonExe .\scripts\apply_hf21_ui_integrity_hotfix.py
& $PythonExe -m py_compile .\server\api\main.py .\server\dashboard\app.py .\smog_ai\places\http_geocoder.py
```

Usuń stary cache geokodera albo zmień jego nazwę, ponieważ może zawierać wcześniej
wybrany błędny Witków:

```powershell
$env:SMOG_AI_GEOCODER_CACHE_PATH = 'C:\ProgramData\SmogAI\cache\geocoder-cache-v2.json'
```

Najlepiej wpisać tę samą wartość na stałe do `.env`.

## Publikacja lokalnego porównania modeli

Przy skonfigurowanym backendzie `local` poniższe polecenie zapisuje artefakt w
lokalnym ObjectStore, a nie w DigitalOcean Spaces:

```powershell
& $PythonExe -m smog_ai export-model-comparison --publish --env-file .\.env
```

Po operacji uruchom ponownie API i dashboard. Endpoint
`/api/v1/models/compare` powinien zwrócić `models` oraz — jeżeli MLflow ma
zarejestrowane próby — `candidate_runs`.

## Test zapytania

```text
Jaka będzie pogoda jutro na lotnisku Witków koło Mieroszowa około godziny 11:27?
```

Oczekiwany punkt referencyjny:

```text
Lotnisko Witków EPDS
50.79686, 16.11448
```
