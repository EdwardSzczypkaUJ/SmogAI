from __future__ import annotations

import os
from pathlib import Path

from server.api.settings import ServerSettings
from server.application.documentation_source import (
    LocalDocumentationSource,
    ObjectStoreDocumentationSource,
)
from server.application.model_source import ObjectStoreModelSource
from server.application.query import ForecastQueryService
from server.application.snapshot_source import SnapshotSource
from server.application.spatial_source import ObjectStoreSpatialSource
from smog_ai.artifacts.layout import ArtifactLayout
from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.nlp.interpreter import create_intent_interpreter
from smog_ai.observability.bridge import create_observability
from smog_ai.places.gazetteer import PolishGazetteerResolver
from smog_ai.places.http_geocoder import HttpGeocoderResolver, OfflineFirstPlaceResolver
from smog_ai.storage.factory import create_object_store


def create_artifact_repository_from_settings(
    settings: ServerSettings,
) -> ArtifactRepository:
    store = create_object_store(settings.object_storage_config())
    return ArtifactRepository(
        store,
        layout=ArtifactLayout(schema_version=settings.artifact_schema_version),
    )


def create_query_service(
    settings: ServerSettings,
    snapshot_source: SnapshotSource,
    artifact_repository: ArtifactRepository | None = None,
) -> ForecastQueryService:
    observability = create_observability(
        backend=settings.observability_backend,
        environment=settings.observability_environment,
        release=settings.observability_release,
        strict=settings.observability_strict,
    )
    interpreter = create_intent_interpreter(
        provider=settings.nlp_provider,
        model=settings.nlp_model,
        base_url=settings.nlp_base_url,
        api_key=os.getenv(settings.nlp_api_key_env) or os.getenv("OPENAI_API_KEY"),
        timeout_seconds=settings.nlp_timeout_seconds,
        max_retries=settings.nlp_max_retries,
        temperature=settings.nlp_temperature,
        timezone=settings.display_timezone,
        allow_rule_based_fallback=settings.nlp_allow_rule_based_fallback,
        observability=observability,
    )
    repository = artifact_repository or create_artifact_repository_from_settings(settings)
    spatial_source = (
        ObjectStoreSpatialSource(
            repository,
            cache_ttl_seconds=settings.spatial_cache_ttl_seconds,
        )
        if settings.spatial_enabled
        else None
    )
    place_resolver = None
    if settings.spatial_enabled:
        offline_resolver = PolishGazetteerResolver(settings.spatial_places_csv)
        place_resolver = offline_resolver
        if settings.geocoder_provider in {"http", "nominatim"}:
            remote_resolver = HttpGeocoderResolver(
                endpoint=settings.geocoder_endpoint or "",
                user_agent=settings.geocoder_user_agent or "",
                cache_path=settings.geocoder_cache_path,
                timeout_seconds=settings.geocoder_timeout_seconds,
                minimum_interval_seconds=settings.geocoder_minimum_interval_seconds,
            )
            place_resolver = OfflineFirstPlaceResolver(offline_resolver, remote_resolver)
    return ForecastQueryService(
        snapshot_source=snapshot_source,
        spatial_source=spatial_source,
        place_resolver=place_resolver,
        interpreter=interpreter,
        observability=observability,
        flush_observability=settings.observability_flush_on_request,
        prompt_template_version=settings.prompt_template_version,
    )


def create_documentation_source(
    settings: ServerSettings,
    artifact_repository: ArtifactRepository | None = None,
):  # type: ignore[no-untyped-def]
    import smog_ai

    resources = Path(smog_ai.__file__).resolve().parent / "resources" / "docs"
    fallback = LocalDocumentationSource(
        processing_path=resources / "TECHNICAL_PROCESSING_PL.md",
        processing_latex_path=resources / "DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex",
        mathematics_path=resources / "MATHEMATICAL_MODEL_PL.md",
        plugin_path=resources / "MODEL_PLUGIN_GUIDE_PL.md",
        latex_path=resources / "DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex",
        hf20_path=resources / "HF20_TIME_CONTRACT_MLOPS_PL.md",
        hf20_latex_path=resources / "DODATEK_TECHNICZNY_HF20_TIME_CONTRACT_MLOPS_PL.tex",
    )
    repository = artifact_repository
    if repository is None and settings.uses_object_store:
        repository = create_artifact_repository_from_settings(settings)
    if repository is None:
        return fallback
    return ObjectStoreDocumentationSource(repository=repository, fallback=fallback)


def create_model_source(
    settings: ServerSettings,
    artifact_repository: ArtifactRepository | None = None,
):  # type: ignore[no-untyped-def]
    repository = artifact_repository or create_artifact_repository_from_settings(settings)
    return ObjectStoreModelSource(repository)
