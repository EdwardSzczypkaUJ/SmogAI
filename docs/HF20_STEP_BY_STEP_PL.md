# Smog AI 1.7.0 HF20 — procedura wykonawcza krok po kroku

## 1. Zakres wydania

HF20 realizuje cztery powiązane cele:

1. rozdziela horyzont serwowany użytkownikowi od horyzontu modelu;
2. trenuje modele dla `h1–h60`, aby zawsze serwować dokładnie 48 przyszłych pełnych godzin;
3. dodaje lokalne porównywanie modeli przez MLflow i artefakt `model-comparison.json`;
4. dodaje lokalne oceny odpowiedzi promptowych oraz opcjonalny Langfuse.

Instalacja HF20 nie pobiera danych, nie tworzy snapshotu, nie uruchamia treningu
oraz nie wysyła niczego do DigitalOcean.

## 2. Bezpieczna kolejność

```text
instalacja HF20
→ konfiguracja 48/12/60
→ weryfikacja istniejącego snapshotu
→ ponowny trening na Snapshot=latest
→ audyt 48 przyszłych godzin
→ lokalne API i dashboard
→ lokalny MLflow (opcjonalnie)
→ lokalna ocena promptów
→ Langfuse (opcjonalnie, po decyzji prywatności)
→ publikacyjny DryRun
→ jawna zgoda na publikację modeli
→ staging DigitalOcean App Platform
→ produkcja
```

## 3. Instalacja HF20

```powershell
$ProjectRoot = "C:\...\GIOS_IMGW_Forecast_Suite_1.7.0_Hourly_MultiTarget_Pluggable"
$RuntimeRoot = Join-Path $env:ProgramData "SmogAI"
$HotfixRoot = "C:\Temp\GIOS_IMGW_1.7.0_HF20_ServingTime_MLflow_Langfuse_DigitalOcean_Hotfix"
$ApplyScript = Join-Path $HotfixRoot "Apply-ServingTime-MLflow-Langfuse-HF20.ps1"

$Parameters = @{
    ProjectRoot = $ProjectRoot
    RuntimeRoot = $RuntimeRoot
}

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& $ApplyScript @Parameters
```

Wymagany finał:

```text
HOTFIX 1.7.0 HF20 ZASTOSOWANY POPRAWNIE.
```

## 4. Konfiguracja czasu

```powershell
$Parameters = @{
    ProjectRoot = $ProjectRoot
    RuntimeRoot = $RuntimeRoot
}

.\scripts\Configure-HF20-TimeContract.ps1 @Parameters
```

Skrypt ustawia w konfiguracji głównej i local-only:

```yaml
hourly_forecasting:
  serving_horizon_hours: 48
  maximum_source_delay_hours: 12
  maximum_model_horizon_hours: 60
```

Dla profili `quick` i `full` dodaje koszyk do h60. Nie włącza MLflow,
Langfuse ani publikacji.

## 5. Weryfikacja istniejącego snapshotu

Nie twórz nowego snapshotu, jeżeli ostatni przeszedł SHA-256:

```powershell
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LocalConfig = Join-Path $RuntimeRoot "config.local-training.yaml"
$LocalEnv = Join-Path $RuntimeRoot "smog-ai.local-training.env"

& $Python -m smog_ai training-snapshot-status `
    --profile quick `
    --verify-checksum `
    --config $LocalConfig `
    --env-file $LocalEnv
```

Wymagane:

```text
valid=true
checksum_match=true
immutable=true
```

## 6. Trening HF20 na tym samym snapshotcie

Terminal roboczy:

```powershell
$Parameters = @{
    ProjectRoot = $ProjectRoot
    RuntimeRoot = $RuntimeRoot
    Targets = "PM10,PM2.5,temperature_c,precipitation_mm"
    Profile = "quick"
    Snapshot = "latest"
}

.\scripts\Run-HF20-TimeContract-Retrain.ps1 @Parameters
```

Skrypt nie pobiera danych i nie tworzy snapshotu. Trenuje h1–h60, eksportuje
lokalne porównanie modeli, wykonuje twardą bramę jakości, generuje 48
przyszłych godzin i uruchamia audyt kontraktu czasu.

### Monitoring

W drugim terminalu:

```powershell
$Parameters = @{
    ProjectRoot = $ProjectRoot
    RuntimeRoot = $RuntimeRoot
    Mode = "quick"
    RefreshSeconds = 5
}

.\scripts\Watch-TrainingProgress.ps1 @Parameters
```

## 7. Samodzielny audyt prognozy

Po treningu można ponawiać samą predykcję i audyt:

```powershell
$Parameters = @{
    ProjectRoot = $ProjectRoot
    RuntimeRoot = $RuntimeRoot
}

.\scripts\Run-HF20-ForecastAudit.ps1 @Parameters
```

Wymagane dla każdego parametru:

```text
serving_lead_hours = 1..48
model_horizon_hours <= 60
target_time > forecast_created_at
wspólna siatka target_time
brak NaN/Inf
```

Opad może pozostać `experimental`; taki model nie przejdzie publikacyjnej
bramy nawet wtedy, gdy jest aktywny lokalnie do testowania wykresów.

## 8. Lokalne porównanie modeli bez MLflow

Artefakt porównania powstaje nawet bez pakietu MLflow:

```powershell
.\scripts\Export-HF20-ModelComparison.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot
```

Plik:

```text
C:\ProgramData\SmogAI\reports\mlflow\model-comparison.json
```

Dashboard korzysta z tego samego kontraktu co endpoint:

```text
GET /api/v1/models/compare
```

## 9. Lokalny MLflow

Instalacja opcjonalna nie włącza chmury:

```powershell
.\scripts\Install-HF20-OptionalTools.ps1 `
    -ProjectRoot $ProjectRoot `
    -InstallMLflow
```

Terminal MLflow:

```powershell
.\scripts\Start-LocalMLflow.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -Port 5000
```

W drugim terminalu włącz tracking w konfiguracji local-only:

```powershell
.\scripts\Enable-LocalMLflow.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -Port 5000
```

Następny trening zapisze kandydatów, metryki, parametry, `dataset_id`, SHA-256
i wybór zwycięzcy. UI:

```text
http://127.0.0.1:5000
```

## 10. Lokalna aplikacja

Terminal API:

```powershell
.\scripts\Start-LocalApi.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot
```

Terminal dashboardu:

```powershell
.\scripts\Start-LocalDashboard.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot
```

Smoke test:

```powershell
.\scripts\Test-LocalServer.ps1 -AsJson
```

## 11. Ocena promptów

Bez przesyłania ocen do Langfuse:

```powershell
.\scripts\Run-HF20-PromptQualityEvaluation.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -ApiUrl "http://127.0.0.1:8000/api/v1"
```

Raport:

```text
C:\ProgramData\SmogAI\reports\prompt-evaluation\prompt-evaluation-*.json
```

Dopiero po świadomym włączeniu Langfuse i ustawieniu kluczy można dodać:

```powershell
-SubmitFeedback
```

Bez backendu Langfuse endpoint zapisuje oceny lokalnie w JSONL.

## 12. Preflight DigitalOcean bez wdrożenia

```powershell
.\scripts\Test-HF20-DigitalOceanReadiness.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -StrictArtifacts
```

Kontrola nie wykonuje deploymentu ani uploadu. Weryfikuje App Spec, staging,
kontrakt read-only, modele, snapshot i artefakty.

## 13. Jawna publikacja zatwierdzonych modeli

Najpierw DryRun:

```powershell
.\scripts\Publish-Approved-HF20-Artifacts.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -Targets "PM10,PM2.5,temperature_c" `
    -PublishComparison `
    -DryRun
```

Właściwa publikacja wymaga jednocześnie:

1. przełącznika `-IApproveDigitalOceanUpload`;
2. ręcznego wpisania frazy `PUBLISH APPROVED MODELS ONLY`;
3. przejścia jakościowej bramy każdego celu.

Mechanizm publikuje wyłącznie model, kartę, metryki, aktywny pointer i
porównanie. Nie publikuje danych surowych, SQLite, snapshotu ani ramek.

## 14. App Platform

Domyślne App Spec uruchamia tylko:

```text
FastAPI — read-only Spaces Bridge
Streamlit — prywatne połączenie do FastAPI
```

Nie uruchamia treningu, importu, migracji ani MLflow. MLflow UI pozostaje
lokalne albo może później zostać wdrożone jako osobny, kosztowo zatwierdzony
system z trwałym backendem bazodanowym i osobnym prefixem artefaktów.

Langfuse jest aktywowany wyłącznie przez:

```text
SMOG_AI_OBSERVABILITY_BACKEND=langfuse
LANGFUSE_PUBLIC_KEY
LANGFUSE_SECRET_KEY
LANGFUSE_BASE_URL
```

Najpierw wdrażamy `.do/app.dev.yaml` ze stagingowym prefixem Spaces, a dopiero
po smoke teście `.do/app.yaml`.
