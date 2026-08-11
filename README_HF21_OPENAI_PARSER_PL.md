# HF21 — OpenAI Structured Outputs, Langfuse i bezpieczny geocoder

Ta paczka naprawia rozpoznawanie lokalizacji i czasu bez zmieniania pipeline'u
modeli, Storage Bridge, IDW ani PCHIP.

## Co zostało zmienione

- OpenAI używa `json_schema` z `strict: true`, generowanego z modeli Pydantic.
- Nie ma ponowienia zapytania bez schematu. Błąd lub odmowa modelu są jawne.
- Model zwraca osobno `raw_text`, `primary_name` i `context_name`.
- Model zwraca współrzędne jako kandydata, nigdy jako automatycznie zaufany punkt.
- Backend porównuje kandydata z niezależnym resolverem, granicami Polski i progiem 3 km.
- Data i czas są porównywane z parserem deterministycznym z tolerancją 1 minuty
  oraz z zakresem opublikowanych prognoz.
- Niepewna lokalizacja albo czas wymagają zatwierdzenia przed pokazaniem wyniku.
- Fraza `około godziny 15:17` działa również w parserze awaryjnym.
- Resolver offline odrzuca słabe i niejednoznaczne dopasowania zamiast zgadywać.
- Mieroszów jest dostępny w lokalnym gazetteerze.
- Opcjonalny geocoder HTTP obsługuje miejsca spoza gazetteera, ma lokalny cache,
  identyfikujący User-Agent i globalne ograniczenie częstotliwości.
- Langfuse dostaje informację o wersji schematu, użyciu Structured Outputs,
  nazwie głównej, kontekście, tokenach i przyczynie zakończenia.

## Podmiana plików

Rozpakuj ZIP bezpośrednio do katalogu projektu i zezwól na zastąpienie plików.
Paczka zachowuje ścieżki `server/...`, `smog_ai/...` i `tests/...`.

Następnie bezpiecznie uzupełnij aktualny dashboard. Instalator nie podmienia
całego `app.py`: sprawdza dwa stabilne znaczniki, tworzy kopię `.bak`, wprowadza
formularz potwierdzenia i kompiluje wynik:

```powershell
& $Python .\scripts\apply_hf21_confirmation_patch.py
```

Jeżeli znaczniki w Twoim aktualnym dashboardzie są inne, instalator przerwie
pracę bez zmiany pliku.

## Minimalna konfiguracja lokalna `.env`

```dotenv
SMOG_AI_LLM_PROVIDER=openai
SMOG_AI_LLM_MODEL=gpt-4.1-mini
SMOG_AI_LLM_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=TU_WSTAW_KLUCZ

# Podczas testu błąd OpenAI ma być widoczny, a nie ukryty przez fallback.
SMOG_AI_LLM_ALLOW_RULE_FALLBACK=false

# Cały Storage Bridge nadal lokalnie — brak nowych kosztów Spaces.
SMOG_AI_OBJECT_STORE_BACKEND=local
SMOG_AI_SERVER_STORAGE_BACKEND=object_store
```

Nie zapisuj prawdziwych kluczy w Git ani w ZIP-ie.

## Langfuse — opcjonalnie

```dotenv
SMOG_AI_OBSERVABILITY_BACKEND=langfuse
SMOG_AI_OBSERVABILITY_ENVIRONMENT=local-test
SMOG_AI_OBSERVABILITY_STRICT=true
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
```

Jeśli zależność nie była instalowana:

```powershell
& $Python -m pip install -e ".[observability]"
```

## Więcej miejsc niż w gazetteerze offline — opcjonalnie

Domyślnie działa tylko resolver offline. Aby świadomie włączyć
Nominatim-kompatybilny serwer HTTP:

```dotenv
SMOG_AI_GEOCODER_PROVIDER=http
SMOG_AI_GEOCODER_ENDPOINT=https://TWOJ-GEOCODER.example
SMOG_AI_GEOCODER_USER_AGENT=SmogAI/1.7.0 (kontakt: TWOJ_EMAIL_LUB_URL)
SMOG_AI_GEOCODER_CACHE_PATH=C:\ProgramData\SmogAI\cache\geocoder-cache.json
SMOG_AI_GEOCODER_MINIMUM_INTERVAL_SECONDS=1
```

Adres endpointu nie jest wpisany na sztywno. Można użyć własnej instancji albo
dostawcy zgodnego z API Nominatim. Jeżeli wybierzesz publiczny serwer OSMF,
musisz przestrzegać jego aktualnej polityki: maksymalnie 1 żądanie/s, własny
User-Agent, lokalny cache, brak autocomplete/bulk geocoding oraz widoczna
atrybucja OpenStreetMap. Kod realizuje limit, identyfikację i cache; interfejs
powinien również wyświetlić źródło zwracane jako `OpenStreetMap/Nominatim (ODbL)`.

## Testy

Po podmianie plików:

```powershell
& $Python -m pytest tests\test_openai_structured_intent.py -q
& $Python -m smog_ai verify-delivery
```

Uruchom ponownie API i dashboard, aby stary proces nie trzymał poprzedniego kodu:

```powershell
.\scripts\Start-LocalApi.ps1 -EnvFile .\.env -Port 8000
.\scripts\Start-LocalDashboard.ps1 -EnvFile .\.env -Port 8503
```

Test API bez dokładnych współrzędnych:

```powershell
$Body = @{
  text = 'Jutro w Mieroszowie koło Wałbrzycha około godziny 15:17. Podaj PM10, PM2.5, temperaturę i opad.'
} | ConvertTo-Json

$Result = Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/api/v1/query' `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($Body))

$Result.intent | ConvertTo-Json -Depth 10
$Result.place | ConvertTo-Json -Depth 10
$Result.time_selection | ConvertTo-Json -Depth 10
```

Oczekiwane minimum:

```text
intent.location          = Mieroszów
intent.location_raw      = Mieroszów koło Wałbrzycha
intent.location_context  = Wałbrzych
intent.target_time       = ...T15:17:00+02:00
intent.time_precision    = exact_minute
place.name               = Mieroszów
place.latitude           = 50.66694
place.longitude          = 16.18972
location_validation.status = accepted albo confirmation_required
time_validation.status     = accepted albo confirmation_required
```

W przypadku `confirmation_required` dashboard nie pokazuje jeszcze prognozy.
Wyświetla proponowane współrzędne i termin, wynik niezależnych kontroli oraz
formularz pozwalający zatwierdzić albo poprawić oba elementy. Zatwierdzony drugi
request przesyła jawne `latitude`, `longitude` i `target_time`, więc nie są one
ponownie traktowane jako niepotwierdzona propozycja modelu.

Jeżeli testujesz samo OpenAI, checkbox dokładnych współrzędnych ma pozostać
wyłączony. Po pomyślnym teście można ustawić
`SMOG_AI_LLM_ALLOW_RULE_FALLBACK=true`, aby chwilowa awaria API nie blokowała
całej aplikacji.
