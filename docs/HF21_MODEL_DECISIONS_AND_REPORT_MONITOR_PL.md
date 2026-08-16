# HF21 — decyzje modeli i raporty w monitorze

Hotfix poprawia dwa lokalne interfejsy bez zmiany modeli, danych ani Serving v2.

## Dashboard

- licznik publikowanych parametrów korzysta z manifestu Serving v2;
- pole `target` kart modeli nie jest już błędnie odczytywane jako `parameter`;
- 4 artefakty modeli są prawidłowo opisane jako źródło 5 parametrów;
- `precipitation_probability` jest pokazane jako klasyfikacyjne wyjście modelu
  hurdle dla `precipitation_mm`;
- cele zatwierdzone i eksperymentalne są wypisane przed tabelą modeli;
- brak metadanej jakości nie jest prezentowany jako `None`: decyzja jest brana
  z opublikowanego manifestu Serving v2.

## Monitor automatu

- pokazuje zatwierdzone i eksperymentalne modele oraz cele Serving v2;
- pokazuje release ID, liczbę powierzchni i politykę celów eksperymentalnych;
- wykrywa raport także po deterministycznej ścieżce, gdy wskaźnik w `run.json`
  jest niepełny;
- pozwala wybrać przebieg w historii;
- wyświetla raport HTML wewnątrz monitora;
- umożliwia pobranie raportu HTML, JSON i Markdown.

## Test

```powershell
.\scripts\Test-SmogAI-UI-Monitor-Hotfix.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot 'C:\ProgramData\SmogAI'
```
