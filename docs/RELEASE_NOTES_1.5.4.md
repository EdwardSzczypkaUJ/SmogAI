# Release notes 1.5.4 — GIOŚ nationwide collection and Pandera-safe first run

Wydanie 1.5.4 jest kumulatywną poprawką dla instalacji 1.5.2 z hotfixem JSON-LD.
Nie wymaga usuwania bazy SQLite, katalogu `%ProgramData%\SmogAI`, środowiska `.venv`
ani obiektów zapisanych wcześniej w DigitalOcean Spaces.

## Poprawione problemy wykryte na rzeczywistych danych

- odpowiedzi HTTP 400/404 dla historycznych lub nieaktywnych stanowisk GIOŚ są
  klasyfikowane jako brak bieżącej serii (`warning/skipped`), a nie awaria całego
  pobierania;
- wyjątek HTTP zachowuje kod statusu, URL i bezpieczny fragment odpowiedzi;
- `first-run` wykonuje najpierw wyłącznie kolekcję, walidację, dopasowanie i upload
  do Spaces; trening nie startuje, dopóki pakiet GIOŚ/IMGW nie jest kompletny;
- ramki treningowe usuwają wiersze powstałe z luk godzinowych, które nie mają
  bieżącej wartości, celu, współrzędnych lub poprawnego czasu celu;
- predykcja nie tworzy prognoz ze starych pomiarów ani prognoz, których termin już
  minął w chwili zapisu;
- snapshot nie publikuje historycznych, wadliwych prognoz utworzonych przez starsze
  wydania po ich czasie docelowym; rekordy pozostają w SQLite do audytu;
- ostrzeżenie deprecacyjne `pd.Timedelta("3h")` zostało usunięte;
- brak zbioru dla pojedynczej kombinacji PM/horyzont jest obsługiwany jako
  niewystarczająca historia, z bezpiecznym modelem persistence.

## Zgodność

Hotfix jest przeznaczony przede wszystkim dla:

- pełnego wydania 1.5.2;
- wydania 1.5.2 po zastosowaniu `GIOS_JSONLD_HF1`;
- wydania 1.5.3.

Konfiguracja Spaces, tokeny, baza oraz dane lokalne pozostają bez zmian.
