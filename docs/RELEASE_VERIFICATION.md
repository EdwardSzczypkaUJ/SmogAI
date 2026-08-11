# Weryfikacja wydania 1.7.0

Bramka `scripts/verify_release.py` jest offline: nie używa kluczy klienta i nie
wywołuje GIOŚ, IMGW, DigitalOcean, LLM ani Langfuse. Kontroluje kod, kontrakty,
przykładowe artefakty i architekturę.

## Zakres bramki

- zgodność wersji `1.7.0`;
- wymagane pliki platformy godzinowej i pluginów;
- TOML, YAML i cztery definicje XML Task Scheduler;
- przenośny katalog projektu;
- Python 3.12/3.13 i bezpieczny bootstrap;
- UTF-8 BOM + CRLF dla PowerShell;
- izolacja testowej SQLite od `%ProgramData%\SmogAI`;
- dokumentacja techniczna, matematyczna i pluginowa;
- zakaz `model.predict()` i interpolacji przestrzennej w `server/`;
- model godzinowy `h=1..48` dla PM10, PM2.5, temperatury i opadu;
- rejestr `ModelProvider` i zewnętrzny plugin;
- hurdle model opadu;
- smoke IDW/RBF;
- oba DigitalOcean App Specs;
- kompilacja Pythona, CLI, FastAPI, pytest i wheel.

## Polecenia

```powershell
.\scripts\Test-Release.ps1
```

albo:

```powershell
& .\.venv\Scripts\python.exe scripts\verify_release.py `
  --output release-verification.json
```

## Test integracyjny po bramce

Na docelowym Windows należy osobno wykonać:

```powershell
& $Python -m smog_ai probe-gios --config $Config --env-file $EnvFile
& $Python -m smog_ai storage-health --config $Config --env-file $EnvFile
& $Python -m smog_ai first-run --config $Config --env-file $EnvFile
.\scripts\Test-LocalServer.ps1 -AsJson
```

Końcowy raport konkretnego archiwum jest zapisany w `RELEASE_VERIFICATION.json`
i `BUILD_REPORT.md` w katalogu głównym wydania.
