# Changelog

## 1.7.0 HF18 — generyczne parametry jakości powietrza

- dodano centralny `AirParameterRegistry` z niezależnymi rolami pobierania, historii, cechy, celu i mapy;
- uogólniono bieżący kolektor i historyczny backfill GIOŚ;
- uogólniono godzinowy trening, predykcję, walidację, snapshot i powierzchnie przestrzenne;
- dodano cechy pomocnicze innych parametrów bez wycieku przyszłości;
- dodano katalog parametrów i konfigurator ról PowerShell;
- dodano publikację aliasów/jednostek do snapshotu i manifestu map, aby textbox obsługiwał parametry konfiguracyjne;
- domyślne role pozostają ograniczone do PM10 i PM2.5.

## 1.7.0 HF12 — GIOŚ Historical PM Backfill

- dodano wznawialny importer oficjalnych godzinowych danych historycznych PM10/PM2.5;
- starsze kompletne lata są pobierane z przygotowanych rocznych ZIP-ów GIOŚ;
- nowsze lata są pobierane przez API rok × województwo × zanieczyszczenie;
- respektowany jest limit 2 żądania/min dla API archiwalnego;
- czasy archiwalne CET są jednoznacznie zamieniane na UTC;
- dodano cache, stan wznowienia, idempotentny zapis SQLite, audyt pokrycia historii i skrypt PowerShell;
- dodano zależność `openpyxl` do odczytu oficjalnych plików XLSX.

## 1.7.0 — Hourly Multi-Target & Pluggable Models

- dokładne prognozy godzinowe `h=1..48`;
- PM10, PM2.5, temperatura i opad jako cele;
- hurdle model opadu i kwantyle;
- cross-fitting prognoz pogody dla modeli PM;
- otwarty rejestr `ModelProvider` i pluginy;
- importer archiwów terminowych/SYNOP IMGW;
- powierzchnie map dla dokładnego `target_time`;
- rozbudowany dashboard, model cards i dokumentacja na platformie;
- dokumentacja matematyczna i techniczna LaTeX.

## 1.5.5 — izolacja pytest i bezpieczne odzyskiwanie SQLite

- testowa konfiguracja bezwzględnie ignoruje `SMOG_AI_DATABASE_URL`;
- autouse fixture usuwa produkcyjne `SMOG_AI_*`, `SPACES_*`, `LANGFUSE_*` i `AWS_*`;
- dodano `Test-PytestIsolated.ps1` z wrogim URL-em strażniczym i osobnym `--basetemp`;
- release gate uruchamia wszystkie podprocesy w oczyszczonym środowisku;
- dodano audyt znaczników danych pytest w bazie produkcyjnej;
- dodano bezpieczną odbudowę: SQLite Online Backup, kwarantanna DB/WAL/SHM, migracje do
  świeżej bazy i raport JSON;
- poprawka hotfixu jest transakcyjna: nieudana kompilacja/testy przywracają pliki;
- usunięto projektowe ostrzeżenie dotyczące niejawnej jednostki `timedelta`;
- dodano testy regresyjne uruchamiane także przy celowo ustawionym produkcyjnym URL-u.

## 1.5.4 — GIOŚ nationwide collection / Pandera-safe datasets

- sensor-specific GIOŚ 400/404 responses are warnings instead of pipeline errors;
- structured HTTP status exceptions for source-aware classification;
- training rows with hourly gaps or invalid required values are removed before Pandera;
- stale/non-future forecasts are not created; legacy invalid forecasts are excluded from snapshots;
- cumulative safe-first-run and complete-Spaces-bundle gating;
- 88 regression tests.

## 1.5.3 — GIOŚ JSON-LD / first-run cascade hotfix

- poprawiono `Accept` i parser aktualnego API GIOŚ v1 JSON-LD;
- dodano stronicowanie bieżących pomiarów i `probe-gios`;
- retry ograniczono do błędów przejściowych;
- niekompletny eksport nie nadpisuje kanonicznego wskaźnika danych;
- first-run nie uruchamia treningu ani nie publikuje pustych snapshotów po awarii kolektora;
- brak curated jest kontrolowanym stanem `skipped`.

## 1.5.3 — 2026-08-02

- naprawiono wykrywanie aktywnego Pythona 3.13 z Anacondy/Condy;
- zastąpiono wielowierszowe `python -c` sondą uruchamianą z tymczasowego pliku `.py`;
- aktywny `$env:CONDA_PREFIX\python.exe` oraz bieżący `python.exe` z `PATH` mają pierwszeństwo;
- dodano skanowanie `py -0p`, `where.exe python`, typowych katalogów oraz rejestru PEP 514;
- dodano `scripts/Diagnose-Python.ps1` z raportem wszystkich kandydatów i powodów odrzucenia;
- kod `winget` `-1978335189` (`0x8A15002B`) nie jest już traktowany jako jednoznaczna awaria instalacji;
- po wyniku winget wskazującym istniejącą instalację detektor ponownie skanuje interpretery;
- dodano dokumentację i test regresyjny dla scenariusza `(base)` + Python 3.13;
- zalecany tryb bez winget: `-PythonExecutable $PythonPath -NoAutomaticPythonInstall`.

## 1.5.1 — 2026-08-02

- instalator akceptuje Python 3.12 i 3.13 x64;
- usunięto twarde sprawdzanie wyłącznie wersji 3.12;
- dodano automatyczne wykrywanie `py.exe`, instalacji CPython, aktywnego Conda i `PATH`;
- przy braku wspieranego interpretera instalator próbuje doinstalować Python 3.12 przez `winget`;
- dodano parametry `-PythonExecutable`, `-PreferredPythonVersion`, `-NoAutomaticPythonInstall` i `-RecreateVenv`;
- istniejące uszkodzone lub niewspierane `.venv` jest przenoszone do kopii, a nie usuwane;
- dodano `scripts/Prepare-Python.ps1`, dokumentację bootstrapu i test regresyjny.

## 1.5.0 — 2026-08-01

- dodano lokalnie generowane powierzchnie PM10/PM2.5 dla całej Polski;
- dodano wymienny Bridge IDW/RBF, EPSG:2180 i maskę granicy Polski;
- dodano confidence, leave-one-station-out MAE/RMSE i Pandera spatial contract;
- dodano wersjonowane `maps/runs/...` i atomowy `maps/latest.json` w Spaces;
- dodano offline gazetteer miejscowości oraz zaznaczanie miasta z textboxa;
- przebudowano FastAPI tak, aby wyłącznie odczytywało lokalnie policzone wyniki;
- dodano endpointy manifestu, powierzchni, granicy i wyszukiwania miejsc;
- przebudowano dashboard na PyDeck z mapą, legendą, stacjami i trybem 3D;
- dodano test zabraniający `model.predict()` i interpolacji w komponencie serwerowym;
- rozszerzono CI/CD, release gate, przykłady i dokumentację DigitalOcean Kraków.

## 1.4.1 — 2026-07-31

- round trip DigitalOcean Spaces przed lokalnym treningiem;
- Pandera, Langfuse, textbox NLP i App Platform;
- portable project-root oraz PowerShell UTF-8 BOM + CRLF.

## 1.0.0

- pierwszy lokalny pipeline GIOŚ/IMGW, SQLite, modele, snapshot i dashboard.

## 1.7.0 HF19 — TrainingSnapshotBridge i etapy 2/3

- dodano niezmienny snapshot SQLite tworzony Online Backup API;
- importer może działać podczas treningu na snapshotcie;
- każdy model zapisuje `dataset_id` i SHA-256;
- dodano CLI i PowerShell dla snapshotów, pilota PM2.5 oraz monitoringu;
- dodano preflight lokalnego FastAPI/Streamlit i DigitalOcean App Platform;
- zachowano App Platform jako warstwę read-only opartą na Spaces.
