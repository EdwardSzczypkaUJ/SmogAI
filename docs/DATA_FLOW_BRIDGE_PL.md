# Bridge przepływu danych: lokalnie albo przez DigitalOcean Spaces

Smog AI 1.7.0 HF14 rozdziela trzy niezależne decyzje:

1. Kolektory zawsze zapisują pełną historię najpierw do lokalnej SQLite.
2. Trening może czytać bezpośrednio z SQLite albo po round-tripie przez ObjectStore.
3. Modele, mapy i dokumentacja są publikowane przez konfigurowalny ObjectStore.

## Tryby treningu

### direct_local

```yaml
data_flow:
  training_mode: direct_local
```

```text
GIOŚ/IMGW -> SQLite -> feature engineering -> trening lokalny
```

Mirror operacyjny do ObjectStore jest opcjonalny:

```yaml
data_flow:
  mirror_operational_to_object_store: true
```

### object_store_roundtrip

```yaml
data_flow:
  training_mode: object_store_roundtrip
```

```text
GIOŚ/IMGW -> SQLite -> ObjectStore -> odczyt lokalny -> trening
```

Gdy `object_storage.backend=spaces`, jest to wymagany w zadaniu round-trip
przez DigitalOcean Spaces. Gdy backend ma wartość `local`, ten sam kontrakt
artefaktów działa bez chmury.

## Cache historii GIOŚ

```yaml
data_flow:
  history_cache_mode: local | object_store | hybrid
  history_cache_prefix: source-cache/gios-history
```

- `local`: oficjalne źródło -> lokalny cache;
- `object_store`: ObjectStore jest kanonicznym cache;
- `hybrid`: lokalny cache -> ObjectStore -> oficjalne źródło.

## Profile

Całkowicie lokalnie:

```yaml
data_flow:
  training_mode: direct_local
  history_cache_mode: local
object_storage:
  backend: local
```

Lokalny trening, zdalne modele/mapy:

```yaml
data_flow:
  training_mode: direct_local
  history_cache_mode: hybrid
object_storage:
  backend: spaces
```

Pełny round-trip kursowy:

```yaml
data_flow:
  training_mode: object_store_roundtrip
  history_cache_mode: object_store
object_storage:
  backend: spaces
```

## Diagnostyka

```powershell
python -m smog_ai data-flow-status --config ... --env-file ...
```

## Import tylko PM2.5 lokalnie

```powershell
.\scripts\Run-Gios-PM25-Only.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -StartYear 2022 `
  -EndYear 2024 `
  -Source prepared `
  -CacheMode local
```

## Import tylko PM2.5 przez Spaces

```powershell
.\scripts\Run-Gios-PM25-Only.ps1 `
  -ProjectRoot $ProjectRoot `
  -RuntimeRoot $RuntimeRoot `
  -StartYear 2022 `
  -EndYear 2024 `
  -Source prepared `
  -CacheMode object_store `
  -CachePrefix source-cache/gios-history
```
