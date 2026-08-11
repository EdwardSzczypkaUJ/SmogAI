from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from smog_ai.hourly.features import expand_prediction_horizons
from smog_ai.hourly.time_contract import (
    SourceDataTooOldError,
    build_forecast_time_contract,
)


def test_time_contract_maps_delayed_source_to_48_future_serving_hours() -> None:
    contract = build_forecast_time_contract(
        source_origin_time=datetime(2026, 8, 9, 9, tzinfo=UTC),
        forecast_created_at=datetime(2026, 8, 9, 13, 55, tzinfo=UTC),
        serving_horizon_hours=48,
        maximum_source_delay_hours=12,
        maximum_model_horizon_hours=60,
    )

    assert contract.serving_leads == tuple(range(1, 49))
    assert contract.model_horizons == tuple(range(5, 53))
    assert contract.target_times[0] == datetime(2026, 8, 9, 14, tzinfo=UTC)
    assert contract.target_times[-1] == datetime(2026, 8, 11, 13, tzinfo=UTC)
    assert all(value > contract.forecast_created_at for value in contract.target_times)


def test_time_contract_uses_h60_at_maximum_supported_delay() -> None:
    contract = build_forecast_time_contract(
        source_origin_time=datetime(2026, 8, 9, 2, tzinfo=UTC),
        forecast_created_at=datetime(2026, 8, 9, 13, 55, tzinfo=UTC),
        serving_horizon_hours=48,
        maximum_source_delay_hours=12,
        maximum_model_horizon_hours=60,
    )

    assert contract.model_horizons[0] == 12
    assert contract.model_horizons[-1] == 59



def test_time_contract_allows_exact_twelve_hour_source_age_through_h60() -> None:
    contract = build_forecast_time_contract(
        source_origin_time=datetime(2026, 8, 9, 2, tzinfo=UTC),
        forecast_created_at=datetime(2026, 8, 9, 14, tzinfo=UTC),
        serving_horizon_hours=48,
        maximum_source_delay_hours=12,
        maximum_model_horizon_hours=60,
    )

    assert contract.source_age_hours == 12.0
    assert contract.model_horizons[0] == 13
    assert contract.model_horizons[-1] == 60


def test_time_contract_rejects_source_age_just_over_twelve_hours() -> None:
    with pytest.raises(SourceDataTooOldError):
        build_forecast_time_contract(
            source_origin_time=datetime(2026, 8, 9, 1, 59, tzinfo=UTC),
            forecast_created_at=datetime(2026, 8, 9, 14, tzinfo=UTC),
            serving_horizon_hours=48,
            maximum_source_delay_hours=12,
            maximum_model_horizon_hours=60,
        )

def test_time_contract_rejects_source_older_than_configured_sla() -> None:
    with pytest.raises(SourceDataTooOldError):
        build_forecast_time_contract(
            source_origin_time=datetime(2026, 8, 9, 1, tzinfo=UTC),
            forecast_created_at=datetime(2026, 8, 9, 13, 55, tzinfo=UTC),
            serving_horizon_hours=48,
            maximum_source_delay_hours=12,
            maximum_model_horizon_hours=60,
        )


def test_prediction_expansion_preserves_serving_and_model_horizon_semantics() -> None:
    contract = build_forecast_time_contract(
        source_origin_time=datetime(2026, 8, 9, 9, tzinfo=UTC),
        forecast_created_at=datetime(2026, 8, 9, 13, 55, tzinfo=UTC),
        serving_horizon_hours=4,
        maximum_source_delay_hours=12,
        maximum_model_horizon_hours=60,
    )
    base = pd.DataFrame(
        {
            "air_station_id": [1],
            "measurement_time": [pd.Timestamp("2026-08-09T09:00:00Z")],
            "current_value": [12.0],
        }
    )

    expanded = expand_prediction_horizons(base, time_contract=contract)

    assert expanded["serving_lead_hours"].tolist() == [1, 2, 3, 4]
    assert expanded["horizon_hours"].tolist() == [5, 6, 7, 8]
    assert expanded["model_horizon_hours"].tolist() == [5, 6, 7, 8]
    assert pd.to_datetime(expanded["target_time"], utc=True).tolist() == [
        pd.Timestamp("2026-08-09T14:00:00Z"),
        pd.Timestamp("2026-08-09T15:00:00Z"),
        pd.Timestamp("2026-08-09T16:00:00Z"),
        pd.Timestamp("2026-08-09T17:00:00Z"),
    ]
