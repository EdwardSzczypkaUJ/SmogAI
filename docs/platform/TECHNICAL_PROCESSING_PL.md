# Smog AI 1.7.0 — dokumentacja techniczna przetwarzania

## 1. Co system oblicza i gdzie

System ma dwie jasno rozdzielone warstwy:

- **komputer lokalny** pobiera GIOŚ i IMGW, przechowuje pełną historię w SQLite, waliduje, trenuje, wykonuje prognozy godzinowe, interpoluje powierzchnie Polski i publikuje artefakty;
- **DigitalOcean Spaces + App Platform** przechowują i prezentują gotowe wyniki. FastAPI i Streamlit nie wywołują `model.predict()` i nie wykonują interpolacji.

```text
GIOŚ JSON-LD + IMGW bieżące + oficjalne archiwum IMGW
        ↓
normalizacja UTC i kontrakty danych
        ↓
SQLite WAL + UNIQUE + transakcje
        ↓
Pandera + kontrola jakości + dopasowanie Haversine
        ↓
Spaces: kompletny pakiet Bronze
        ↓
lokalny ponowny odczyt ze Spaces
        ↓
cechy godzinowe + otwarty rejestr modeli
        ↓
prognozy stacyjne h=1..48
        ↓
interpolacja przestrzenna każdej dokładnej godziny
        ↓
Spaces: modele, metryki, prognozy, mapy i dokumentacja
        ↓
FastAPI → Streamlit → użytkownik
```

## 2. Konfiguracja i ścieżki

Projekt może znajdować się w dowolnym katalogu. Skrypty ustalają repozytorium przez `$PSScriptRoot` lub `SMOG_AI_PROJECT_ROOT`. Dane uruchomieniowe są oddzielone od kodu:

```text
%ProgramData%\SmogAI\config.yaml
%ProgramData%\SmogAI\smog-ai.env
%ProgramData%\SmogAI\server-local.env
%ProgramData%\SmogAI\data\smog.db
%ProgramData%\SmogAI\logs\
%ProgramData%\SmogAI\models\
```

Sekrety nie znajdują się w repozytorium ani w argumentach procesu.

## 3. GIOŚ

Kolektor:

1. pobiera stronicowaną listę stacji w JSON-LD;
2. pobiera sensory każdej stacji;
3. wybiera PM10 i PM2.5;
4. pobiera bieżące serie;
5. normalizuje czas do UTC;
6. zachowuje diagnostyczne `raw_json`;
7. zapisuje dane idempotentnie.

Błąd pojedynczego sensora nie unieważnia całej kolekcji. Niedostępne stanowiska historyczne są klasyfikowane jako pominięte/ostrzeżenia, a rzeczywiste awarie trafiają do `collection_errors`.

## 4. IMGW bieżące i archiwalne

### 4.1. Bieżący SYNOP

Najnowsze obserwacje są pobierane z publicznego endpointu SYNOP. Opad otrzymuje jawne pole:

```text
precipitation_accumulation_period_hours
```

W domyślnej konfiguracji opad oznacza **akumulację 6 h kończącą się w czasie pomiaru**. System nie dzieli wartości przez sześć i nie tworzy sztucznego rozkładu godzinowego.

### 4.2. Oficjalne archiwum terminowe/SYNOP

Adapter archiwalny:

1. wyznacza miesiące z `lookback_months` albo zakresu lat;
2. odczytuje listing rocznych katalogów IMGW;
3. pobiera miesięczne ZIP-y do cache;
4. liczy SHA-256 i sprawdza integralność ZIP;
5. czyta CSV według dołączonego oficjalnego nagłówka;
6. normalizuje stację i czas;
7. zapisuje temperaturę, wilgotność, ciśnienie, wiatr i opad;
8. zachowuje kody jakości i źródło w `raw_json`;
9. zapisuje checksum przetworzonego pliku w `application_state`.

Ponowne uruchomienie pomija niezmieniony plik. Kody pól są konfigurowalne, więc adapter można dostosować lub zastąpić.

## 5. SQLite

Baza używa WAL, `busy_timeout`, migracji Alembic i transakcji. Najważniejsze niezmienniki:

- pomiar jest unikalny według źródła, stacji/sensora, parametru i czasu;
- prognoza jest unikalna według modelu, stacji, parametru, czasu utworzenia, czasu docelowego i horyzontu;
- prognoza powstaje przed czasem docelowym;
- podejrzane dane nie są automatycznie kasowane — otrzymują flagi jakości.

## 6. Walidacja Pandera

Osobne kontrakty obejmują:

- dane GIOŚ;
- dane IMGW;
- pakiet operacyjny;
- godzinowe ramki treningowe;
- prognozy;
- snapshoty i powierzchnie.

Raport zawiera czas, wiersze, kolumny, przypadki błędów i checksum. Błąd kontraktu treningowego blokuje aktywację modelu.

## 7. Bronze, Curated, Serving

### Bronze

`operational-data.json.gz` zawiera stacje, sensory, pomiary i dopasowania. Manifest ma `run_id`, checksum, zakres danych, liczności i status kompletności.

### Curated

Dla celów `PM10`, `PM2.5`, `temperature_c` i `precipitation_mm` powstają wersjonowane ramki godzinowe. Każdy rekord ma:

```text
measurement_time = origin_time
target_time
horizon_hours = target_time - origin_time
cechy dostępne w origin_time
target
```

### Serving

Warstwa Serving zawiera modele, model cards, metryki, gotowe prognozy, mapy i dokumentację. App Platform korzysta wyłącznie z niej.

### Bezpieczeństwo ramek na danych rzeczywistych

Normalizacja godzinowa zachowuje luki potrzebne do obliczania lagów, ale wiersz
z brakującą wartością bieżącą nie staje się automatycznie obserwacją uczącą.
Dla PM10, PM2.5 i temperatury origin musi zawierać rzeczywistą wartość bieżącą.
Dla opadu sześciogodzinnego brak bieżącej akumulacji jest dopuszczalny, ponieważ
WO6G jest raportowane tylko w wybranych terminach, a provider hurdle imputuje
brakujące cechy bez wymyślania godzinowego opadu.

Modele temperatury i opadu są trenowane na jednej serii dla każdej unikalnej
stacji IMGW. Dopasowania wielu stacji GIOŚ do tej samej stacji IMGW nie powielają
obserwacji pogodowych. Dla PM dopasowanie jest nadal używane, ponieważ pogoda
stanowi cechę konkretnej stacji jakości powietrza.

Długa reprezentacja `origin × horyzont` ma konfigurowalny limit
`maximum_training_rows_per_target`. Originy są wybierane deterministycznie i
proporcjonalnie według stacji przed ekspansją, natomiast target jest zawsze
dołączany z pełnej serii po dokładnym `target_time`. Ogranicza to pamięć bez
zmiany znaczenia horyzontu.

## 8. Cechy

Cechy obejmują:

- lagi 1, 3, 6, 12, 24 h;
- średnie i odchylenia kroczące;
- tempo zmian;
- wilgotność, ciśnienie, wiatr i jego składowe;
- lokalizację i dopasowanie stacji;
- dokładny horyzont 1–48 h;
- sinus/cosinus godziny, dnia tygodnia i roku czasu docelowego.

Dla opadu `precipitation_mm` oznacza akumulację w skonfigurowanym okresie, domyślnie `mm / 6 h`, kończącą się w `target_time`.

## 9. Otwarta platforma modeli

Domena korzysta z Bridge `ModelProvider`:

```python
class ModelProvider(Protocol):
    name: str
    task: ModelTask

    def fit(self, X, y, *, context): ...
    def predict(self, artifact, X, *, context) -> PredictionBundle: ...
    def describe(self, artifact) -> dict: ...
```

Provider nie zna SQLAlchemy, Spaces, FastAPI ani Streamlit. Można go dołączyć przez:

- `register_models(registry)`;
- entry point `smog_ai.model_providers`;
- `module:object` w konfiguracji.

Platforma może więc korzystać z ridge, Huber, regresji wielomianowej czasu, GAM, splajnów, XGBoost, LightGBM, CatBoost, PyTorch, TCN, Transformera lub modelu grafowego bez przebudowy domeny.

## 10. Trening bez wycieku

Kolejność:

1. temperatura;
2. opad typu hurdle;
3. chronologiczne prognozy out-of-fold pogody;
4. dołączenie tych prognoz do danych PM;
5. PM10 i PM2.5;
6. porównanie kandydatów z persistence;
7. zapis artefaktu i atomowa aktywacja.

Rzeczywista pogoda z przyszłości nie jest cechą modelu PM. Przy zbyt krótkiej historii działa jawny fallback `insufficient_history`.

## 11. Dokładna prognoza godzinowa

Dla wspólnego `origin_time` system tworzy horyzonty:

```text
h=1, h=2, ..., h=48
```

oraz dokładne:

```text
target_time = origin_time + h
```

Nie wybiera najbliższego modelu 6/12/24 h. Dla czasu pomiędzy pełnymi godzinami może użyć interpolacji liniowej albo PCHIP tylko pomiędzy sąsiednimi prognozami. Ekstrapolacja jest domyślnie zabroniona.

## 12. Opad

Model hurdle zwraca:

- prawdopodobieństwo opadu;
- warunkową sumę przy opadzie;
- wartość oczekiwaną.

Semantyka okresu akumulacji jest zawsze publikowana. W domyślnej konfiguracji karta pokazuje `mm / 6 h`, a nie `mm/h`.

## 13. Interpolacja Polski

Dla każdego parametru i każdego dokładnego `target_time` powstaje osobna powierzchnia. Interpolator jest wymienny (`IDW`, `RBF`, kolejne implementacje). API zwraca:

- współrzędne miasta;
- środek komórki siatki;
- odległość miasta od środka komórki;
- stacje użyte do interpolacji;
- odległość do najbliższej stacji;
- algorytm i pewność.

## 14. Spaces

Najpierw zapisywany jest niezmienny obiekt wersji. Dopiero po pełnym uploadzie i kontroli checksum aktualizowany jest wskaźnik `latest.json`. Najważniejsze prefiksy:

```text
datasets/bronze/
datasets/curated-hourly/
models-hourly/
metrics/hourly-models/
forecasts/
maps/
documentation/
```

## 15. FastAPI i dashboard

FastAPI interpretuje pytanie, rozwiązuje miejsce i odczytuje pakiet o dokładnym czasie. Brak pakietu daje jawną informację o niedostępności — bez cichego wyboru najbliższej godziny.

Dashboard pokazuje:

- mapę 2D i opcjonalne ograniczone 3D;
- nazwy miast nad warstwami;
- PM10, PM2.5, temperaturę i opad;
- dokładny punkt interpolacji;
- profile godzinowe;
- aktywne modele, metryki i providerów;
- dokumentację Markdown i źródła LaTeX.

## 16. Idempotencja i awarie

Bezpieczne powtórzenie zapewniają:

- UNIQUE w SQLite;
- checksum archiwów i artefaktów;
- niezmienne klucze wersji;
- outbox z exponential backoff;
- mutex Windows i dzierżawa w bazie;
- atomowe wskaźniki aktywnych modeli i powierzchni.

## 17. Harmonogram Windows

- **godzinowo:** bieżące dane, walidacja, weryfikacja, prognozy, mapy, publikacja;
- **dziennie:** uzupełnianie historii IMGW/GIOŚ, jakość, outbox, backup;
- **tygodniowo:** cloud round-trip, trening, walidacja i aktywacja;
- **miesięcznie:** integralność, backup i retencja.

Skrypty używają pełnych ścieżek, `.venv\Scripts\python.exe`, blokady, limitów czasu i osobnych logów.

## 18. Backup i odzyskiwanie

SQLite jest kopiowana przez Online Backup API. Backup ma SHA-256 i wynik `PRAGMA quick_check`. Pipeline, import archiwów i publikacja są idempotentne, więc po odbudowie można bezpiecznie ponowić kolekcję.

## 19. Polecenia

```powershell
python -m smog_ai backfill-imgw-archive
python -m smog_ai upload-operational-data
python -m smog_ai build-hourly-features --source object_store
python -m smog_ai list-model-methods
python -m smog_ai train-hourly
python -m smog_ai predict-hourly
python -m smog_ai publish-documentation
python -m smog_ai storage-health
python -m smog_ai hourly-readiness
python -m smog_ai healthcheck --json
```

Pełne źródło techniczne LaTeX jest dostępne jako `DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex` oraz przez endpoint `/api/v1/docs/processing/source`.

## 20. Ograniczony trening, szybkie douczanie i drift (HF16)

Pełna historia pozostaje w SQLite i, zależnie od wybranego Bridge, może być
również archiwizowana w ObjectStore. Ograniczeniu podlega wyłącznie ramka
materializowana dla konkretnego przebiegu treningowego.

### 20.1. Bridge polityki zbioru uczącego

Interfejs `TrainingSetPolicy` ma implementacje:

```text
FullHistoryPolicy
RollingWindowPolicy
BoundedRollingStratifiedPolicy — domyślna
```

Domyślna polityka wykonuje kolejno:

```text
pełna historia
→ okno czasowe zależne od celu
→ zachowanie wszystkich najnowszych obserwacji
→ zachowanie zdarzeń rzadkich
→ deterministyczne próbkowanie warstwowe
→ limit liczby wierszy
→ wagi: świeżość × stacja × horyzont
```

Próbkowanie jest powtarzalne dla tego samego `random_state`. Warstwy obejmują
stację, koszyk horyzontu, miesiąc oraz kwantyl wartości docelowej.

### 20.2. Profile quick i full

| Właściwość | quick | full |
|---|---:|---:|
| PM: maksymalne okno | 365 dni | 730 dni |
| pogoda: maksymalne okno | 730 dni | 1095 dni |
| maks. wierszy na cel | 250 000 | 600 000 |
| maks. walidacji | 60 000 | 120 000 |
| horyzonty na jeden origin | maks. 8 | maks. 12 |
| foldy cross-fit | 2 | 4 |
| modele kwantylowe | nie | tak |
| budżet ścienny | 30 min | 120 min |

Profil `quick` służy do tygodniowego odświeżenia. Profil `full` służy do
okresowego konkursu champion/challenger. Przekroczenie budżetu nie kasuje
poprawnie ocenionych kandydatów; pipeline może aktywować najlepszego już
zwalidowanego kandydata i oznacza model jako `budget_truncated`.

### 20.3. Ograniczenie ekspansji h1--h48

Dla jednego czasu bazowego nie tworzymy bezwarunkowo 48 rekordów. Horyzonty są
podzielone na koszyki:

```text
1–6, 7–12, 13–24, 25–48
```

W profilu quick wybierane są deterministycznie po dwa horyzonty z każdego
koszyka. W kolejnych originach wybór ma inną fazę, dlatego globalnie wszystkie
horyzonty 1--48 pozostają reprezentowane.

### 20.4. Wagi obserwacji

Każdy wybrany rekord otrzymuje wagę łączącą:

```text
zanik ważności wraz z wiekiem
równoważenie liczby obserwacji stacji
równoważenie liczby obserwacji horyzontu
```

Wagi są normalizowane do średniej 1 i ograniczane do bezpiecznego przedziału
`[0.1, 10]`. Provider może wykorzystać `sample_weight`; provider bez takiej
obsługi nadal korzysta z warstwowego doboru próby.

### 20.5. Lekki korektor reszt

Pełny model nie jest uczony co godzinę. Po zgromadzeniu zweryfikowanych
prognoz można wykonać:

```powershell
python -m smog_ai update-hourly-residuals
```

Korektor `SGDRegressor.partial_fit()` uczy się reszty względem aktywnego
championa. Nowa wersja jest aktywowana wyłącznie wtedy, gdy na późniejszej
części chronologicznej zmniejsza MAE o skonfigurowany próg. Korektor nie
nadpisuje modelu bazowego; jest wersjonowany razem z całym artefaktem.

### 20.6. Detekcja driftu

```powershell
python -m smog_ai hourly-drift-status
```

Porównywane są dwa kolejne okna zweryfikowanych prognoz. Alarm powstaje, gdy:

```text
MAE wzrasta relatywnie powyżej progu
lub
bezwzględny bias przekracza próg celu
```

Wynik `retrain_recommended=true` jest sygnałem operacyjnym. Nie uruchamia sam
pełnego treningu w środku obsługi zapytania.

### 20.7. Harmonogram

```text
co godzinę  — kolekcja, predykcja h1–h48, mapy; bez treningu
codziennie  — weryfikacja, partial_fit korektora, drift, backup
co tydzień  — profil quick
co miesiąc — profil full
```

### 20.8. Polecenia i progress/ETA

```powershell
python -m smog_ai training-policy-status --profile quick
python -m smog_ai train-hourly --profile quick
python -m smog_ai train-hourly --profile full
python -m smog_ai update-hourly-residuals
python -m smog_ai hourly-drift-status
```

Skrypty Windows:

```text
scripts/Run-QuickRetrain.ps1
scripts/Run-FullRetrain.ps1
scripts/Run-IncrementalUpdate.ps1
scripts/Watch-TrainingProgress.ps1
```

Każdy kosztowny etap zapisuje trwały postęp, bieżące zadanie, procent i ETA w
`logs/progress`. Zamknięcie monitora nie zatrzymuje procesu treningowego.


## 21. RangeAwareBackfillBridge — pobieranie wyłącznie brakujących zakresów

Przed każdym backfillem system ponownie oblicza pokrycie bezpośrednio z
lokalnej SQLite. Zakres z wcześniejszego raportu jest jedynie zakresem
żądanym; `state.json` ani istnienie pliku w cache nie są traktowane jako dowód
kompletności.

```text
zakres żądany
→ audyt SQLite z kadencją parametru
→ plan źródłowy
→ kontrola przed akcją
→ cache local/object_store/hybrid
→ zapis tylko brakujących godzin
→ kontrola po akcji
```

Dla `WO6G` oczekiwane są sloty co sześć godzin. Regularne pięciogodzinne
odstępy pomiędzy sumami nie są lukami i nie uruchamiają pobierania.

Próg liczby stacji jest elementem zakresu żądanego. Jeżeli wcześniejszy audyt
wykorzystał pięć stacji, ten sam próg jest odtwarzany z pakietu. Użytkownik może
go nadpisać dla AIR i pogody oddzielnie.

Dostępne polecenia:

```powershell
python -m smog_ai data-range-audit
python -m smog_ai plan-missing-ranges --audit-package data-ranges.zip
python -m smog_ai fill-missing-ranges --audit-package data-ranges.zip
python -m smog_ai progress --run-type range-backfill --watch
```

Provider Bridge obejmuje GIOŚ bieżące, GIOŚ przygotowane ZIP-y, GIOŚ API
roczne, IMGW bieżące oraz IMGW archiwalne w układzie miesięcznym albo
stacja--rok. Po dwóch próbach bez zwiększenia pokrycia luka pozostaje jawna,
ale źródło nie jest odpytywane w nieskończoność.

## 22. Generyczny rejestr parametrów jakości powietrza

Od HF18 lista parametrów nie jest zaszyta w kolektorze ani w modelu. Centralny
`AirParameterRegistry` przechowuje kod kanoniczny, aliasy, jednostkę, kadencję,
zakres walidacji, tokeny plików archiwalnych i niezależne role:

```text
collect_current
historical_backfill
auxiliary_feature
forecast_target
spatial_surface
```

Włączenie pobierania nie tworzy automatycznie modelu. Bezpieczna sekwencja to:

```text
definicja
→ bieżące pobieranie
→ backfill i audyt zakresów
→ opcjonalna cecha pomocnicza
→ trening quick i brama jakości
→ trening full
→ warstwa przestrzenna
```

Wbudowany katalog obejmuje PM10, PM2.5, NO2, SO2, O3, CO, C6H6, NO i NOX.
Domyślnie aktywne pozostają wyłącznie role dotychczasowych PM10 i PM2.5.
Nowy kod może zostać dodany konfiguracyjnie bez modyfikacji kolektora lub
pipeline'u treningowego.

Dla celu `p` i pomocniczego parametru `q` generowane są cechy dostępne w czasie
origin:

```text
aux_<q>_value
aux_<q>_lag_1
aux_<q>_lag_6
aux_<q>_lag_24
```

Publiczny manifest i snapshot zawierają katalog nazw, aliasów i jednostek.
Dzięki temu parser textboxa może rozpoznać także alias parametru dodanego przez
konfigurację, nie tylko kody wbudowane w serwer.

Polecenia operacyjne:

```powershell
python -m smog_ai air-parameter-catalog
python -m smog_ai collect-gios --parameters "NO2,O3"
python -m smog_ai backfill-gios-history --pollutants "NO2,O3"
python -m smog_ai train-hourly --profile quick --targets "NO2"
```

Skrypt `scripts/Set-AirParameterRoles.ps1` modyfikuje role atomowo, tworzy
backup `config.yaml` i waliduje plik tymczasowy przed zastąpieniem konfiguracji.

## HF20 — 48 godzin serwowania i horyzont modelu do h60

Szczegółowy kontrakt czasu, rozszerzoną bramę jakości opadu, integrację MLflow,
Langfuse oraz jawny proces publikacji DigitalOcean opisuje:

- `docs/platform/HF20_TIME_CONTRACT_MLOPS_PL.md`,
- `docs/latex/DODATEK_TECHNICZNY_HF20_TIME_CONTRACT_MLOPS_PL.tex`.

Najważniejsza zasada: `forecast_horizon` oznacza lead serwujący `1..48`, a
`model_horizon_hours` rzeczywistą odległość od czasu źródłowego, maksymalnie
`60`. Surowe dane, SQLite i snapshoty nie są objęte publikacją modeli.
