from __future__ import annotations

from smog_ai.data_flow import (
    DirectLocalTrainingDataBridge,
    ObjectStoreRoundTripTrainingDataBridge,
    create_training_data_bridge,
    data_flow_status,
)


def test_direct_local_training_bridge_configures_database(app_config) -> None:
    app_config.data_flow.training_mode = "direct_local"
    app_config.training.input_source = "object_store"
    bridge = create_training_data_bridge(app_config)
    assert isinstance(bridge, DirectLocalTrainingDataBridge)
    assert bridge.requires_operational_export is False
    bridge.configure_training(app_config)
    assert app_config.training.input_source == "database"


def test_object_store_bridge_is_backend_independent(app_config) -> None:
    app_config.data_flow.training_mode = "object_store_roundtrip"
    app_config.object_storage.backend = "local"
    bridge = create_training_data_bridge(app_config)
    assert isinstance(bridge, ObjectStoreRoundTripTrainingDataBridge)
    assert bridge.requires_operational_export is True
    bridge.configure_training(app_config)
    assert app_config.training.input_source == "object_store"
    assert data_flow_status(app_config)["training"]["object_store_backend"] == "local"


def test_data_flow_status_exposes_reference_profiles(app_config) -> None:
    status = data_flow_status(app_config)
    assert "fully_local" in status["examples"]
    assert "course_spaces_roundtrip" in status["examples"]
