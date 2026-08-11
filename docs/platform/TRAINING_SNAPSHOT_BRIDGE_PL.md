# TrainingSnapshotBridge — niezmienny zbiór treningowy przy aktywnym imporcie

## Cel

`TrainingSnapshotBridge` oddziela żywą bazę ingestu od danych używanych do
uczenia modelu. Importer może nadal dopisywać rekordy do:

```text
C:\ProgramData\SmogAI\data\smog.db
```

podczas gdy trening czyta spójną, niezmienną kopię:

```text
C:\ProgramData\SmogAI\training-datasets\<profil>\dataset-<dataset_id>\smog.db
```

Kopia jest tworzona przez SQLite Online Backup API. Nie używa zwykłego
`Copy-Item`, dlatego obejmuje jeden transakcyjnie spójny stan bazy także wtedy,
gdy trwa zapis w trybie WAL.

## Bridge

```python
class TrainingSnapshotBridge:
    def create(...): ...
    def latest(profile): ...
    def resolve(profile, selector): ...
    def validate(snapshot, verify_checksum=True): ...
    def cleanup(profile, keep=None): ...
```

Obsługiwane selektory:

- `auto` — utwórz nowy snapshot, chyba że konfiguracja pozwala użyć świeżego;
- `latest` — użyj ostatniego snapshotu danego profilu;
- `live` — tryb diagnostyczny, bez gwarancji nieruchomości;
- konkretny `dataset_id` — odtwórz eksperyment na tej samej bazie.

## Proweniencja modelu

Każdy model wytrenowany przez `snapshot-train-hourly` zapisuje w metrykach:

```json
{
  "data_provenance": {
    "dataset_id": "training-20260809T...",
    "training_snapshot": {
      "database_sha256": "...",
      "created_at": "...",
      "data_ranges": {},
      "row_counts": {},
      "immutable": true
    }
  }
}
```

Sama baza snapshotu pozostaje lokalna. Do DigitalOcean Spaces trafia jedynie
mały manifest proweniencji; nie publikuje się kopii SQLite zawierającej dane
źródłowe.

## Współbieżność

Dozwolone:

```text
import/backfill -> żywa SQLite
trening        -> snapshot SQLite
FastAPI        -> gotowe artefakty w Spaces
Streamlit      -> FastAPI
```

Niedozwolone jest uruchomienie dwóch treningów jednocześnie. Chroni przed tym
wspólny `ProcessLease` `snapshot-hourly-training`.

## Progress i ETA

Reporter ma dwa ważone etapy:

```text
snapshot  10%
training  90%
```

Etap snapshotu raportuje skopiowane strony SQLite. Etap modelowy nadal pokazuje
cel, providera, fold walidacyjny, budżet i ETA.

## Polecenia

```powershell
python -m smog_ai create-training-snapshot `
  --profile quick `
  --targets "PM2.5"

python -m smog_ai training-snapshot-status `
  --profile quick `
  --verify-checksum

python -m smog_ai snapshot-train-hourly `
  --profile quick `
  --targets "PM2.5" `
  --snapshot auto
```
