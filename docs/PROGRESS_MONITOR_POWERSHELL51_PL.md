# HF15.1 — monitor importu zgodny z Windows PowerShell 5.1

## Objaw

```text
Watch-GiosHistoryProgress.ps1 : Niezgodne typy argumentów.
```

## Przyczyna

Windows PowerShell 5.1 ma błąd bindera przy konwersji generycznej listy .NET
przez konstrukcję:

```powershell
@($genericList)
```

Monitor używał tego wzorca dla:

- `List[System.IO.FileInfo]`;
- `List[object]`.

HF15.1 używa jawnego:

```powershell
$genericList.ToArray()
```

i dodaje dokładną lokalizację/stos przy kolejnym błędzie.

## Bezpieczeństwo

Hotfix modyfikuje wyłącznie:

```text
scripts\Watch-GiosHistoryProgress.ps1
```

Nie dotyka importera, SQLite, cache, modeli ani DigitalOcean Spaces. Można go
zastosować, gdy właściwy import danych nadal działa.

## Instalacja

```powershell
$ProjectRoot = "C:\...\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable"
$HotfixRoot = "C:\Temp\GIOS_IMGW_1.7.0_HF15_1_ProgressMonitor_PowerShell51_Hotfix"

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

& "$HotfixRoot\Apply-ProgressMonitor-PowerShell51-HF15_1.ps1" `
    -ProjectRoot $ProjectRoot
```

Po instalacji uruchom ponownie monitor. Nie restartuj importu, jeżeli proces
`backfill-gios-history` nadal działa.
