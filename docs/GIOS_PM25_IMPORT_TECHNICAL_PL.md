# Import historycznych danych PM2.5 GIOŚ — diagnoza i przetwarzanie

Autor: Edward Szczypka, edward@szczypka.guru

## Diagnoza danych 2022–2024

Audyt lokalnych archiwów wykazał:

| Rok | Arkusz | Serie | Poprawne wartości | Rekordy prepared w SQLite |
|---:|---|---:|---:|---:|
| 2022 | `2022_PM25_1g.xlsx` | 89 | 755 956 | 0 |
| 2023 | `2023_PM25_1g.xlsx` | 92 | 779 380 | 0 |
| 2024 | `2024_PM25_1g.xlsx` | 96 | 817 648 | 0 |

Pliki ZIP przechodzą CRC, a wszystkie trzy skoroszyty są czytelne. Awaria nie
leży po stronie oficjalnych danych GIOŚ. Występowały dwa błędy importera:

1. selektor oczekiwał nazwy `PM2.5`, podczas gdy oficjalne ZIP-y używają
   `PM25`;
2. heurystyka nagłówka wybierała wiersz `Nr` z wartościami `1, 2, ...`
   zamiast jawnego wiersza `Kod stacji`.

Dodatkowo tryb prepared był niepotrzebnie uzależniony od bieżącej dostępności
metadanych API, a wrapper PowerShell zapisywał pustą transkrypcję zamiast
rzeczywistego stdout/stderr procesu Pythona.

## Poprawiony przepływ

```text
Oficjalny roczny ZIP GIOŚ
        ↓
Bridge cache: local / object_store / hybrid
        ↓
normalizacja nazwy PM25 ↔ PM2.5 ↔ PM2,5
        ↓
wybór arkusza *_PM25_1g.xlsx
        ↓
wykrycie jawnego wiersza „Kod stacji”
        ↓
parsowanie czasu jako stały CET i zapis UTC
        ↓
mapowanie kodu stacji do lokalnego katalogu
        ↓
kontrolowany fallback stacji historycznej
        ↓
insert partiami, ON CONFLICT DO NOTHING
        ↓
kontrola: valid_values = inserted + duplicates
        ↓
state.json dopiero po pełnym sukcesie
```

## Niezmienniki jakości

Importer nie może oznaczyć arkusza jako ukończony, jeżeli:

- nie odnalazł arkusza PM2.5 1g;
- wybrał sekwencyjny wiersz numerów zamiast kodów stacji;
- parser znalazł poprawne wartości, lecz zapisano zero wierszy;
- liczba rozwiązanych insertów i duplikatów różni się od liczby poprawnych
  wartości;
- wystąpił błąd transakcji.

## Obserwowalność i progress

Każda seria stacji emituje zdarzenie JSON zawierające:

```text
stage
year
parameter
series_completed
series_total
percent
station_code
valid_values_series
invalid_values_series
inserted_total
duplicates_total
```

Skrypty PowerShell używają `Tee-Object`, a logger kieruje komunikaty do stdout,
dzięki czemu log nie jest pusty w Windows PowerShell 5.1. Monitor
`Watch-GiosHistoryProgress.ps1` pokazuje procent, ETA, PID, CPU, RAM oraz
ostatnie linie logu.

## Bridge danych

Import historyczny może używać:

- `local` — ZIP jest przechowywany lokalnie;
- `object_store` — cache kanoniczny znajduje się w DigitalOcean Spaces lub
  innym S3;
- `hybrid` — lokalny cache, następnie ObjectStore, następnie źródło GIOŚ.

Tryb cache nie zmienia parsera ani reguł zapisu do SQLite.
