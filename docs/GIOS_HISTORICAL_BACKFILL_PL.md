# Historyczne dane GIOŚ PM10 i PM2.5 — pobieranie, kontrola i trening

## 1. Po co jest ten importer

Bieżący endpoint GIOŚ przechowuje tylko krótki zakres ostatnich danych. Taki
zbiór wystarcza do demonstracji pipeline'u, lecz nie wystarcza do uczciwej
walidacji modeli godzinowych. W takiej sytuacji platforma poprawnie wybiera
model bazowy `persistence`, który zwraca ostatnią wartość dla kolejnych
horyzontów.

Polecenie `backfill-gios-history` importuje oficjalne archiwalne pomiary
automatyczne, jednogodzinne PM10 i PM2.5. Import jest:

- idempotentny;
- wznawialny;
- oparty na lokalnym cache;
- ograniczony zgodnie z limitem GIOŚ 2 żądania/min dla API archiwalnego;
- odporny na przerwanie komputera lub połączenia;
- zapisujący czasy archiwalne jako stały CET (UTC+01:00), a wewnętrznie UTC.

## 2. Dwa źródła oficjalne

### 2.1. Przygotowane roczne ZIP-y — wariant szybki

Dla kompletnych starszych lat platforma pobiera pliki „Wyniki pomiarów z
YYYY roku” z Banku danych pomiarowych GIOŚ. Z archiwum wybierane są wyłącznie:

```text
YYYY_PM10_1g.xlsx
YYYY_PM2.5_1g.xlsx
```

czyli automatyczne wyniki jednogodzinne. Dane manualne 24-godzinne nie są
mieszane z modelem godzinowym.

Aktualna lista przygotowanych plików obejmuje lata do 2024. GIOŚ zaleca ten
wariant dla danych starszych niż dwa lata.

### 2.2. Roczne API według województwa — wariant dla nowszych lat

Dla lat, dla których nie ma jeszcze przygotowanego ZIP-a, używany jest endpoint:

```text
/v1/rest/archivalData/getDataForAllStationsByYearAndVoivodeship
```

Zapytanie jest wykonywane osobno dla:

```text
rok × województwo × PM10/PM2.5
```

API jest stronicowane, maksymalnie 500 rekordów na stronę. Oficjalny limit
wynosi 2 żądania na minutę, dlatego import całej Polski za bieżący rok może
trwać wiele godzin lub kilka dni. Cache i `state.json` pozwalają bezpiecznie
zatrzymać proces i kontynuować później.

## 3. Pierwszy zalecany import

Najpierw pobierz trzy kompletne lata całej Polski z przygotowanych ZIP-ów:

```powershell
.\scripts\Run-GiosHistoricalBackfill.ps1 `
    -StartYear 2022 `
    -EndYear 2024 `
    -Source prepared `
    -Voivodeships ALL `
    -Pollutants "PM10,PM2.5"
```

To jest najszybszy sposób uzyskania wieloletniej historii PM dla wszystkich
stacji.

## 4. Import nowszych danych nakładających się na historię IMGW

Dla pilota Katowice/Kraków pobierz najpierw nowsze lata tylko dla dwóch
województw:

```powershell
.\scripts\Run-GiosHistoricalBackfill.ps1 `
    -StartYear 2025 `
    -EndYear 2026 `
    -Source api `
    -Voivodeships "ŚLĄSKIE,MAŁOPOLSKIE" `
    -Pollutants "PM10,PM2.5" `
    -RequestIntervalSeconds 31
```

Ten zakres lepiej nakłada się czasowo na najnowszą historię pogodową IMGW i
pozwala wcześniej ocenić modele dla Katowic i Krakowa.

Pełna Polska dla 2025–2026:

```powershell
.\scripts\Run-GiosHistoricalBackfill.ps1 `
    -StartYear 2025 `
    -EndYear 2026 `
    -Source api `
    -Voivodeships ALL `
    -Pollutants "PM10,PM2.5" `
    -RequestIntervalSeconds 31
```

To polecenie należy traktować jako długotrwałe zadanie. Nie zmniejszaj odstępu
poniżej 30 sekund.

## 5. Krótki test API przed pełnym uruchomieniem

Można ograniczyć pobieranie do dwóch pierwszych stron każdej kombinacji:

```powershell
.\scripts\Run-GiosHistoricalBackfill.ps1 `
    -StartYear 2026 `
    -EndYear 2026 `
    -Source api `
    -Voivodeships "ŚLĄSKIE" `
    -Pollutants "PM10" `
    -MaxPagesPerCombination 2 `
    -NoResume
```

Po teście uruchom pełny import bez parametru `-MaxPagesPerCombination`.

## 6. Wznawianie po przerwaniu

Domyślnie `-Resume` jest włączone. Ponów dokładnie to samo polecenie. Platforma:

- wykorzysta już pobrane ZIP-y;
- wykorzysta zapisane strony JSON-LD;
- pominie zakończone lata/województwa/zanieczyszczenia;
- nie utworzy duplikatów w SQLite.

Cache znajduje się domyślnie w:

```text
C:\ProgramData\SmogAI\tmp\gios-history-cache
```

Nie usuwaj cache po przerwanym imporcie.

## 7. Kontrola pokrycia historii

```powershell
$ProjectRoot = "C:\...\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable"
$RuntimeRoot = Join-Path $env:ProgramData "SmogAI"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $RuntimeRoot "config.yaml"
$EnvFile = Join-Path $RuntimeRoot "smog-ai.env"

& $Python -m smog_ai gios-history-status `
    --config $Config `
    --env-file $EnvFile
```

Najważniejsze pola:

```text
rows
start
end
span_days
stations
unique_hours
production_training_ready
```

`production_training_ready=true` oznacza wyłącznie, że zakres wynosi co
najmniej 365 dni. Nie zastępuje kontroli luk, liczby stacji i walidacji modelu.

## 8. Kolejność po pobraniu historii

Nie uruchamiaj od razu pełnego `first-run`. Najpierw:

```powershell
& $Python -m smog_ai backup --tier daily --config $Config --env-file $EnvFile
& $Python -m smog_ai validate --config $Config --env-file $EnvFile
& $Python -m smog_ai match-stations --config $Config --env-file $EnvFile
& $Python -m smog_ai upload-operational-data --config $Config --env-file $EnvFile
& $Python -m smog_ai build-hourly-features --source object_store --config $Config --env-file $EnvFile
& $Python -m smog_ai train-hourly --config $Config --env-file $EnvFile
```

Postęp treningu:

```powershell
& $Python -m smog_ai progress --watch --refresh-seconds 5 --config $Config --env-file $EnvFile
```

Po treningu sprawdź:

```powershell
& $Python -m smog_ai hourly-readiness --config $Config --env-file $EnvFile
```

Modele PM10 i PM2.5 mogą zostać zastąpione tylko wtedy, gdy kandydat rzeczywiście
pokona `persistence` na chronologicznej walidacji. Nie należy wymuszać modelu
złożonego tylko po to, aby wykres był zmienny.

## 9. Ważne ograniczenia jakości

- Archiwalne wyniki jednogodzinne GIOŚ są publikowane w stałym CET, również dla
  dat letnich. Importer zamienia je jednoznacznie na UTC.
- Dane bieżące i archiwalne mogą później zostać zweryfikowane przez GIOŚ.
- Dla modelu PM z pogodą potrzebny jest wspólny zakres czasowy GIOŚ i IMGW.
- Sam duży licznik rekordów nie wystarcza; konieczne są pokrycie czasowe,
  liczba stacji, brak istotnych luk i chronologiczna walidacja.
- Jeśli po pełnym treningu nadal wygrywa `persistence`, oznacza to, że kandydaci
  nie wykazali przewagi w walidacji. Takiego wyniku nie wolno sztucznie zmieniać.
