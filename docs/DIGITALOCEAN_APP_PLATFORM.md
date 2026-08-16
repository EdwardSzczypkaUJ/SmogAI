# DigitalOcean App Platform 1.7.0 — FastAPI, Streamlit i GitHub Actions

## 1. Warunek wejściowy

Najpierw lokalny automat musi opublikować do prywatnego Space:

```text
serving/latest.json
serving/releases/release=<ID>/manifest.json
serving/releases/release=<ID>/surfaces/<PARAMETR>/h001.json.gz
documentation/latest.json
```

App Platform odczytuje gotowe, skompresowane wyniki. Nie zawiera kolektora,
treningu, predykcji ani interpolatora przestrzennego. Nie otrzymuje bazy SQLite,
historii pomiarów, datasetów treningowych ani ciężkiego `dashboard_snapshot`.

`serving/latest.json` jest małym, atomowo podmienianym wskaźnikiem. FastAPI
czyta manifest, a następnie pobiera i rozpakowuje tylko jedną potrzebną
powierzchnię parametr × godzina. Nie rozpakowuje całego wydania podczas startu.

## 2. Komponenty

`.do/app.yaml` definiuje dokładnie dwa serwisy:

```text
api        FastAPI: `/api/*`, NLP, odczyt Spaces, wybór dokładnego target_time
dashboard  Streamlit: `/`, mapa, profile godzinowe, modele i dokumentacja
```

Nie ma `jobs` ani bazy danych. Dashboard nie otrzymuje kluczy Spaces; komunikuje
się z API przez `${api.PRIVATE_URL}/api/v1`.

## 3. Repozytorium

```powershell
git init
git branch -M main
git add .
git commit -m 'Initial release 1.7.0'
gh repo create TWOJ_LOGIN/gios-imgw-forecast --private --source . --remote origin --push
```

Nie commituj `%ProgramData%\SmogAI`, `.venv`, baz, logów ani sekretów.

## 4. Autoryzacja i token

W DigitalOcean nadaj App Platform jednorazowy dostęp do repozytorium GitHub.
Następnie utwórz Personal Access Token do deploymentu. Token App Platform nie
jest kluczem Spaces.

Dla API utwórz osobny klucz Spaces ograniczony do właściwego bucketu z prawem
`Read`. Lokalny pipeline nadal używa klucza `Read/Write/Delete`.

## 5. Konfiguracja repozytorium

```powershell
$Token = Read-Host 'DigitalOcean Personal Access Token' -AsSecureString

.\scripts\Configure-GitHubDeploy.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot (Join-Path $env:ProgramData 'SmogAI') `
  -AppName 'smog-ai-krakow-prod' `
  -CustomerName 'Smog AI Kraków' `
  -DigitalOceanAccessToken $Token
```

Następnie nadpisz w GitHub Secrets lokalny klucz Spaces osobnym kluczem
read-only:

```powershell
gh secret set SPACES_ACCESS_KEY_ID
gh secret set SPACES_SECRET_ACCESS_KEY
```

## 6. CI/CD

Workflow realizuje:

```text
pull request → Python 3.12 → pytest → release gate → bez deployu
push/merge main → te same kontrole → digitalocean/app_action/deploy@v2
→ health FastAPI → health Streamlit
```

`deploy_on_push: false` jest celowe. Deployment wykonuje wyłącznie workflow po
przejściu testów, więc nie powstają dwie równoległe publikacje.

## 7. Pierwszy deploy

```powershell
gh workflow run 'CI and deploy to DigitalOcean App Platform' --ref main
gh run watch --exit-status
```

Weryfikacja:

```powershell
$AppUrl = 'https://TWOJA-APLIKACJA.ondigitalocean.app'
Invoke-RestMethod "$AppUrl/api/v1/health"
Invoke-RestMethod "$AppUrl/api/v1/ready"
Invoke-RestMethod "$AppUrl/api/v1/spatial/manifest"
Invoke-RestMethod "$AppUrl/api/v1/models"
Invoke-WebRequest "$AppUrl/api/v1/docs/processing"
Start-Process $AppUrl
```

Test dokładnej godziny:

```powershell
$Body = @{
  text = 'Jutro o 17:00 jadę do Katowic. Jakie będą PM10, PM2.5, temperatura i opad?'
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "$AppUrl/api/v1/query" `
  -ContentType 'application/json; charset=utf-8' -Body $Body |
  ConvertTo-Json -Depth 30
```

Odpowiedź powinna wskazywać `direct_hourly_surface`, dokładny `target_time`,
czas bazowy, horyzont i punkt siatki.

## 8. Aktualizowanie danych bez redeployu

Nowy lokalny pipeline publikuje niezmienne artefakty i atomowo aktualizuje
`latest.json`. FastAPI odświeża cache po TTL. Nowy model, prognozy, mapy i
dokumentacja nie wymagają deploymentu kodu.
