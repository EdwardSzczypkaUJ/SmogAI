# Przykładowe logi

```text
2026-08-01T21:07:00Z INFO run_id=... task=hourly stage=collect_gios downloaded=1432 inserted=108
2026-08-01T21:08:13Z INFO stage=collect_imgw downloaded=67 inserted=21
2026-08-01T21:08:25Z INFO stage=export_object_store object=datasets/bronze/... status=ok
2026-08-01T21:09:40Z INFO stage=predict parameter=PM10 horizon=24 inserted=164
2026-08-01T21:09:55Z INFO stage=build_spatial_surfaces algorithm=idw cells=5124 surfaces=6 loo_mae=4.81
2026-08-01T21:10:03Z INFO stage=publish maps_latest=updated forecasts_latest=updated
2026-08-01T21:10:04Z INFO status=success duration_seconds=184 exit_code=0
```

Błąd Spaces nie usuwa danych lokalnych:

```text
WARN stage=publish status=failed retryable=true outbox=pending next_attempt_at=...
INFO pipeline_status=partial_success exit_code=6 local_data_preserved=true
```
