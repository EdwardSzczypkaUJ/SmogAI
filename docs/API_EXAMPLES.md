# Przykłady API 1.7.0

Założenie lokalne:

```powershell
$Api = 'http://127.0.0.1:8000/api/v1'
```

## Health i gotowość

```powershell
Invoke-RestMethod "$Api/health"
Invoke-RestMethod "$Api/ready"
Invoke-RestMethod "$Api/version"
```

## Manifest dokładnych powierzchni godzinowych

```powershell
Invoke-RestMethod "$Api/spatial/manifest" | ConvertTo-Json -Depth 30
```

Oczekiwane pola:

```text
forecast_mode = horizon-conditioned-hourly
exact_target_time_available = true
horizons_hours = 1..48
parameters = PM10, PM2.5, temperature_c,
             precipitation_probability, precipitation_mm
```

## Powierzchnia dla konkretnej godziny

```powershell
$Target = [uri]::EscapeDataString('2026-08-04T17:00:00+02:00')
Invoke-RestMethod "$Api/spatial/surface?parameter=PM10&target_time=$Target"
```

Jeżeli dokładny pakiet nie istnieje, API zwraca 404 zamiast wybierać najbliższy
horyzont.

## Pytanie tekstowe

```powershell
$Body = @{
  text = 'Jutro o 17:00 jadę do Katowic. Jakie będą PM10, PM2.5, temperatura i opad?'
  session_id = 'manual-test'
} | ConvertTo-Json

$Result = Invoke-RestMethod -Method Post -Uri "$Api/query" `
  -ContentType 'application/json; charset=utf-8' -Body $Body

$Result | ConvertTo-Json -Depth 40
```

Dokładne współrzędne mają pierwszeństwo przed geokoderem:

```powershell
$Body = @{
  text = 'Jutro około 15:17 sprawdź pogodę na startowisku.'
  place_name = 'Startowisko Mieroszów'
  latitude = 50.000000
  longitude = 16.000000
  location_source = 'saved_point'
} | ConvertTo-Json

$Result = Invoke-RestMethod -Method Post -Uri "$Api/query" `
  -ContentType 'application/json; charset=utf-8' -Body $Body
```

Współrzędne w przykładzie są wyłącznie demonstracyjne — należy podać rzeczywisty
punkt użytkownika, kliknięty punkt mapy albo wynik geokodera.

Każdy wynik zawiera m.in.:

```text
requested_target_time
forecast_origin_time
target_time
horizon_hours
exact_time_match
temporal_method
cell_latitude/cell_longitude = dokładny punkt zapytania
spatial_method = quality_weighted_idw
distance_power = 2
station_contributions
temporal_source_times
nearest_station_distance_km
model_version
unit
```

## Profil dnia

```powershell
$Body = @{ text = 'Jutro jadę do Krakowa. Pokaż przebieg całego dnia.' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$Api/query" `
  -ContentType 'application/json' -Body $Body |
  Select-Object -ExpandProperty timeline
```

## Modele i providery

```powershell
Invoke-RestMethod "$Api/models" | ConvertTo-Json -Depth 30
```

## Dokumentacja

```powershell
Invoke-WebRequest "$Api/docs/processing" -OutFile processing.md
Invoke-WebRequest "$Api/docs/processing/source" -OutFile processing.tex
Invoke-WebRequest "$Api/docs/mathematics" -OutFile mathematics.md
Invoke-WebRequest "$Api/docs/mathematics/source" -OutFile mathematics.tex
Invoke-WebRequest "$Api/docs/model-plugins" -OutFile plugins.md
```

## Wyszukiwanie miejsca

```powershell
Invoke-RestMethod "$Api/places/search?q=Katow"
```
