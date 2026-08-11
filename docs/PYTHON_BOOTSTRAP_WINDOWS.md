# Automatyczny bootstrap Pythona na Windows — wersja 1.7.0

## Cel

Użytkownik nie musi ręcznie instalować Pythona 3.12, jeżeli na komputerze działa już Python 3.13 x64. Projekt obsługuje zakres:

```text
Python >= 3.12 i < 3.14
```

Mechanizm obowiązujący w 1.7.0 usuwa błąd wykrywania, który w 1.5.1 mógł nie rozpoznać aktywnego Pythona z Anacondy/Condy i niepotrzebnie uruchomić `winget`.

## Co zostało zmienione

Detektor:

1. używa aktywnego `$env:CONDA_PREFIX\python.exe` w pierwszej kolejności;
2. sprawdza bieżące `python.exe`/`python` z `PATH`;
3. odczytuje wszystkie interpretery z `py -0p`;
4. sprawdza `where.exe python`;
5. sprawdza typowe katalogi python.org;
6. sprawdza wpisy rejestru zgodne z PEP 514;
7. dopiero na końcu rozważa `winget`.

Każdy kandydat jest uruchamiany przez tymczasowy plik diagnostyczny `.py`. Nie jest już używany wielowierszowy argument `python -c`, który mógł być różnie cytowany przez Windows PowerShell 5.1, PowerShell 7, `py.exe` i Condę.

## Najbezpieczniejsze uruchomienie przy aktywnym Conda

W terminalu, w którym na początku wiersza widać `(base)` albo nazwę środowiska Conda:

```powershell
$PythonPath = (& python -c "import sys; print(sys.executable)").Trim()

& $PythonPath -c "import sys, struct; print(sys.version); print(sys.executable); print(struct.calcsize('P') * 8)"
```

Wynik powinien wskazywać Python 3.12.x albo 3.13.x oraz `64`.

Następnie:

```powershell
.\scripts\Setup-All.ps1 `
  -PythonExecutable $PythonPath `
  -NoAutomaticPythonInstall `
  -SpaceName 'NAZWA-SPACE' `
  -SpacesRegion 'fra1' `
  -SpacesPrefix 'smog-ai/krakow/production' `
  -LlmProvider 'rule_based' `
  -SkipLangfuse `
  -SkipFirstRun
```

`-NoAutomaticPythonInstall` gwarantuje, że instalator nie wywoła `winget`. Wskazany Python służy tylko do utworzenia lokalnego `.venv`; zależności projektu nie są instalowane do środowiska Conda.

## Wariant automatyczny

```powershell
.\scripts\Setup-All.ps1 `
  -PreferredPythonVersion '3.13' `
  -SpaceName 'NAZWA-SPACE' `
  -SpacesRegion 'fra1' `
  -SpacesPrefix 'smog-ai/krakow/production' `
  -LlmProvider 'rule_based' `
  -SkipLangfuse `
  -SkipFirstRun
```

Detektor powinien sam wybrać aktywny Python 3.13.

## Diagnostyka wszystkich kandydatów

```powershell
.\scripts\Diagnose-Python.ps1
```

Wersja JSON:

```powershell
.\scripts\Diagnose-Python.ps1 -AsJson
```

Skrypt pokazuje:

- źródło kandydata;
- ścieżkę wykonywalną;
- rzeczywistą wersję;
- architekturę;
- ścieżkę `sys.executable`;
- powód odrzucenia.

Szybka kontrola wybranego interpretera:

```powershell
.\scripts\Prepare-Python.ps1 `
  -PythonExecutable $PythonPath `
  -NoAutomaticPythonInstall `
  -AsJson
```

## Znaczenie kodu winget `-1978335189`

Kod dziesiętny:

```text
-1978335189
```

odpowiada:

```text
0x8A15002B
```

i oznacza brak mającej zastosowanie aktualizacji. W 1.7.0 nie jest traktowany jako dowód, że instalacja Pythona się nie powiodła. Instalator ponawia wykrywanie istniejących interpreterów.

## Istniejące `.venv`

- poprawne `.venv` z Pythonem 3.12 lub 3.13 jest ponownie używane;
- uszkodzone, 32-bitowe albo niewspierane `.venv` jest przenoszone do kopii;
- `-RecreateVenv` wymusza nowe środowisko i zachowuje stare jako `.venv.backup-...`.

## Awaryjne ręczne utworzenie `.venv`

```powershell
$PythonPath = (& python -c "import sys; print(sys.executable)").Trim()

& $PythonPath -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

Potem ponownie uruchom `Setup-All.ps1` z `-PythonExecutable $PythonPath -NoAutomaticPythonInstall`.
