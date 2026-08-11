# Release notes 1.5.3 — Python 3.13 / Conda detection hotfix

## Zakres wydania

Wydanie 1.5.3 zachowuje wszystkie funkcje 1.5.0/1.5.1:

- lokalne pobieranie GIOŚ i IMGW;
- SQLite, walidację i lokalny ML;
- round trip danych przez DigitalOcean Spaces;
- lokalnie liczone prognozy oraz interpolowaną mapę całej Polski;
- publiczne FastAPI/Streamlit na App Platform, które tylko odczytują gotowe wyniki;
- Pandera, opcjonalny Langfuse, textbox i GitHub Actions.

Zmiana dotyczy instalatora Windows.

## Naprawiony problem

W 1.5.1 aktywny Python 3.13 z Anacondy/Condy mógł nie zostać rozpoznany. Instalator uruchamiał wtedy `winget install Python.Python.3.12`, mimo że poprawny interpreter już działał.

Przyczyną była sonda przekazywana jako wielowierszowy argument `python -c`. W zależności od sposobu przekazywania argumentów przez Windows PowerShell 5.1, PowerShell 7, `py.exe` i Condę mogła ona zakończyć się błędem bez czytelnej diagnostyki.

## Zmiany techniczne

- sonda Pythona jest zapisywana do tymczasowego pliku `.py`;
- aktywny `$env:CONDA_PREFIX\python.exe` ma pierwszeństwo;
- sprawdzane są `python.exe` z `PATH`, `py -0p`, `where.exe`, typowe katalogi oraz rejestr PEP 514;
- dodano pełny raport kandydatów przez `scripts/Diagnose-Python.ps1`;
- kod winget `-1978335189` / `0x8A15002B` powoduje ponowne skanowanie zamiast natychmiastowego błędu;
- użytkownik może całkowicie wyłączyć winget parametrem `-NoAutomaticPythonInstall`;
- dodano test regresyjny sprawdzający brak starej sondy `-c $Probe`.

## Zalecane polecenie przy aktywnym `(base)`

```powershell
$PythonPath = (& python -c "import sys; print(sys.executable)").Trim()

.\scripts\Setup-All.ps1 `
  -PythonExecutable $PythonPath `
  -NoAutomaticPythonInstall `
  -SpaceName "NAZWA-SPACE" `
  -SpacesRegion "fra1" `
  -SpacesPrefix "smog-ai/krakow/production" `
  -LlmProvider "rule_based" `
  -SkipLangfuse `
  -SkipFirstRun
```

## Diagnostyka

```powershell
.\scripts\Diagnose-Python.ps1
.\scripts\Diagnose-Python.ps1 -AsJson
```

## Weryfikacja

Wydanie jest sprawdzane przez:

- pytest;
- kompilację modułów;
- test architektury lokalnego ML i lokalnej interpolacji;
- walidację plików DigitalOcean;
- kontrolę UTF-8 BOM + CRLF wszystkich skryptów PowerShell;
- test kontraktu detektora Pythona;
- manifest SHA-256 oraz ponowne rozpakowanie ZIP-a.
