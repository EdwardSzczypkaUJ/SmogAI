# Release notes 1.5.3 — GIOŚ JSON-LD i bezpieczny first-run

Wydanie naprawia błąd wykryty podczas pierwszego rzeczywistego uruchomienia na Windows.

## Naprawy

- GIOŚ v1 jest wywoływany z `Accept: application/ld+json`, dlatego endpointy nie zwracają już HTTP 406 z powodu negocjacji treści.
- Parser obsługuje aktualne pola `Wskaźnik - kod`, `Wskaźnik - wzór` oraz aktualne koperty JSON-LD.
- Pomiary bieżące są pobierane stronicowo (maksymalnie 500 rekordów na stronę).
- Stałe błędy 4xx nie są bezcelowo ponawiane; retry pozostaje dla błędów przejściowych i sieciowych.
- Niekompletny pakiet GIOŚ/IMGW jest zapisywany jako audyt `last-attempt.json`, ale nie zastępuje poprawnego `datasets/bronze/latest.json`.
- `first-run` zatrzymuje trening, interpolację i publikację, gdy obowiązkowe pobranie danych nie zakończyło się poprawnie.
- Brak pierwszego zbioru curated jest stanem `skipped`, a nie kaskadą sześciu błędów `NoSuchKey`.
- Pusty snapshot nie jest tworzony ani publikowany.
- Dodano polecenie `python -m smog_ai probe-gios` do lekkiego testu aktualnego kontraktu API.

Konfiguracja Spaces, lokalna SQLite, sekrety i istniejące dane z wersji 1.5.2 pozostają zgodne.
