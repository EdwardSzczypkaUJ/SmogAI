# Instalacja lokalna Windows

## Wymagania

- Windows 10/11 x64;
- Python 3.12 **albo 3.13** x64;
- PowerShell 7 preferowany, Windows PowerShell 5.1 obsługiwany;
- dostęp do Internetu i prywatnego DigitalOcean Space.

Python nie musi być przygotowany ręcznie. `Setup-All.ps1`:

1. akceptuje istniejący Python 3.12 lub 3.13 x64;
2. preferuje interpreter wskazany przez `-PythonExecutable`;
3. wykrywa `py.exe`, typowe instalacje CPython oraz aktywne środowisko Conda;
4. tworzy odizolowane `.venv` w katalogu projektu;
5. gdy nie znajdzie wspieranego interpretera, próbuje zainstalować Python 3.12 x64 przez `winget`;
6. nie usuwa ani nie zastępuje Pythona 3.13 zainstalowanego na komputerze.

Zakres wspierany przez pakiet jest zapisany w `pyproject.toml`:

```text
>=3.12,<3.14
```

## Automat

Z dowolnego katalogu projektu:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\Setup-All.ps1 -SkipFirstRun
```

Przy istniejącym Pythonie 3.13 można jawnie wskazać tę wersję:

```powershell
.\scripts\Setup-All.ps1 `
  -PreferredPythonVersion '3.13' `
  -SkipFirstRun
```

Przy aktywnym środowisku Conda można wskazać dokładny interpreter bez zgadywania ścieżki:

```powershell
.\scripts\Setup-All.ps1 `
  -PythonExecutable (Join-Path $env:CONDA_PREFIX 'python.exe') `
  -SkipFirstRun
```

Nie aktywuj ręcznie `.venv`; skrypty zawsze wywołują `.venv\Scripts\python.exe` bezpośrednio.

## Kontrola Pythona bez instalowania projektu

```powershell
.\scripts\Prepare-Python.ps1 -AsJson
```

Polecenie zwraca wersję, architekturę i ścieżkę interpretera. Bez `-NoAutomaticPythonInstall` może doinstalować Python 3.12 przez `winget`, gdy nie ma żadnej wspieranej wersji.

## Wymuszenie braku instalacji systemowej

```powershell
.\scripts\Setup-All.ps1 `
  -NoAutomaticPythonInstall `
  -SkipFirstRun
```

W tym wariancie brak Pythona 3.12/3.13 kończy instalację czytelnym błędem, bez uruchamiania `winget`.

## Ponowne utworzenie `.venv`

Jeżeli `.venv` jest uszkodzone lub chcesz świadomie przejść na inny interpreter:

```powershell
.\scripts\Setup-All.ps1 `
  -PreferredPythonVersion '3.13' `
  -RecreateVenv `
  -SkipFirstRun
```

Stare środowisko nie jest kasowane. Instalator przenosi je do katalogu podobnego do:

```text
.venv.backup-20260802-143000
```

## Instalacja ręczna

Dla Python 3.13:

```powershell
$ProjectRoot = (Get-Location).Path
py -3.13 -m venv (Join-Path $ProjectRoot '.venv')
& (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -m pip install -r requirements.txt
```

Dla Python 3.12 wystarczy zamienić `-3.13` na `-3.12`.

Runtime może znajdować się w dowolnym miejscu przez `-RuntimeRoot`; domyślnie `%ProgramData%\SmogAI`.

Skrypty `.ps1` są zapisane UTF-8 BOM + CRLF, aby zachować polskie znaki w PowerShell 5.1.
