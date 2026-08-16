from __future__ import annotations

from server.api.settings import ServerSettings


def test_analytics_spaces_uses_separate_bucket_prefix_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANALYTICS_SPACES_BUCKET", "analytics-bucket")
    monkeypatch.setenv("ANALYTICS_SPACES_REGION", "fra1")
    monkeypatch.setenv("ANALYTICS_SPACES_ENDPOINT_URL", "https://fra1.digitaloceanspaces.com")
    monkeypatch.setenv("ANALYTICS_SPACES_PREFIX", "smog-ai/krakow/analytics-v1")

    settings = ServerSettings.from_env()
    config = settings.analytics_object_storage_config()

    assert settings.uses_separate_analytics_store is True
    assert config.bucket == "analytics-bucket"
    assert config.prefix == "smog-ai/krakow/analytics-v1"
    assert config.access_key_env == "ANALYTICS_SPACES_ACCESS_KEY_ID"
    assert config.secret_key_env == "ANALYTICS_SPACES_SECRET_ACCESS_KEY"


def test_spatial_cache_limit_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("SMOG_AI_SPATIAL_CACHE_MAX_ITEMS", "0")
    settings = ServerSettings.from_env()

    try:
        settings.validate()
    except RuntimeError as exc:
        assert "SMOG_AI_SPATIAL_CACHE_MAX_ITEMS" in str(exc)
    else:
        raise AssertionError("zero cache limit should have been rejected")
