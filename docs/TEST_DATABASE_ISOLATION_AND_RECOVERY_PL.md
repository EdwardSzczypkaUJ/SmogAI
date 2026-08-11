# Izolacja testów i odzyskiwanie bazy SQLite — obowiązuje w 1.7.0

## Objaw

Jeżeli w raporcie pytest występuje wpis podobny do:

```text
engine = Engine(sqlite:///C:/ProgramData/SmogAI/data/smog.db)
app_config = AppConfig(environment='test', ...)
```

testy korzystały z bazy produkcyjnej zamiast z katalogu `pytest-*`. Nie jest to zestaw
21 niezależnych usterek biznesowych. Większość asercji zawodzi dlatego, że test oczekuje
jednej syntetycznej stacji, a widzi setki rekordów produkcyjnych.

## Dlaczego zalecana jest odbudowa, a nie ręczne DELETE

Testy mogły nie tylko dodać rozpoznawalne rekordy (`A1`, `S1`, `legacy-test-v1`, `p1`),
lecz również zmienić znaczniki czasu istniejących pomiarów. Usunięcie kilku markerów nie
przywróciłoby pewnego stanu. Dlatego narzędzie:

1. zatrzymuje się, jeżeli działa zadanie Harmonogramu;
2. wykonuje transakcyjnie spójny SQLite Online Backup;
3. tworzy i weryfikuje świeżą bazę w pliku tymczasowym;
4. przenosi oryginalne DB/WAL/SHM do kwarantanny;
5. atomowo instaluje świeżą bazę;
6. zapisuje SHA-256 i raport JSON.

## Audyt

Zatrzymaj lokalne FastAPI, Streamlit i zadania `\SmogAI\`, po czym uruchom:

```powershell
$ProjectRoot = (Get-Location).Path
$RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'

.\scripts\Repair-TestContaminatedDatabase.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot
```

Kody:

```text
0 — nie wykryto znanych markerów
4 — wykryto dane pytest; odbudowa zalecana
1/2 — błąd lub odmowa operacji
```

W przypadku logu jednoznacznie potwierdzającego użycie bazy produkcyjnej odbudowę należy
wykonać również wtedy, gdy audyt nie znajdzie markerów — użyj `-Force`.

## Odbudowa

```powershell
.\scripts\Repair-TestContaminatedDatabase.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -Rebuild `
  -Force `
  -Confirm:$false
```

Kopie powstaną w:

```text
%ProgramData%\SmogAI\backups\test-leak-recovery\YYYYMMDDTHHMMSSZ\
```

Znajdują się tam:

- `smog-before-rebuild.sqlite` — spójny Online Backup;
- `original-files\smog.db`, opcjonalnie `-wal` i `-shm`;
- `recovery-report.json` z checksumem i licznikami.

## Ponowne zasilenie

```powershell
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

& $Python -m smog_ai collect-gios --config $Config --env-file $EnvFile
& $Python -m smog_ai collect-imgw --config $Config --env-file $EnvFile
& $Python -m smog_ai first-run --config $Config --env-file $EnvFile
```

## Bezpieczne testy w przyszłości

```powershell
.\scripts\Test-PytestIsolated.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot
```

Bezpośrednie `python -m pytest` również jest chronione w 1.7.0 (mechanizm wprowadzono w 1.5.5), lecz skrypt dodatkowo
wykonuje guard i zapewnia czytelny komunikat. Testy nie łączą się z GIOŚ, IMGW ani Spaces.
