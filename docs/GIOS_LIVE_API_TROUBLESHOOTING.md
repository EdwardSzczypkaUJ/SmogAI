# GIOŚ v1 — test i diagnostyka na żywo

## Szybki test

```powershell
$ProjectRoot = (Get-Location).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $env:ProgramData "SmogAI\config.yaml"
$EnvFile = Join-Path $env:ProgramData "SmogAI\smog-ai.env"

& $Python -m smog_ai probe-gios --config $Config --env-file $EnvFile
```

Prawidłowy wynik zawiera `"status": "ok"`, `"api": "GIOS v1 JSON-LD"` oraz próbny identyfikator stacji.

## Interpretacja

- `406` — uruchamiany jest stary kod bez hotfixu albo pośrednik modyfikuje nagłówek `Accept`;
- `429` — limit żądań, klient zastosuje retry/backoff;
- `500/502/503/504` — awaria przejściowa po stronie usługi, klient ponowi żądanie;
- błąd DNS/timeout — problem sieciowy lub chwilowa niedostępność;
- `200`, ale brak stacji — niezgodna odpowiedź/schema; zapisz log do analizy.

`probe-gios` niczego nie zapisuje w SQLite ani Spaces.
