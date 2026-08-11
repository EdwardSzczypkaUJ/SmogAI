# GIOŚ/IMGW Forecast Suite 1.7.0 — raport budowy i weryfikacji

Data UTC: `2026-08-03T15:26:22.067931+00:00`  
Status: **PASSED**

## Zakres wydania

Wydanie 1.7.0 przebudowuje prognozowanie z dyskretnych horyzontów 6/12/24 h
na otwartą platformę modeli godzinowych, warunkowanych dokładnym horyzontem
`h=1..48`. Pełnoprawnymi celami są `PM10`, `PM2.5`, `temperature_c` oraz
`precipitation_mm`.

Opad jest obsługiwany przez model typu hurdle, a modele PM mogą wykorzystywać
chronologicznie wygenerowane prognozy temperatury i opadu bez wycieku
informacji z przyszłości. Rejestr `ModelProvider` pozwala podłączać alternatywne
regresory i biblioteki bez przebudowy pipeline'u, SQLite, Spaces, FastAPI lub
Streamlit.

## Architektura potwierdzona bramką

```text
Windows lokalnie:
  GIOŚ/IMGW -> SQLite -> Pandera -> Spaces -> lokalny trening
  -> lokalna predykcja godzinowa -> lokalna interpolacja Polski -> Spaces

DigitalOcean App Platform:
  read-only FastAPI + publiczny Streamlit
  bez model.predict() i bez interpolacji przestrzennej
```

## Wyniki pełnej bramki offline

- kontrole: **18/18 passed**;
- pytest: **103 passed**;
- Python bramki: **3.13.5**;
- kompilacja wszystkich modułów Python: **passed**;
- CLI: **passed**;
- FastAPI seeded spatial query: **passed**;
- interpolacja przestrzenna: **passed**;
- App Spec produkcyjny i deweloperski: **passed**;
- zgodność lokalnego przetwarzania / cloud read-only: **passed**;
- izolacja testów od produkcyjnej SQLite: **passed**;
- PowerShell: **23/23 UTF-8 BOM + CRLF**;
- zabezpieczenie wildcard Uvicorna na Windows: **passed**;
- bezpieczne zamykanie uchwytów backupu SQLite: **passed**;
- dokumentacja techniczna i matematyczna: **passed**;
- wheel build oraz import z izolowanego środowiska: **passed**;
- dołączony wheel: `gios_imgw_forecast_suite-1.7.0-py3-none-any.whl`;
- SHA-256 wheel: `a1d89160a5bb12df1a65eaff0bdd7c1dd2a91a13c6cd268c0901fe50c42aa727`.

## Dokumentacja zweryfikowana wizualnie

Wyrenderowano i sprawdzono oba dokumenty PDF:

```text
docs/pdf/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.pdf       10 stron, A4
docs/pdf/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.pdf 10 stron, A4
```

Nie stwierdzono uciętego tekstu, nakładania elementów ani problemów z polskimi
znakami. Źródła LaTeX pozostają w paczce i są również udostępniane przez API.

## Testy wymagające środowiska klienta

Bramka jest celowo offline. Nie wykonano w środowisku budowy logowania na konto
klienta DigitalOcean, rzeczywistego zapisu do jego Space, deploymentu App
Platform ani natywnego uruchomienia Harmonogramu zadań na jego komputerze.
Ocena naukowa jakości modelu wymaga również wielosezonowej historii. Projekt
zawiera instrukcje i polecenia diagnostyczne dla tych testów pilotażowych.
