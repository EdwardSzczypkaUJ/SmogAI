# HF18.4 — stabilny schemat katalogu parametrów

## Problem

Po dodaniu generycznych parametrów część z nich nie ma jeszcze żadnego
pomiaru. API zwracało wtedy:

```json
"measurements": {}
```

Natomiast renderer Windows PowerShell 5.1 działał z `Set-StrictMode` i
odczytywał bezwarunkowo:

```powershell
$Value.measurements.rows
```

Brak pola kończył cały katalog przed wyświetleniem tabeli IMGW, dlatego
użytkownik nie widział temperatury mimo że `temperature_c` nadal pozostawało
aktywnym celem modelowym.

## Kontrakt po HF18.4

Każdy parametr — również jeszcze niepobrany i parametr pochodny — ma pełną
strukturę:

```json
{
  "rows": 0,
  "start": null,
  "end": null,
  "stations": 0,
  "unique_hours": 0
}
```

PowerShell nadal stosuje defensywny odczyt właściwości, aby katalog nie
przestał działać po przyszłym rozszerzeniu schematu.

## Zakres poprawki

- generyczne parametry GIOŚ bez danych;
- `precipitation_probability`, która jest wielkością modelową, a nie surowym
  pomiarem;
- bezpieczna konwersja wartości liczbowych w Windows PowerShell 5.1;
- test rzeczywistego renderera po instalacji;
- brak zmian w SQLite, modelach, Spaces i `config.yaml`.
