# Release notes 1.5.5 — test isolation and SQLite recovery

## Dlaczego powstało to wydanie

Skrypt hotfixu 1.5.4 uruchamiał `pytest` w tym samym procesowym środowisku, z którego
wcześniej uruchamiano aplikację. Jeżeli terminal miał `SMOG_AI_DATABASE_URL` wskazujący
`C:\ProgramData\SmogAI\data\smog.db`, właściwość `AppConfig.database_url` traktowała
tę wartość jako nadrzędną także dla `environment=test`. W rezultacie testy mogły odczytać
i zmodyfikować rzeczywistą bazę. Charakterystyczny objaw to liczby setek stacji w testach,
które oczekują pojedynczego rekordu.

## Zmiany ochronne

- testowa konfiguracja zawsze używa `paths.database_path`;
- wszystkie runtime/sekretne zmienne są czyszczone przez fixture;
- skrypt testowy zapisuje `--basetemp` poza projektem i wykonuje guard;
- release gate czyści środowisko każdego podprocesu;
- nowy test ustawia celowo produkcyjny URL i potwierdza, że plik nie jest tworzony;
- hotfix 1.5.5 wykonuje rollback plików, gdy testy nie przejdą.

## Odzyskiwanie danych

Nie próbujemy usuwać tylko syntetycznych wierszy, ponieważ część testów modyfikuje czas
realnych pomiarów. Narzędzie odzyskiwania zachowuje całą starą bazę i tworzy nową.
Po odbudowie należy ponownie uruchomić kolektory i `first-run`. Dane oraz modele już
opublikowane do DigitalOcean Spaces nie są kasowane.

## Weryfikacja

Pełny zestaw testów jest uruchamiany dwukrotnie: w czystym środowisku oraz przy celowo
ustawionych produkcyjnych zmiennych. W obu przypadkach baza strażnicza nie może powstać.
