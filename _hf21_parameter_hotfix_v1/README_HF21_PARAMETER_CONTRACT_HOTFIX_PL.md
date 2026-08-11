# HF21 — hotfix kontraktu parametrów PM i pogody

Hotfix usuwa duplikaty `TEMPERATURE_C/temperature_c` oraz
`PRECIPITATION_MM/precipitation_mm` i ujednolica wybieranie parametrów przez
dashboard. Nie zmienia bazy danych, modeli, snapshotów ani obiektów Storage
Bridge.

## Instalacja

Zatrzymaj API i dashboard (`Ctrl+C` w dwóch oknach PowerShell), a następnie:

```powershell
$ProjectRoot = 'C:\..Work\..GotoIT\Works\..Projects\Weather\..Work\GIOS_IMGW_Forecast_Suite_1.7.0_HF21_ExactPoint_StorageBridge_PCHIP'
Set-Location $ProjectRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

& $PythonExe .\scripts\apply_hf21_parameter_contract_hotfix.py --project-root $ProjectRoot
& $PythonExe -m py_compile .\server\application\query.py .\server\dashboard\app.py
```

Uruchom ponownie API i dashboard w ich dotychczasowych oknach. W dashboardzie
wykonaj zapytanie ponownie; stary wynik przechowywany w sesji Streamlit nie jest
automatycznie zastępowany przez zapytanie wykonane z osobnego PowerShella.

## Oczekiwany wynik API

Odpowiedź powinna zawierać dokładnie pięć parametrów:

```text
PM10
PM2.5
temperature_c
precipitation_probability
precipitation_mm
```

Wartości PM dla testu Witków powinny nadal pochodzić z `quality_weighted_idw`,
a wartość dla minuty `11:27` z interpolacji `pchip`.
