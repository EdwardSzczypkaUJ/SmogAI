# Generyczna platforma parametrów jakości powietrza

## 1. Cel

Platforma rozdziela pięć niezależnych ról parametru GIOŚ:

1. `collect_current` — pobieranie danych bieżących;
2. `historical_backfill` — pobieranie archiwów lub rocznego API;
3. `auxiliary_feature` — użycie parametru jako cechy pomocniczej;
4. `forecast_target` — trenowanie i publikacja modelu tego parametru;
5. `spatial_surface` — budowa ogólnopolskiej powierzchni przestrzennej.

Włączenie pobierania `NO2` nie uruchamia automatycznie treningu `NO2`. Model
powstaje dopiero po włączeniu roli `forecast_target` i dodaniu kodu do
`hourly_forecasting.targets`.

## 2. Centralny rejestr

Każda definicja zawiera kod kanoniczny, aliasy, jednostkę, kadencję,
ograniczenia wartości, kod używany przez roczne API, tokeny nazw plików ZIP i
listę dozwolonych providerów modeli.

Wbudowane definicje obejmują:

```text
PM10, PM2.5, NO2, SO2, O3, CO, C6H6, NO, NOX
```

Domyślnie pobierane i modelowane pozostają PM10 i PM2.5. Pozostałe definicje
są dostępne, lecz ich role pobierania/modelowania są wyłączone, aby aktualizacja
nie uruchomiła niekontrolowanej liczby zapytań ani modeli.

## 3. Aliasowanie

Rejestr normalizuje między innymi:

```text
PM25, PM2,5, PM2.5              -> PM2.5
NO₂, dwutlenek azotu            -> NO2
SO₂, dwutlenek siarki           -> SO2
O₃, ozon                         -> O3
C₆H₆, benzen                     -> C6H6
```

Aliasowanie jest wspólne dla katalogu sensorów, importera historycznego, CLI,
textboxa i modelu.

## 4. Bieżące pobieranie

```powershell
python -m smog_ai collect-gios `
  --parameters "NO2,O3" `
  --config $Config `
  --env-file $EnvFile
```

Brak `--parameters` oznacza parametry z rolą `collect_current=true`. `ALL`
również oznacza tę skonfigurowaną rolę, a nie bezwarunkowo wszystkie sensory
z metadanych GIOŚ.

## 5. Historia

```powershell
python -m smog_ai backfill-gios-history `
  --start-year 2024 `
  --end-year 2026 `
  --pollutants "NO2,O3" `
  --source auto `
  --config $Config `
  --env-file $EnvFile
```

`--pollutants ALL` rozwija się do parametrów z
`historical_backfill=true`. Import pozostaje idempotentny, korzysta z Bridge
cache `local/object_store/hybrid` i współpracuje z RangeAwareBackfillBridge.

## 6. Generyczny cel modelu

Dla dowolnego parametru powietrza `p` model uczy się:

```text
Y_p(s,t,h) = value_p(s,t+h)
```

z dokładnym `target_time=t+h`. Wspólny zestaw cech zawiera historię parametru,
pogodę, lokalizację, czas docelowy i opcjonalne cechy innych parametrów z rolą
`auxiliary_feature=true`.

Do trenowania tylko wybranych celów:

```powershell
python -m smog_ai train-hourly `
  --profile quick `
  --targets "NO2" `
  --config $Config `
  --env-file $EnvFile
```

Pozostałe aktywne modele nie są dezaktywowane.

## 7. Cechy pomocnicze

Dla parametru pomocniczego `q` powstają cechy:

```text
aux_<q>_value
aux_<q>_lag_1
aux_<q>_lag_6
aux_<q>_lag_24
```

Cechy są łączone po `air_station_id` i dokładnej godzinie origin. Braki są
zachowywane jako `NaN` i obsługiwane przez provider modelu; nie wykonuje się
przecieku danych z przyszłości.

## 8. Snapshot, mapy i textbox

Snapshot publikuje bieżące wartości wszystkich parametrów z rolą pobierania,
modelowania lub mapowania. Manifest powierzchni określa listę dostępnych
parametrów. Parser textboxa ogranicza wybór do parametrów faktycznie
opublikowanych w manifeście, dzięki czemu po włączeniu modelu `NO2` zapytanie
„jutro o 12:00 podaj dwutlenek azotu w Katowicach” wybiera `NO2` bez zmiany
kodu serwera.

## 9. Bezpieczna procedura dodania nowego parametru

1. Dodać lub aktywować definicję w `air_parameters.parameters`.
2. Włączyć `collect_current` i wykonać pobranie bieżące.
3. Włączyć `historical_backfill` i uzupełnić luki.
4. Uruchomić audyt zakresów, jednostek, stacji i duplikatów.
5. Opcjonalnie włączyć `auxiliary_feature`.
6. Dopiero po przejściu progu danych włączyć `forecast_target`.
7. Wykonać trening `quick`, bramę jakości wobec persistence, następnie `full`.
8. Po akceptacji włączyć `spatial_surface` i opublikować mapy.

## 10. Postęp i ETA

Kolektor i trening raportują kod parametru jako część bieżącego zadania.
Postęp nie jest liczony jako liczba parametrów, lecz jako ważone jednostki:
strony API, serie stacji, foldy walidacyjne, providery, kwantyle i powierzchnie.
