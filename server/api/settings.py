from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from smog_ai import __version__
from smog_ai.config import ObjectStorageConfig


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_places_path() -> Path:
    import smog_ai

    return Path(smog_ai.__file__).resolve().parent / "resources" / "polish_places.csv"


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Runtime settings shared by FastAPI and server-side Streamlit adapters.

    The first five fields intentionally preserve the positional constructor used
    by the local test suite.  App Platform can use DigitalOcean Spaces as the
    canonical source, while filesystem and database stores remain available as
    interchangeable Bridge implementations.
    """

    data_dir: Path
    api_token: str
    max_upload_bytes: int
    keep_versions: int
    rate_limit_per_minute: int
    database_url: str | None = None
    storage_backend: str = "auto"
    environment: str = "development"
    docs_enabled: bool = True
    app_version: str = __version__
    commit_sha: str | None = None
    customer_name: str = "GIOŚ/IMGW Forecast Suite"
    uploads_enabled: bool = True

    object_store_backend: str = "local"
    object_store_local_root: Path = Path("object-store")
    object_store_bucket: str | None = None
    object_store_endpoint: str | None = None
    object_store_region: str | None = None
    object_store_prefix: str = "smog-ai"
    object_store_access_key_env: str = "SPACES_ACCESS_KEY_ID"
    object_store_secret_key_env: str = "SPACES_SECRET_ACCESS_KEY"
    object_store_session_token_env: str | None = None
    object_store_verify_tls: bool = True
    object_store_addressing_style: str = "virtual"
    artifact_schema_version: str = "1"
    spatial_enabled: bool = True
    spatial_cache_ttl_seconds: float = 60.0
    spatial_cache_max_items: int = 64
    spatial_places_csv: Path = _default_places_path()

    nlp_provider: str = "rule_based"
    nlp_model: str = "gpt-5.4-mini"
    nlp_base_url: str = "https://api.openai.com/v1"
    nlp_api_key_env: str = "LLM_API_KEY"
    nlp_timeout_seconds: float = 30.0
    nlp_max_retries: int = 2
    nlp_temperature: float = 0.0
    nlp_allow_rule_based_fallback: bool = True
    geocoder_provider: str = "offline"
    geocoder_endpoint: str | None = None
    geocoder_user_agent: str | None = None
    geocoder_cache_path: Path = Path("server_data/geocoder-cache.json")
    geocoder_timeout_seconds: float = 8.0
    geocoder_minimum_interval_seconds: float = 1.0
    display_timezone: str = "Europe/Warsaw"

    observability_backend: str = "none"
    observability_environment: str = "development"
    observability_release: str = __version__
    observability_flush_on_request: bool = False
    observability_strict: bool = False
    prompt_template_version: str = "air-query-v1"
    prompt_feedback_enabled: bool = True
    prompt_feedback_path: Path = Path("server_data/prompt-feedback.jsonl")
    own_analytics_enabled: bool = True
    own_analytics_private_prefix: str = "private/analytics"
    own_analytics_retention_days: int = 90
    analytics_object_store_bucket: str | None = None
    analytics_object_store_endpoint: str | None = None
    analytics_object_store_region: str | None = None
    analytics_object_store_prefix: str = "smog-ai/analytics"
    analytics_object_store_access_key_env: str = "ANALYTICS_SPACES_ACCESS_KEY_ID"
    analytics_object_store_secret_key_env: str = "ANALYTICS_SPACES_SECRET_ACCESS_KEY"
    mlflow_ui_url: str | None = None

    @classmethod
    def from_env(cls) -> "ServerSettings":
        data_dir = Path(os.getenv("SMOG_AI_SERVER_DATA_DIR", "server_data")).expanduser()
        database_url = os.getenv("SMOG_AI_SERVER_DATABASE_URL") or os.getenv("DATABASE_URL")
        object_store_local_root = Path(
            os.getenv("SMOG_AI_OBJECT_STORE_LOCAL_ROOT", "object-store")
        ).expanduser()
        environment = os.getenv("SMOG_AI_ENV", "development").strip().lower()
        return cls(
            data_dir=data_dir,
            api_token=os.getenv("SMOG_AI_SERVER_API_TOKEN", "change-me"),
            max_upload_bytes=int(
                os.getenv("SMOG_AI_SERVER_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
            ),
            keep_versions=int(os.getenv("SMOG_AI_SERVER_KEEP_VERSIONS", "50")),
            rate_limit_per_minute=int(
                os.getenv("SMOG_AI_SERVER_RATE_LIMIT_PER_MINUTE", "60")
            ),
            database_url=database_url,
            storage_backend=os.getenv("SMOG_AI_SERVER_STORAGE_BACKEND", "auto")
            .strip()
            .lower(),
            environment=environment,
            docs_enabled=_env_bool("SMOG_AI_SERVER_DOCS_ENABLED", True),
            app_version=os.getenv("SMOG_AI_APP_VERSION", __version__),
            commit_sha=os.getenv("SMOG_AI_COMMIT_SHA") or os.getenv("SOURCE_VERSION"),
            customer_name=os.getenv(
                "SMOG_AI_CUSTOMER_NAME", "GIOŚ/IMGW Forecast Suite"
            ).strip(),
            uploads_enabled=_env_bool("SMOG_AI_SERVER_UPLOADS_ENABLED", True),
            object_store_backend=os.getenv(
                "SMOG_AI_OBJECT_STORE_BACKEND", "local"
            ).strip().lower(),
            object_store_local_root=object_store_local_root,
            object_store_bucket=_env_optional("SMOG_AI_OBJECT_STORE_BUCKET")
            or _env_optional("SPACES_BUCKET"),
            object_store_endpoint=_env_optional("SMOG_AI_OBJECT_STORE_ENDPOINT")
            or _env_optional("SPACES_ENDPOINT_URL"),
            object_store_region=_env_optional("SMOG_AI_OBJECT_STORE_REGION")
            or _env_optional("SPACES_REGION"),
            object_store_prefix=os.getenv(
                "SMOG_AI_OBJECT_STORE_PREFIX", os.getenv("SPACES_PREFIX", "smog-ai")
            ).strip("/ "),
            object_store_access_key_env=os.getenv(
                "SMOG_AI_OBJECT_STORE_ACCESS_KEY_ENV", "SPACES_ACCESS_KEY_ID"
            ),
            object_store_secret_key_env=os.getenv(
                "SMOG_AI_OBJECT_STORE_SECRET_KEY_ENV", "SPACES_SECRET_ACCESS_KEY"
            ),
            object_store_session_token_env=_env_optional(
                "SMOG_AI_OBJECT_STORE_SESSION_TOKEN_ENV"
            ),
            object_store_verify_tls=_env_bool("SMOG_AI_OBJECT_STORE_VERIFY_TLS", True),
            object_store_addressing_style=os.getenv(
                "SMOG_AI_OBJECT_STORE_ADDRESSING_STYLE", "virtual"
            ),
            artifact_schema_version=os.getenv("SMOG_AI_ARTIFACT_SCHEMA_VERSION", "1"),
            spatial_enabled=_env_bool("SMOG_AI_SPATIAL_ENABLED", True),
            spatial_cache_ttl_seconds=float(
                os.getenv("SMOG_AI_SPATIAL_CACHE_TTL_SECONDS", "60")
            ),
            spatial_cache_max_items=int(
                os.getenv("SMOG_AI_SPATIAL_CACHE_MAX_ITEMS", "64")
            ),
            spatial_places_csv=Path(
                os.getenv("SMOG_AI_SPATIAL_PLACES_CSV", str(_default_places_path()))
            ).expanduser(),
            nlp_provider=os.getenv("SMOG_AI_LLM_PROVIDER", "rule_based").strip().lower(),
            nlp_model=os.getenv("SMOG_AI_LLM_MODEL", "gpt-5.4-mini").strip(),
            nlp_base_url=os.getenv(
                "SMOG_AI_LLM_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            nlp_api_key_env=os.getenv("SMOG_AI_LLM_API_KEY_ENV", "LLM_API_KEY"),
            nlp_timeout_seconds=float(os.getenv("SMOG_AI_LLM_TIMEOUT_SECONDS", "30")),
            nlp_max_retries=int(os.getenv("SMOG_AI_LLM_MAX_RETRIES", "2")),
            nlp_temperature=float(os.getenv("SMOG_AI_LLM_TEMPERATURE", "0")),
            nlp_allow_rule_based_fallback=_env_bool(
                "SMOG_AI_LLM_ALLOW_RULE_FALLBACK", True
            ),
            geocoder_provider=os.getenv("SMOG_AI_GEOCODER_PROVIDER", "offline").strip().lower(),
            geocoder_endpoint=_env_optional("SMOG_AI_GEOCODER_ENDPOINT"),
            geocoder_user_agent=_env_optional("SMOG_AI_GEOCODER_USER_AGENT"),
            geocoder_cache_path=Path(
                os.getenv("SMOG_AI_GEOCODER_CACHE_PATH", "server_data/geocoder-cache.json")
            ).expanduser(),
            geocoder_timeout_seconds=float(os.getenv("SMOG_AI_GEOCODER_TIMEOUT_SECONDS", "8")),
            geocoder_minimum_interval_seconds=float(
                os.getenv("SMOG_AI_GEOCODER_MINIMUM_INTERVAL_SECONDS", "1")
            ),
            display_timezone=os.getenv("DISPLAY_TIMEZONE", "Europe/Warsaw"),
            observability_backend=os.getenv(
                "SMOG_AI_OBSERVABILITY_BACKEND", "none"
            ).strip().lower(),
            observability_environment=os.getenv(
                "SMOG_AI_OBSERVABILITY_ENVIRONMENT", environment
            ),
            observability_release=os.getenv(
                "SMOG_AI_OBSERVABILITY_RELEASE", os.getenv("SMOG_AI_APP_VERSION", __version__)
            ),
            observability_flush_on_request=_env_bool(
                "SMOG_AI_OBSERVABILITY_FLUSH_ON_REQUEST", False
            ),
            observability_strict=_env_bool("SMOG_AI_OBSERVABILITY_STRICT", False),
            prompt_template_version=os.getenv(
                "SMOG_AI_PROMPT_TEMPLATE_VERSION", "air-query-v1"
            ),
            prompt_feedback_enabled=_env_bool(
                "SMOG_AI_PROMPT_FEEDBACK_ENABLED", True
            ),
            prompt_feedback_path=Path(
                os.getenv(
                    "SMOG_AI_PROMPT_FEEDBACK_PATH",
                    str(data_dir / "prompt-feedback.jsonl"),
                )
            ).expanduser(),
            own_analytics_enabled=_env_bool("SMOG_AI_OWN_ANALYTICS_ENABLED", True),
            own_analytics_private_prefix=os.getenv(
                "SMOG_AI_OWN_ANALYTICS_PRIVATE_PREFIX", "private/analytics"
            ).strip("/ "),
            own_analytics_retention_days=int(
                os.getenv(
                    "SMOG_AI_ANALYTICS_RETENTION_DAYS",
                    os.getenv("ANALYTICS_RETENTION_DAYS", "90"),
                )
            ),
            analytics_object_store_bucket=_env_optional("ANALYTICS_SPACES_BUCKET"),
            analytics_object_store_endpoint=_env_optional(
                "ANALYTICS_SPACES_ENDPOINT_URL"
            ),
            analytics_object_store_region=_env_optional("ANALYTICS_SPACES_REGION"),
            analytics_object_store_prefix=os.getenv(
                "ANALYTICS_SPACES_PREFIX", "smog-ai/analytics"
            ).strip("/ "),
            mlflow_ui_url=_env_optional("SMOG_AI_MLFLOW_UI_URL"),
        )

    @property
    def uses_object_store(self) -> bool:
        return self.storage_backend in {"object_store", "object-store", "s3", "spaces"}

    def object_storage_config(self) -> ObjectStorageConfig:
        backend = self.object_store_backend
        if self.storage_backend in {"s3", "spaces"}:
            backend = self.storage_backend
        return ObjectStorageConfig(
            enabled=True,
            backend=backend,  # type: ignore[arg-type]
            local_root=self.object_store_local_root,
            bucket=self.object_store_bucket,
            endpoint_url=self.object_store_endpoint,
            region=self.object_store_region,
            prefix=self.object_store_prefix,
            access_key_env=self.object_store_access_key_env,
            secret_key_env=self.object_store_secret_key_env,
            session_token_env=self.object_store_session_token_env,
            verify_tls=self.object_store_verify_tls,
            addressing_style=self.object_store_addressing_style,  # type: ignore[arg-type]
        )

    @property
    def uses_separate_analytics_store(self) -> bool:
        return bool(self.analytics_object_store_bucket)

    def analytics_object_storage_config(self) -> ObjectStorageConfig:
        return ObjectStorageConfig(
            enabled=True,
            backend="spaces",
            local_root=self.object_store_local_root,
            bucket=self.analytics_object_store_bucket,
            endpoint_url=self.analytics_object_store_endpoint,
            region=self.analytics_object_store_region,
            prefix=self.analytics_object_store_prefix,
            access_key_env=self.analytics_object_store_access_key_env,
            secret_key_env=self.analytics_object_store_secret_key_env,
            verify_tls=self.object_store_verify_tls,
            addressing_style=self.object_store_addressing_style,  # type: ignore[arg-type]
        )

    def validate(self) -> None:
        allowed = {
            "auto",
            "filesystem",
            "database",
            "object_store",
            "object-store",
            "s3",
            "spaces",
        }
        if self.storage_backend not in allowed:
            raise RuntimeError(
                "SMOG_AI_SERVER_STORAGE_BACKEND must be auto, filesystem, database, "
                "object_store, spaces or s3"
            )
        if self.storage_backend == "database" and not self.database_url:
            raise RuntimeError(
                "SMOG_AI_SERVER_DATABASE_URL (or DATABASE_URL) is required for database storage"
            )
        if self.uses_object_store:
            self.object_storage_config()
        if self.keep_versions < 1:
            raise RuntimeError("SMOG_AI_SERVER_KEEP_VERSIONS must be at least 1")
        if self.max_upload_bytes < 1024:
            raise RuntimeError("SMOG_AI_SERVER_MAX_UPLOAD_BYTES is unexpectedly small")
        if self.rate_limit_per_minute < 1:
            raise RuntimeError("SMOG_AI_SERVER_RATE_LIMIT_PER_MINUTE must be at least 1")
        if self.spatial_cache_ttl_seconds < 0:
            raise RuntimeError("SMOG_AI_SPATIAL_CACHE_TTL_SECONDS cannot be negative")
        if self.spatial_cache_max_items < 1:
            raise RuntimeError("SMOG_AI_SPATIAL_CACHE_MAX_ITEMS must be at least 1")
        if self.uses_separate_analytics_store:
            self.analytics_object_storage_config()
        if not 1 <= self.own_analytics_retention_days <= 3650:
            raise RuntimeError(
                "SMOG_AI_ANALYTICS_RETENTION_DAYS must be between 1 and 3650"
            )
        if self.spatial_enabled and not self.spatial_places_csv.exists():
            raise RuntimeError(
                f"Polish places gazetteer does not exist: {self.spatial_places_csv}"
            )
        if self.nlp_provider not in {"rule_based", "openai", "openai_compatible"}:
            raise RuntimeError(
                "SMOG_AI_LLM_PROVIDER must be rule_based, openai or openai_compatible"
            )
        if self.geocoder_provider not in {"offline", "http", "nominatim"}:
            raise RuntimeError(
                "SMOG_AI_GEOCODER_PROVIDER must be offline, http or nominatim"
            )
        if self.geocoder_provider in {"http", "nominatim"}:
            if not self.geocoder_endpoint or not self.geocoder_user_agent:
                raise RuntimeError(
                    "HTTP geocoder requires SMOG_AI_GEOCODER_ENDPOINT and "
                    "SMOG_AI_GEOCODER_USER_AGENT"
                )
        if self.observability_backend not in {"none", "noop", "langfuse"}:
            raise RuntimeError(
                "SMOG_AI_OBSERVABILITY_BACKEND must be none or langfuse"
            )
        if self.environment in {"production", "prod"} and self.uploads_enabled:
            weak_tokens = {"", "change-me", "CHANGE_ME_TO_A_LONG_RANDOM_TOKEN"}
            if self.api_token in weak_tokens or len(self.api_token) < 32:
                raise RuntimeError(
                    "SMOG_AI_SERVER_API_TOKEN must have at least 32 random characters "
                    "when HTTP uploads are enabled in production"
                )
