# HF21 — poprawka `float(None)` w formularzu potwierdzenia

Poprawka dotyczy wyłącznie dashboardu. Gdy niezależny resolver zwróci punkt,
ale pole `distance_to_reference_km` ma wartość `null`, aplikacja wyświetli
ostrzeżenie i pozwoli użytkownikowi zatwierdzić współrzędne. Nie wykona już
`float(None)`.

Zatrzymaj dashboard, zastosuj poprawkę i sprawdź kompilację:

```powershell
$ProjectRoot = 'C:\..Work\..GotoIT\Works\..Projects\Weather\..Work\GIOS_IMGW_Forecast_Suite_1.7.0_HF21_ExactPoint_StorageBridge_PCHIP'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
Set-Location $ProjectRoot

& $PythonExe .\scripts\apply_hf21_none_distance_hotfix.py --project-root $ProjectRoot
& $PythonExe -m py_compile .\server\dashboard\app.py
```

Następnie uruchom ponownie dashboard. API, modeli, powierzchni i Storage Bridge
nie trzeba przebudowywać.
