# Hotfix 1.5.2 — wykrywanie Pythona 3.13 i Condy

## Objaw w 1.5.1

Instalator wyświetlał:

```text
Brak Pythona 3.12/3.13. Instaluję Python.Python.3.12 przez winget...
```

mimo że w aktywnym terminalu działał Python 3.13. Następnie `winget` kończył się kodem:

```text
-1978335189
```

## Przyczyna

Wersja 1.5.1 używała wielowierszowego kodu przekazywanego przez `python -c`. Na części konfiguracji Windows PowerShell/Conda sonda mogła zostać niepoprawnie przekazana do interpretera, przez co poprawny Python był odrzucany. Następnie instalator próbował niepotrzebnie uruchomić `winget`.

## Naprawa

Wersja 1.5.2:

- wykonuje sondę z tymczasowego pliku `.py`;
- daje pierwszeństwo aktywnemu interpreterowi Conda;
- skanuje `PATH`, `py -0p`, `where.exe`, typowe katalogi i rejestr;
- pokazuje pełną diagnostykę kandydatów;
- nie traktuje kodu `0x8A15002B` jako krytycznego błędu instalacji;
- umożliwia całkowite wyłączenie `winget` przez `-NoAutomaticPythonInstall`.

## Polecenie zalecane dla użytkownika z Pythonem 3.13

```powershell
$PythonPath = (& python -c "import sys; print(sys.executable)").Trim()

.\scripts\Setup-All.ps1 `
  -PythonExecutable $PythonPath `
  -NoAutomaticPythonInstall
```

Pozostałe parametry Spaces można dopisać jak w głównej instrukcji.
