# HF20.1 — naprawa audytu CLI i konfiguracji lokalnego MLflow

## Naprawione usterki

1. `audit-hourly-serving-contract` wywoływał funkcję z modułu
   `smog_ai.hourly.audit`, ale `smog_ai.cli` nie importował tej funkcji.
   Efektem był `NameError` dopiero podczas wykonania komendy.

2. `Enable-LocalMLflow.ps1` przekazywał wieloliniowy program Python przez
   `python -c`. Windows PowerShell 5.1 zniekształcał cudzysłowy w natywnym
   wierszu poleceń. Program docierał do Pythona jako m.in.
   `path.name + .before-local-mlflow`.

HF20.1 używa osobnego pliku `scripts/enable_local_mlflow.py`, dzięki czemu
kod nie przechodzi przez problematyczne cytowanie `-c`.

## Bezpieczeństwo

Poprawka nie zmienia konfiguracji runtime podczas instalacji, nie uruchamia
treningu, nie generuje prognoz i nie wykonuje uploadu. Włączenie MLflow jest
osobnym krokiem i domyślnie aktualizuje tylko `config.local-training.yaml`.
