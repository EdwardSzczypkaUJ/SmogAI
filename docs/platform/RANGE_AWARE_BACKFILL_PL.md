# Smog AI — uzupełnianie wyłącznie brakujących zakresów

## 1. Cel

Mechanizm `RangeAwareBackfillBridge` oddziela cztery stany:

1. plik lub strona API istnieje w cache;
2. dane istnieją w SQLite;
3. zakres jest kompletny na poziomie dostępności źródła;
4. zakres spełnia późniejszy próg jakości modelu.

SQLite jest źródłem prawdy dla kompletności. `state.json`, lokalny cache i
DigitalOcean Spaces przyspieszają transport, ale nigdy nie zastępują świeżego
audytu bazy.

## 2. Przepływ

```text
żądany zakres
→ audyt SQLite z poprawną kadencją parametru
→ plan akcji według źródła
→ kontrola SQLite przed każdą akcją
→ lokalny/ObjectStore/hybrydowy cache
→ import tylko wartości z brakujących przedziałów
→ kontrola SQLite po akcji
→ raport luk rezydualnych
```

## 3. Bridge źródeł

```text
BackfillProvider
├── GiosLiveBackfillProvider
├── GiosHistoricalBackfillProvider(prepared)
├── GiosHistoricalBackfillProvider(api)
├── ImgwLiveBackfillProvider
└── ImgwArchiveRangeBackfillProvider
```

Backend cache pozostaje wymienny:

```text
local
object_store
hybrid
```

Wariant `hybrid` sprawdza najpierw lokalny cache, potem ObjectStore/Spaces, a
na końcu źródło oficjalne.

## 4. Granularność źródła

Nie każde źródło pozwala pobrać dokładnie jedną brakującą godzinę.

- GIOŚ przygotowane archiwum: naturalna jednostka pobrania to roczny ZIP;
  ZIP nie jest pobierany ponownie, jeżeli jest w cache, a parser zapisuje tylko
  godziny należące do luk.
- GIOŚ API roczne: naturalna jednostka to rok × województwo × parametr × strona;
  strony są cache'owane, a do SQLite trafiają tylko żądane czasy.
- IMGW miesięczne ZIP-y: pobierane są wyłącznie miesiące przecinające luki.
- IMGW stacyjne ZIP-y roczne: pobierane są pliki stacji dla roku, gdy katalog
  nie zawiera miesięcznych archiwów sieciowych.
- Dane bieżące: kolektor uruchamia się tylko wtedy, gdy istnieje luka w jego
  oknie publikacyjnym.

## 5. Opad

`WO6G` jest sumą z sześciu godzin kończącą się w czasie pomiaru. Audyt nie
oczekuje sztucznego rekordu w każdej godzinie. Oczekiwane sloty są wyznaczane
co 6 godzin z fazą odtworzoną z istniejących rekordów.

Nie jest wykonywana deagregacja:

```text
6 mm / 6 h ≠ sześć wymyślonych rekordów po 1 mm
```

## 6. Duplikaty i uzupełnianie pól pogody

AIR pozostaje chronione naturalnym kluczem:

```text
(source, source_sensor_id, parameter, measurement_time)
```

Dla pogody konflikt:

```text
(source, source_station_id, measurement_time)
```

nie kończy się bezwarunkowym `DO NOTHING`. Nowy merge wypełnia wyłącznie
kolumny, które wcześniej były `NULL`. Znana temperatura nie jest zastępowana
inną temperaturą, ale brakujący opad lub ciśnienie może zostać dopisane z
oficjalnego archiwum.

## 7. Ochrona przed nieskończonym pobieraniem

Dla każdej akcji zapisywane są:

```text
missing_hours_before
missing_hours_after
no_progress_count
provider
last_status
```

Po domyślnie dwóch próbach bez poprawy akcja otrzymuje status
`skipped_no_progress`. Luka nadal pozostaje w raporcie, ale program nie pobiera
tego samego źródła w nieskończoność.

## 8. Progi kompletności

Mechanizm rozróżnia obecność pojedynczego pomiaru od pokrycia sieci. Audyt
może wymagać np. pięciu różnych stacji na slot. Dla pakietu `data-ranges.zip`
próg jest odtwarzany z metadanych audytu. Jeżeli oficjalne źródło po dwóch
próbach nadal nie zwiększa liczby stacji, zakres pozostaje jawnie częściowy,
ale nie jest pobierany bez końca.

## 9. Progress i ETA

Trwały stan:

```text
logs/progress/range-backfill-current.json
logs/progress/range-backfill-<run_id>.json
logs/progress/range-backfill-<run_id>.jsonl
```

Etapy mają wagi:

```text
audit   5%
plan    5%
execute 85%
verify  5%
```

Monitor:

```powershell
.\scripts\Watch-MissingRangeBackfill.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot
```

## 10. Uruchomienie na dostarczonym `data-ranges.zip`

Najpierw plan bez zmian:

```powershell
.\scripts\Run-MissingRangeBackfill.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -AuditPackage "C:\ścieżka\data-ranges.zip" `
    -CacheMode local `
    -DryRun
```

Następnie wykonanie:

```powershell
.\scripts\Run-MissingRangeBackfill.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -AuditPackage "C:\ścieżka\data-ranges.zip" `
    -CacheMode local
```

Zakres z pliku jest jedynie zakresem żądanym. Program zawsze ponownie sprawdza
aktualną SQLite przed planem i przed każdą akcją. Jeżeli pakiet audytu zawiera
progi `minimum_air_stations_per_hour` oraz `minimum_weather_stations_per_hour`,
są one zachowywane. Można je jawnie nadpisać parametrami
`-MinimumAirStations` i `-MinimumWeatherStations`.

## 11. Raporty

```text
logs/range-backfill/range-backfill-plan-....json
logs/range-backfill/range-backfill-result-....json
logs/range-backfill/range-backfill-coverage-after-....json
```

Status `partial_success` jest uczciwym wynikiem, gdy oficjalne źródło nie
opublikowało jeszcze danych lub po ponownej próbie nie zwiększyło pokrycia.
