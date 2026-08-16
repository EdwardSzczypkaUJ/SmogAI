# SmogAI HF21 — bezpieczne wejście na DigitalOcean

## Architektura docelowa

Komputer Windows pozostaje warstwą danych i MLOps. Pobiera GIOŚ/IMGW, waliduje,
buduje cechy, trenuje modele, generuje prognozy oraz powierzchnie. DigitalOcean
App Platform nie trenuje i nie przechowuje SQLite. Odczytuje wyłącznie
zweryfikowane artefakty Serving v2 z prywatnego Space.

Do Space trafiają:

- `serving/latest.json` — atomowy wskaźnik aktywnego wydania;
- manifest wydania;
- skompresowane powierzchnie `*.json.gz`;
- skompresowana granica Polski i katalog miejscowości;
- małe metadane potrzebne API.

Nie trafiają: SQLite, surowa historia pomiarów, snapshoty treningowe, pliki
`.env`, modele robocze ani logi lokalnego automatu.

## E0 — pieczęć wersji

Uruchom w głównym katalogu bieżącego repozytorium:

```powershell
$ProjectRoot = (Get-Location).Path
.\scripts\Protect-SmogAI-Before-DigitalOcean.ps1 `
  -ProjectRoot $ProjectRoot `
  -OutputRoot 'C:\Users\edzio\Downloads\SmogAI-Seals' `
  -Label 'before-digitalocean'
```

Powstaną: ZIP dokładnego working tree, `git bundle`, manifest i plik SHA-256.
ZIP obejmuje pliki śledzone oraz bezpieczne pliki nieśledzone. Operacja zatrzyma
się przy wykryciu wartości wyglądającej jak klucz.

## E1 — raport aktualności danych

```powershell
$Python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $Python -m smog_ai data-freshness-report `
  --config 'C:\ProgramData\SmogAI\config.yaml' `
  --env-file 'C:\ProgramData\SmogAI\smog-ai.env'
```

Raport JSON i HTML powstaje w
`C:\ProgramData\SmogAI\reports\freshness`. Statusy:

- `fresh` — wiek nie przekracza progu z `quality.stale_*_hours`;
- `warning` — od jednego do dwóch progów;
- `stale` — więcej niż dwa progi;
- `missing` — brak pomiarów parametru.

Opcja `--fail-on-stale` zwraca kod częściowy 4 dla `stale/missing`.

## E2 — preflight bez publikacji

Pierwsze uruchomienie nie może zawierać frazy zgody:

```powershell
.\scripts\Publish-SmogAI-ServingToDigitalOcean.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot 'C:\ProgramData\SmogAI' `
  -SkipSeal
```

Preflight sprawdza połączenie ze Space, bucket, endpoint, prefix, manifest,
sumy SHA-256, listę obiektów i przewidywany transfer. Nie wykonuje zapisu.
Komenda celowo korzysta z wartości `SPACES_*`; lokalne ustawienie
`SMOG_AI_OBJECT_STORE_BACKEND=local` nie może przełączyć jej z powrotem na dysk.

## E3 — jawna publikacja

Po przeczytaniu raportu:

```powershell
.\scripts\Publish-SmogAI-ServingToDigitalOcean.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot 'C:\ProgramData\SmogAI' `
  -SkipSeal `
  -Approval 'PUBLISH VERIFIED SERVING V2'
```

Obiekty niezmienne są przesyłane i odczytywane ponownie w celu sprawdzenia
SHA-256. `serving/latest.json` jest aktualizowany dopiero na końcu. Powtórzenie
jest idempotentne: identyczne obiekty nie są przesyłane ponownie.

Raport przebiegu znajduje się w
`C:\ProgramData\SmogAI\reports\digitalocean\<czas>`.

## E4 — App Platform

Najpierw wdrażamy `.do/app.dev.yaml` z osobnym prefixem staging. Dopiero po
testach używamy `.do/app.yaml`. API otrzymuje klucz Spaces tylko do odczytu.
Dashboard nie otrzymuje żadnych kluczy storage ani LLM i komunikuje się z API
przez `${api.PRIVATE_URL}`.

Sekrety GitHub/App Platform:

- `DIGITALOCEAN_ACCESS_TOKEN`;
- `SPACES_ACCESS_KEY_ID` i `SPACES_SECRET_ACCESS_KEY` — osobny klucz read-only
  dla aplikacji;
- `LLM_API_KEY`;
- opcjonalnie klucze Langfuse.

Lokalny automat publikujący używa osobnego klucza Spaces z prawem zapisu.

## E5 — przyszły harmonogram

Harmonogram zostanie dołączony dopiero po pomyślnym stagingu. Kolejność zadania:

1. pobranie bieżących pomiarów;
2. raport świeżości;
3. budowa/weryfikacja prognoz;
4. budowa/walidacja Serving v2;
5. preflight Spaces;
6. publikacja;
7. zdalny healthcheck;
8. raport końcowy i retencja.

Każdy krok będzie miał blokadę pojedynczego procesu, checkpoint, retry i
monitoring CPU/RAM/dysku/sieci. Brak świeżych danych nie może podmienić ostatniego
poprawnego wydania.
# DigitalOcean — publikacja Serving v2

Preflight i publikacja używają docelowych wartości `SPACES_BUCKET`,
`SPACES_REGION`, `SPACES_ENDPOINT_URL` i `SPACES_PREFIX`. Lokalna wartość
`SMOG_AI_OBJECT_STORE_BACKEND=local` nie przełącza tych poleceń z powrotem na
lokalny magazyn.

Właściwa publikacja jest blokowana, gdy raport aktualności danych nie ma statusu
`fresh`. Po świadomej akceptacji wyjątkowo można użyć `-AllowStaleData`, ale nie
jest to zalecane dla przebiegu produkcyjnego.

Skrypt publikacyjny przyjmuje domyślny próg świeżości 8 godzin. Można go jawnie
zmienić parametrem `-FreshnessThresholdHours`, bez modyfikowania konfiguracji
treningu ani lokalnego pipeline'u.
