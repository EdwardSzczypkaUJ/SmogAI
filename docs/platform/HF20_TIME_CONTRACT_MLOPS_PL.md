# Smog AI HF20 — kontrakt czasu, brama opadu, MLflow, Langfuse i DigitalOcean

## 1. Cel wydania

HF20 rozdziela godzinę prezentowaną użytkownikowi od rzeczywistego horyzontu
modelu. System ma zawsze zwracać **48 przyszłych pełnych godzin**, nawet gdy
najświeższe wspólne dane GIOŚ/IMGW są opóźnione. Dodatkowo wprowadza:

- trening modelowych horyzontów `h1–h60`;
- twardą, wielometryczną bramę jakości opadu;
- lokalne porównanie modeli i opcjonalny tracking MLflow;
- tracing pytań i ocenę odpowiedzi przez opcjonalny Langfuse;
- jawnie zatwierdzaną publikację wyłącznie modeli i artefaktów serwujących;
- brak automatycznego wysyłania surowych danych, SQLite i snapshotów.

## 2. Kontrakt czasu

Wprowadzamy trzy czasy:

- `source_origin_time` — ostatnia wspólna godzina danych wejściowych;
- `forecast_created_at` — chwila utworzenia pakietu prognoz;
- `serving_anchor_time` — najbliższa pełna godzina **ściśle po** chwili
  utworzenia prognozy.

Dla kolejnej godziny użytkownika definiujemy:

\[
L_s \in \{1,\ldots,48\},
\]

oraz modelowy horyzont:

\[
H_m = \frac{t_{target}-t_{source}}{1\mathrm{h}}.
\]

Przy opóźnieniu danych do 12 godzin i 48 godzinach serwowania wymagany jest
zakres modelowy do:

\[
H_{m,\max}=48+12=60.
\]

Przykład dla danych z 09:00 i uruchomienia o 13:55:

| Pole | lead 1 | lead 48 |
|---|---:|---:|
| `serving_lead_hours` | 1 | 48 |
| `target_time` | 14:00 | 13:00 dwa dni później |
| `model_horizon_hours` | 5 | 52 |

W bazie `Forecast.forecast_horizon` zachowuje semantykę serwującą `1–48`.
Rzeczywisty horyzont modelu, wiek źródła i kotwica serwowania znajdują się w
`features_json`.

## 3. Warunki bezpieczeństwa czasu

Predykcja jest odrzucana, gdy:

- źródło jest starsze niż `maximum_source_delay_hours`;
- aktywny model nie deklaruje obsługi wszystkich wymaganych horyzontów;
- modelowy horyzont przekracza `maximum_model_horizon_hours`;
- `target_time` nie jest przyszłą pełną godziną.

Konfiguracja HF20:

```yaml
hourly_forecasting:
  serving_horizon_hours: 48
  maximum_source_delay_hours: 12
  maximum_model_horizon_hours: 60
```

## 4. Trening h1–h60

Pełne dane pozostają w lokalnej SQLite. Profil `quick` nadal ogranicza próbkę,
ale koszyki horyzontów obejmują bufor opóźnienia:

```yaml
horizon_bucket_edges: [6, 12, 24, 48, 60]
```

Nie każda obserwacja jest mnożona przez wszystkie 60 horyzontów. Wybór jest
warstwowy i deterministyczny, lecz w całej próbce reprezentowane są horyzonty
`1–60`.

Do pierwszego przeliczenia HF20 można użyć tego samego, zweryfikowanego
snapshotu HF19.2:

```text
snapshot = latest
```

Nie trzeba ponownie pobierać danych ani tworzyć następnej kopii SQLite.

## 5. Brama jakości opadu

Opad ma dwuczęściowy model hurdle:

1. klasyfikacja wystąpienia opadu;
2. regresja ilości opadu pod warunkiem wystąpienia.

Brama wymaga jednocześnie:

- poprawy MAE względem persistence;
- dodatniego Brier Skill Score względem klimatologii;
- dodatniego Brier Skill Score względem persistence;
- `ROC AUC >= 0.60`;
- bezwzględnego biasu nie większego niż skonfigurowany próg.

Model niespełniający bramy ma status `experimental`. Może pozostać aktywny
lokalnie do testowania wykresów, lecz **nie może zostać opublikowany** przez
`publish-approved-models`.

Semantyka opadu zachowuje okres akumulacji:

```text
accumulation_period_hours
ending_at_target_time = true
disaggregated_to_hourly = false
```

## 6. MLflow — local-first

MLflow jest opcjonalnym Bridge. Domyślnie jest wyłączony. Po świadomym
włączeniu lokalny serwer używa:

```text
C:\ProgramData\SmogAI\mlflow\mlflow.db
C:\ProgramData\SmogAI\mlflow\artifacts
http://127.0.0.1:5000
```

Rejestrowane są:

- cel i provider;
- profil treningowy;
- parametry próby i horyzontów;
- MAE, RMSE, bias, metryki opadu;
- `dataset_id` i SHA-256 snapshotu;
- kandydat wybrany jako aktywny.

Niezależnie od obecności pakietu MLflow system tworzy lokalny artefakt
`model-comparison.json`, dzięki któremu aplikacja może porównywać modele bez
uruchamiania kosztownego serwera MLflow w chmurze.

### Opcjonalny MLflow na DigitalOcean

Pełny współdzielony Model Registry wymaga trwałego backendu bazodanowego i
trwałego magazynu artefaktów. Nie jest dodawany do domyślnego App Spec, aby nie
uruchamiać dodatkowej płatnej usługi. Jeżeli zostanie zaakceptowany osobno,
architektura będzie następująca:

```text
MLflow service → PostgreSQL
              → dedykowany prefix DigitalOcean Spaces
Smog AI App    → odczyt comparison.json
              → opcjonalny link do MLflow UI
```

## 7. Langfuse — jakość odpowiedzi na pytania

Langfuse pozostaje domyślnie wyłączony. Po jawnej konfiguracji SDK zapisuje:

- treść pytania i wynik interpretacji;
- wersję promptu;
- wykorzystany model LLM;
- czas odpowiedzi i użycie fallbacku;
- dokładny czas prognozy;
- wersje modeli pogodowych i pyłowych;
- ocenę użytkownika powiązaną z `trace_id`.

API udostępnia:

```text
POST /api/v1/feedback
GET  /api/v1/feedback/summary
```

Bez Langfuse oceny są przechowywane lokalnie w pliku JSONL. Włączenie Langfuse
jest decyzją prywatności i kosztu, ponieważ treści promptów i odpowiedzi są
wysyłane do zewnętrznej usługi.

## 8. Porównanie modeli w aplikacji

Aplikacja udostępnia:

```text
GET /api/v1/models/compare
```

Dashboard pokazuje dwie warstwy porównania:

- aktywne i historyczne wersje zapisane w lokalnej bazie modeli;
- wszystkich kandydatów zarejestrowanych jako runy MLflow, wraz z MAE, RMSE,
  biasem, Brier score, profilem i `dataset_id`.

Gdy lokalny serwer MLflow jest zatrzymany, aplikacja nadal działa z ostatnim
wersjonowanym `model-comparison.json`. Gdy skonfigurowano
`SMOG_AI_MLFLOW_UI_URL`, pojawia się także link do pełnego UI MLflow.

## 9. Publikacja do DigitalOcean

Publikacja wymaga osobnej, jawnej komendy oraz frazy bezpieczeństwa. Do Spaces
mogą trafić wyłącznie:

- zatwierdzony plik modelu;
- karta modelu;
- metryki;
- wskaźnik aktywnej wersji;
- artefakt porównania modeli;
- później zatwierdzone prognozy, mapy i dokumentacja.

Nigdy nie są publikowane przez ten mechanizm:

- surowe pomiary;
- SQLite;
- snapshot treningowy;
- ramki treningowe;
- cache źródłowy.

Skrypt publikacyjny zatrzymuje się bez przełącznika
`-IApproveDigitalOceanUpload`.

## 10. DigitalOcean App Platform

Domyślna architektura serwująca pozostaje lekka:

```text
Windows/local pipeline
  → zaakceptowane modele
  → prognozy i mapy
  → jawna publikacja do Spaces

DigitalOcean App Platform
  FastAPI: Bridge read → dokładny punkt IDW → PCHIP
  Streamlit: prywatne połączenie do FastAPI
```

W App Platform nie działa trening, `model.predict`, backfill ani migracja danych.
Dozwolona jest lekka deterministyczna interpolacja na żądanie z już
opublikowanych prognoz stacyjnych. MLflow i Langfuse są opcjonalne oraz
niezależne od podstawowego wdrożenia.

## 11. Kolejność wdrożenia

1. zainstalować HF20;
2. zaktualizować konfigurację czasu;
3. przeliczyć cztery modele na tym samym snapshotcie dla `h1–h60`;
4. wygenerować dokładnie 48 przyszłych godzin;
5. przejść audyt kontraktu czasu;
6. sprawdzić status eksperymentalny/zaakceptowany opadu;
7. opcjonalnie uruchomić lokalny MLflow;
8. przetestować lokalne API, dashboard i feedback;
9. wykonać jawny plan publikacji `-DryRun`;
10. dopiero po zgodzie opublikować zatwierdzone modele do Spaces;
11. wdrożyć staging App Platform;
12. po akceptacji promować produkcję.
