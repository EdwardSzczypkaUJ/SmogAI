# Checklista przekazania klientowi — 1.7.0

## Pakiet i środowisko

```text
[ ] ZIP 1.7.0 zweryfikowany plikiem SHA-256
[ ] projekt rozpakowany w dowolnym wybranym katalogu
[ ] Python 3.12 lub 3.13 x64 wykryty
[ ] .venv utworzone przez instalator
[ ] pip check poprawny
[ ] runtime i sekrety poza repozytorium
[ ] dokumentacja LaTeX znajduje się w docs/latex
```

## DigitalOcean Spaces

```text
[ ] Standard Storage, region fra1
[ ] listing Restricted, CDN wyłączony
[ ] lokalny klucz Read/Write/Delete
[ ] klucz API App Platform tylko Read
[ ] SPACES_BUCKET/REGION/ENDPOINT/PREFIX zgodne lokalnie i w GitHub
[ ] storage-health status ok
```

## Dane i modele

```text
[ ] GIOŚ JSON-LD pobrane
[ ] bieżące IMGW pobrane
[ ] archiwum terminowe IMGW uzupełnione
[ ] Bronze complete=true
[ ] Pandera: brak błędu krytycznego
[ ] aktywne modele godzinowe dla PM10, PM2.5, temperatury i opadu
[ ] provider/model card widoczne w /api/v1/models
[ ] horyzonty 1..48 obecne
[ ] prognozy mają target_time = origin_time + h
[ ] opad ma jawny okres akumulacji
```

## Mapy

```text
[ ] mapy PM10 i PM2.5 dla h1..h48
[ ] mapa temperatury
[ ] mapa prawdopodobieństwa opadu
[ ] mapa oczekiwanej sumy opadu
[ ] exact_target_time_available=true
[ ] nazwy miast widoczne nad warstwą
[ ] tryb 3D jest opcjonalny i nie zasłania etykiet
[ ] punkt miasta, środek komórki i odległości są pokazane
```

## Lokalny pilot

```text
[ ] FastAPI /health i /ready ok
[ ] dashboard działa
[ ] pytanie z dokładną godziną zwraca direct_hourly_surface
[ ] pytanie bez godziny pokazuje profil dnia
[ ] zakładka Model i jakość działa
[ ] zakładka Jak to działa udostępnia Markdown i LaTeX
[ ] Test-LocalServer.ps1 -AsJson bez błędów
```

## GitHub i App Platform

```text
[ ] prywatne repozytorium
[ ] GitHub autoryzowany w DigitalOcean
[ ] Secrets/Variables ustawione
[ ] pull request uruchamia testy bez deployu
[ ] merge do main uruchamia automatyczny deploy
[ ] /api/v1/health, /ready, /models i /docs/manifest działają zdalnie
[ ] publiczny dashboard działa
[ ] App Platform nie wykonuje ML ani interpolacji przestrzennej
```

## Operacje

```text
[ ] cztery zadania w \SmogAI\
[ ] blokada równoległego uruchomienia
[ ] backup SQLite i restore test
[ ] outbox/retry test
[ ] rotacja kluczy opisana
[ ] odpowiedzialna osoba zna Operations Runbook
```
