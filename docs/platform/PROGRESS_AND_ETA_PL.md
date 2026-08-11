# Postęp i ETA długich przebiegów Smog AI

Wersja 1.7.0 HF6 zapisuje trwały stan `first-run` do:

```text
C:\ProgramData\SmogAI\logs\progress\first-run-current.json
C:\ProgramData\SmogAI\logs\progress\first-run-<run_id>.json
C:\ProgramData\SmogAI\logs\progress\first-run-<run_id>.jsonl
```

Stan zawiera:

- procent całego `first-run`;
- procent aktualnego etapu;
- aktualny cel, provider, fold albo powierzchnię przestrzenną;
- liczbę wykonanych i planowanych jednostek pracy;
- czas działania;
- ETA i zakres niepewności ETA;
- szacowany czas zakończenia;
- czas trwania bieżącego zadania;
- status `running`, `success`, `partial_success`, `failed` albo `cancelled`.

## Monitor PowerShell

```powershell
.\scripts\Watch-FirstRunProgress.ps1 `
    -ProjectRoot (Get-Location).Path `
    -RuntimeRoot "$env:ProgramData\SmogAI"
```

## CLI

Pojedynczy odczyt:

```powershell
.\.venv\Scripts\python.exe -m smog_ai progress `
    --config C:\ProgramData\SmogAI\config.yaml `
    --env-file C:\ProgramData\SmogAI\smog-ai.env
```

Tryb ciągły:

```powershell
.\.venv\Scripts\python.exe -m smog_ai progress `
    --watch `
    --refresh-seconds 5 `
    --config C:\ProgramData\SmogAI\config.yaml `
    --env-file C:\ProgramData\SmogAI\smog-ai.env
```

## Jak liczony jest procent

Cały przebieg jest podzielony na ważone etapy:

```text
collection       4%
training_data    7%
training        64%
prediction       5%
spatial         17%
documentation    1%
snapshot         1%
publication      1%
```

Wewnątrz treningu providerzy mają różne wagi. MLP, gradient boosting i model
hurdle mają większą wagę niż persistence, średnia historyczna albo Ridge.
Przestrzenna część raportuje osobno każdą kombinację parametru i horyzontu.

## Jak liczony jest ETA

Pierwszy przebieg korzysta z konserwatywnych czasów domyślnych oraz prędkości
już zakończonych jednostek pracy. Po każdym zadaniu i etapie zapisywany jest
lokalny czas wykonania. Kolejne przebiegi używają mediany wcześniejszych czasów
na tym samym komputerze.

ETA ma poziom pewności:

- `low` — pierwszy przebieg albo mało ukończonych jednostek;
- `medium` — część kosztownych zadań została zakończona;
- `high` — większość przebiegu lub dostępna historia lokalna.

Scikit-learn nie udostępnia wspólnego callbacku procentowego dla wszystkich
providerów. Podczas pojedynczego blokującego `fit()` heartbeat nadal aktualizuje
czas bieżącego zadania, szacowany czas zadania, wykorzystanie dotychczasowej
historii i ETA całego przebiegu. Nie jest to fałszywy procent iteracji modelu.
