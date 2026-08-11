from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from smog_ai.errors import ConfigurationError


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")
    database_path: Path = Path("data/smog.db")
    models_dir: Path = Path("models")
    snapshots_dir: Path = Path("snapshots")
    logs_dir: Path = Path("logs")
    backups_dir: Path = Path("backups")
    temp_dir: Path = Path("tmp")
    imgw_metadata_csv: Path = Path(__file__).parent / "resources" / "imgw_synop_stations.csv"

    def ensure(self) -> None:
        for path in (
            self.data_dir,
            self.database_path.parent,
            self.models_dir,
            self.snapshots_dir,
            self.logs_dir,
            self.backups_dir,
            self.temp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


class APIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gios_base_url: str = "https://api.gios.gov.pl/pjp-api/v1/rest"
    imgw_synop_url: str = "https://danepubliczne.imgw.pl/api/data/synop"
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 45.0
    max_retries: int = 4
    backoff_base_seconds: float = 1.0
    user_agent: str = "GIOS-IMGW-Forecast-Suite/1.7.0"
    gios_page_size: int = 500
    gios_request_interval_seconds: float = 0.05
    gios_naive_time_zone: str = "Europe/Warsaw"
    imgw_naive_time_zone: str = "UTC"
    # The public SYNOP field ``suma_opadu`` and the terminowe archive code
    # ``WO6G`` are treated as an accumulation ending at measurement_time.
    # The value is not converted into an invented hourly distribution.
    imgw_live_precipitation_accumulation_period_hours: int | None = Field(
        default=6, ge=1, le=24
    )

    @field_validator("gios_base_url", "imgw_synop_url")
    @classmethod
    def https_only(cls, value: str) -> str:
        if not value.lower().startswith("https://"):
            raise ValueError("Production API URLs must use HTTPS")
        return value.rstrip("/")


class ImgwArchiveConfig(BaseModel):
    """Official IMGW terminowe/SYNOP archive backfill.

    ``WO6G`` is a six-hour accumulation ending at the observation time.  The
    importer stores this semantic explicitly and never invents an hourly rain
    distribution.  URLs and periods are configurable so another archive
    implementation can replace the official IMGW adapter.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    run_on_first_run: bool = True
    base_url: str = (
        "https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/"
        "dane_meteorologiczne/terminowe/synop"
    )
    header_url: str = (
        "https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/"
        "dane_meteorologiczne/terminowe/synop/s_t_nag%C5%82%C3%B3wek.csv"
    )
    lookback_months: int = Field(default=24, ge=1, le=240)
    start_year: int | None = Field(default=None, ge=1960, le=2200)
    end_year: int | None = Field(default=None, ge=1960, le=2200)
    max_files_per_run: int = Field(default=0, ge=0, le=1000)
    station_ids: list[str] = Field(default_factory=list)
    cache_dir: Path = Path("imgw-archive-cache")
    source_timezone: str = "UTC"
    precipitation_accumulation_period_hours: int = Field(default=6, ge=1, le=24)
    request_interval_seconds: float = Field(default=0.05, ge=0.0, le=10.0)
    temperature_code: str = "TEMP"
    humidity_code: str = "WLGW"
    pressure_codes: list[str] = Field(default_factory=lambda: ["HPOW", "HPON", "HPOD"])
    precipitation_code: str = "WO6G"
    wind_speed_code: str = "FWR"
    wind_direction_code: str = "KRWR"
    skip_unchanged_cached_files: bool = True

    @field_validator("base_url", "header_url")
    @classmethod
    def archive_https_only(cls, value: str) -> str:
        if not value.lower().startswith("https://"):
            raise ValueError("IMGW archive URLs must use HTTPS")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_year_range(self) -> "ImgwArchiveConfig":
        if self.start_year is not None and self.end_year is not None:
            if self.end_year < self.start_year:
                raise ValueError("imgw_archive.end_year cannot be smaller than start_year")
        return self



class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale_air_hours: int = 4
    stale_weather_hours: int = 4
    max_station_match_km: float = 80.0
    spike_absolute_pm10: float = 150.0
    spike_absolute_pm25: float = 100.0
    verify_tolerance_minutes: int = 90


class DataValidationConfig(BaseModel):
    """Tabular data contracts.

    Raw/operational data is reported rather than deleted. Curated model inputs and
    dashboard snapshots are blocking by default. ``require_pandera`` should be true
    in the customer production config and may be false in minimal diagnostic tests.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    require_pandera: bool = False
    lazy: bool = True
    operational_policy: Literal["report", "fail"] = "report"
    training_policy: Literal["report", "fail"] = "fail"
    snapshot_policy: Literal["report", "fail"] = "fail"
    max_failure_cases: int = Field(default=500, ge=1, le=10000)
    reports_dir: Path = Path("validation-reports")


class ObjectStorageConfig(BaseModel):
    """Generic object-storage settings.

    ``backend=spaces`` is an alias for the S3 implementation.  The same fields
    work with DigitalOcean Spaces, AWS S3, MinIO and other S3-compatible stores.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    backend: Literal["local", "memory", "s3", "spaces"] = "local"
    local_root: Path = Path("object-store")
    bucket: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    prefix: str = "smog-ai"
    access_key_env: str = "SPACES_ACCESS_KEY_ID"
    secret_key_env: str = "SPACES_SECRET_ACCESS_KEY"
    session_token_env: str | None = None
    verify_tls: bool = True
    addressing_style: Literal["virtual", "path", "auto"] = "virtual"
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0
    max_attempts: int = 5

    @model_validator(mode="after")
    def validate_remote(self) -> "ObjectStorageConfig":
        if self.backend in {"s3", "spaces"}:
            if not self.bucket:
                raise ValueError("object_storage.bucket is required for s3/spaces")
            if self.backend == "spaces" and not self.endpoint_url:
                if not self.region:
                    raise ValueError("object_storage.region or endpoint_url is required for DigitalOcean Spaces")
                self.endpoint_url = f"https://{self.region}.digitaloceanspaces.com"
        return self


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    operational_export_days: int = 730
    export_after_collection: bool = True
    export_training_frames_before_training: bool = True
    upload_models: bool = True


class DataFlowConfig(BaseModel):
    """Select how source data reaches local training.

    This is independent from ``object_storage.backend``:

    * ``direct_local`` reads model input directly from local SQLite;
    * ``object_store_roundtrip`` enforces SQLite -> ObjectStore -> local ML.
    """

    model_config = ConfigDict(extra="forbid")

    training_mode: Literal["direct_local", "object_store_roundtrip"] = (
        "object_store_roundtrip"
    )
    mirror_operational_to_object_store: bool = True
    history_cache_mode: Literal["local", "object_store", "hybrid"] = "local"
    history_cache_prefix: str = "source-cache/gios-history"

    @field_validator("history_cache_prefix")
    @classmethod
    def normalize_history_cache_prefix(cls, value: str) -> str:
        cleaned = value.strip().strip("/")
        if not cleaned:
            raise ValueError("data_flow.history_cache_prefix cannot be empty")
        return cleaned



class TrainingSnapshotConfig(BaseModel):
    """Immutable SQLite dataset used by local model training.

    The live ingestion database may continue receiving data while SQLite's
    Online Backup API creates a transactionally consistent training snapshot.
    Models are registered and activated in the live runtime database.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    root_dir: Path = Path("training-datasets")
    default_selector: Literal["auto", "latest", "live"] = "auto"
    reuse_latest_minutes: int = Field(default=0, ge=0, le=10080)
    mirror_manifest_to_object_storage: bool = True
    backup_pages: int = Field(default=4096, ge=64, le=131072)
    backup_sleep_seconds: float = Field(default=0.01, ge=0.0, le=5.0)
    backup_stall_seconds: float = Field(default=180.0, ge=10.0, le=3600.0)
    backup_max_restarts: int = Field(default=3, ge=0, le=100)
    retain_quick: int = Field(default=8, ge=1, le=100)
    retain_full: int = Field(default=4, ge=1, le=100)


class AirParameterConfig(BaseModel):
    """One canonical GIOŚ air parameter and its independent roles.

    Collection, historical backfill, forecasting and map publication are
    deliberately separate.  Enabling collection never silently creates a new
    model target.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    canonical_unit: str = "µg/m³"
    cadence_hours: int = Field(default=1, ge=1, le=744)
    collect_current: bool = False
    historical_backfill: bool = False
    forecast_target: bool = False
    auxiliary_feature: bool = False
    spatial_surface: bool = False
    allow_negative: bool = False
    valid_min: float | None = 0.0
    valid_max: float | None = None
    exceedance_threshold: float | None = None
    spike_absolute: float | None = None
    annual_api_indicator: str | None = None
    prepared_archive_tokens: list[str] = Field(default_factory=list)
    algorithms: list[str] = Field(
        default_factory=lambda: [
            "persistence",
            "historical_mean",
            "ridge",
            "polynomial_ridge",
            "hist_gradient_boosting",
            "mlp",
        ]
    )

    @field_validator("aliases", "prepared_archive_tokens", "algorithms")
    @classmethod
    def clean_string_lists(cls, value: list[str]) -> list[str]:
        output: list[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned and cleaned not in output:
                output.append(cleaned)
        return output

    @model_validator(mode="after")
    def validate_parameter(self) -> "AirParameterConfig":
        if self.valid_min is not None and self.valid_max is not None:
            if self.valid_max <= self.valid_min:
                raise ValueError("air parameter valid_max must exceed valid_min")
        if self.spike_absolute is not None and self.spike_absolute <= 0:
            raise ValueError("air parameter spike_absolute must be positive")
        if self.forecast_target and not self.algorithms:
            raise ValueError("forecast air parameter requires at least one algorithm")
        return self


def _default_air_parameter_definitions() -> dict[str, AirParameterConfig]:
    common_candidates = [
        "persistence",
        "historical_mean",
        "ridge",
        "polynomial_ridge",
        "hist_gradient_boosting",
        "mlp",
    ]
    return {
        "PM10": AirParameterConfig(
            display_name="Pył zawieszony PM10",
            aliases=["PM10", "PYŁ ZAWIESZONY PM10", "PYL ZAWIESZONY PM10"],
            collect_current=True,
            historical_backfill=True,
            forecast_target=True,
            auxiliary_feature=True,
            spatial_surface=True,
            exceedance_threshold=50.0,
            spike_absolute=150.0,
            annual_api_indicator="PM10",
            prepared_archive_tokens=["PM10"],
            algorithms=common_candidates,
        ),
        "PM2.5": AirParameterConfig(
            display_name="Pył zawieszony PM2.5",
            aliases=["PM2.5", "PM25", "PM2,5", "PYŁ ZAWIESZONY PM2.5"],
            collect_current=True,
            historical_backfill=True,
            forecast_target=True,
            auxiliary_feature=True,
            spatial_surface=True,
            exceedance_threshold=25.0,
            spike_absolute=100.0,
            annual_api_indicator="PM2.5",
            prepared_archive_tokens=["PM25", "PM2.5", "PM2,5"],
            algorithms=common_candidates,
        ),
        "NO2": AirParameterConfig(
            display_name="Dwutlenek azotu",
            aliases=["NO2", "DWUTLENEK AZOTU"],
            annual_api_indicator="NO2",
            prepared_archive_tokens=["NO2"],
            algorithms=common_candidates,
        ),
        "SO2": AirParameterConfig(
            display_name="Dwutlenek siarki",
            aliases=["SO2", "DWUTLENEK SIARKI"],
            annual_api_indicator="SO2",
            prepared_archive_tokens=["SO2"],
            algorithms=common_candidates,
        ),
        "O3": AirParameterConfig(
            display_name="Ozon",
            aliases=["O3", "OZON"],
            annual_api_indicator="O3",
            prepared_archive_tokens=["O3"],
            algorithms=common_candidates,
        ),
        "CO": AirParameterConfig(
            display_name="Tlenek węgla",
            aliases=["CO", "TLENEK WĘGLA", "TLENEK WEGLA"],
            canonical_unit="mg/m³",
            annual_api_indicator="CO",
            prepared_archive_tokens=["CO"],
            algorithms=common_candidates,
        ),
        "C6H6": AirParameterConfig(
            display_name="Benzen",
            aliases=["C6H6", "BENZEN"],
            annual_api_indicator="C6H6",
            prepared_archive_tokens=["C6H6", "BENZEN"],
            algorithms=common_candidates,
        ),
        "NO": AirParameterConfig(
            display_name="Tlenek azotu",
            aliases=["NO", "TLENEK AZOTU"],
            annual_api_indicator="NO",
            prepared_archive_tokens=["NO"],
            algorithms=common_candidates,
        ),
        "NOX": AirParameterConfig(
            display_name="Tlenki azotu",
            aliases=["NOX", "NOx", "TLENKI AZOTU"],
            annual_api_indicator="NOx",
            prepared_archive_tokens=["NOX", "NOx"],
            algorithms=common_candidates,
        ),
    }


class AirParametersConfig(BaseModel):
    """Central parameter registry shared by collectors, backfill and ML."""

    model_config = ConfigDict(extra="forbid")

    unknown_sensor_policy: Literal["metadata_only", "collect", "ignore"] = (
        "metadata_only"
    )
    parameters: dict[str, AirParameterConfig] = Field(
        default_factory=_default_air_parameter_definitions
    )

    @field_validator("parameters")
    @classmethod
    def validate_parameter_codes(
        cls, value: dict[str, AirParameterConfig]
    ) -> dict[str, AirParameterConfig]:
        if not value:
            raise ValueError("air_parameters.parameters cannot be empty")
        cleaned: dict[str, AirParameterConfig] = {}
        for raw_code, row in value.items():
            code = str(raw_code).strip().upper().replace(",", ".")
            if not code:
                raise ValueError("air parameter code cannot be empty")
            if code in cleaned:
                raise ValueError(f"duplicate air parameter code: {code}")
            cleaned[code] = row
        return cleaned


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizons_hours: list[int] = Field(default_factory=lambda: [6, 12, 24])
    parameters: list[str] = Field(default_factory=lambda: ["PM10", "PM2.5"])
    minimum_training_rows: int = 200
    validation_fraction: float = 0.2
    minimum_mae_improvement_fraction: float = 0.01
    algorithms: list[str] = Field(
        default_factory=lambda: [
            "persistence",
            "historical_mean",
            "hist_gradient_boosting",
            "mlp",
        ]
    )
    random_state: int = 42
    max_training_days: int = 730
    input_source: Literal["database", "object_store"] = "database"
    allow_database_fallback: bool = False

    @field_validator("horizons_hours")
    @classmethod
    def horizons_positive_unique(cls, value: list[int]) -> list[int]:
        if not value or any(item <= 0 for item in value):
            raise ValueError("Forecast horizons must be positive")
        return sorted(set(value))


class ExternalModelFactoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    import_string: str
    enabled: bool = True


class ModelPlatformConfig(BaseModel):
    """Open estimator platform independent of a concrete ML library.

    Built-in methods are registered in ``smog_ai.modeling``.  External methods
    may be loaded from plugin modules exposing ``register_models(registry)`` or
    from Python entry points.  This keeps training/prediction independent from
    scikit-learn and makes XGBoost, LightGBM, CatBoost, PyTorch or a proprietary
    estimator replaceable without changing the domain pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    entry_point_group: str = "smog_ai.model_providers"
    discover_entry_points: bool = True
    plugin_modules: list[str] = Field(default_factory=list)
    external_factories: list[ExternalModelFactoryConfig] = Field(default_factory=list)
    method_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PrecipitationForecastConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurrence_threshold_mm: float = Field(default=0.1, ge=0.0, le=20.0)
    accumulation_period_hours: int = Field(default=6, ge=1, le=24)
    provider: str = "hurdle_hist_gradient_boosting"
    minimum_positive_rows: int = Field(default=20, ge=5)

    # A hurdle model must beat simple baselines for both occurrence and amount.
    minimum_mae_improvement_vs_persistence: float = Field(
        default=0.01, ge=-1.0, le=1.0
    )
    minimum_brier_skill_vs_climatology: float = Field(
        default=0.0, ge=-1.0, le=1.0
    )
    minimum_brier_skill_vs_persistence: float = Field(
        default=0.0, ge=-1.0, le=1.0
    )
    minimum_roc_auc: float = Field(default=0.60, ge=0.0, le=1.0)
    maximum_absolute_bias_mm: float = Field(default=1.0, ge=0.0, le=100.0)
    mark_experimental_on_failure: bool = True
    # Local serving may keep an experimental h1-h60 model active so charts can
    # be tested. Publication gates must still reject it until quality passes.
    activate_experimental_locally: bool = True



class HourlyTrainingProfileConfig(BaseModel):
    """A bounded training recipe independent from the estimator provider.

    Profiles control data volume, horizon expansion, validation cost and the
    set of candidate methods.  The complete raw history remains in SQLite and
    ObjectStore; only the materialised training sample is bounded.
    """

    model_config = ConfigDict(extra="forbid")

    maximum_training_days_by_target: dict[str, int] = Field(
        default_factory=lambda: {
            "PM10": 365,
            "PM2.5": 365,
            "temperature_c": 730,
            "precipitation_mm": 730,
        }
    )
    maximum_rows_per_target: int = Field(default=250_000, ge=10_000, le=20_000_000)
    validation_max_rows: int = Field(default=60_000, ge=1_000, le=5_000_000)
    always_keep_recent_days: int = Field(default=90, ge=1, le=3650)
    horizon_bucket_edges: list[int] = Field(default_factory=lambda: [6, 12, 24, 48, 60])
    samples_per_horizon_bucket: int = Field(default=2, ge=1, le=24)
    cross_fit_folds: int = Field(default=2, ge=2, le=12)
    algorithms: dict[str, list[str]] = Field(default_factory=dict)
    fit_quantiles: bool = False
    max_wall_time_seconds: int = Field(default=1800, ge=60, le=172800)

    @field_validator("maximum_training_days_by_target")
    @classmethod
    def validate_days_by_target(cls, value: dict[str, int]) -> dict[str, int]:
        cleaned = {str(key): int(days) for key, days in value.items()}
        if not cleaned or any(days < 7 or days > 7300 for days in cleaned.values()):
            raise ValueError(
                "maximum_training_days_by_target values must be in [7, 7300]"
            )
        return cleaned

    @field_validator("horizon_bucket_edges")
    @classmethod
    def validate_horizon_edges(cls, value: list[int]) -> list[int]:
        cleaned = sorted(set(int(item) for item in value))
        if not cleaned or any(item <= 0 or item > 168 for item in cleaned):
            raise ValueError("horizon_bucket_edges must contain values in [1, 168]")
        return cleaned


class HourlyTrainingPolicyConfig(BaseModel):
    """Policy Bridge selecting a bounded and reproducible training sample."""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "full_history",
        "rolling_window",
        "bounded_rolling_stratified",
    ] = "bounded_rolling_stratified"
    default_profile: Literal["quick", "full"] = "quick"
    rare_event_quantile: float = Field(default=0.90, ge=0.50, le=0.999)
    recency_half_life_days: float = Field(default=180.0, ge=1.0, le=3650.0)
    quick: HourlyTrainingProfileConfig = Field(
        default_factory=lambda: HourlyTrainingProfileConfig(
            maximum_training_days_by_target={
                "PM10": 365,
                "PM2.5": 365,
                "temperature_c": 730,
                "precipitation_mm": 730,
            },
            maximum_rows_per_target=250_000,
            validation_max_rows=60_000,
            always_keep_recent_days=90,
            horizon_bucket_edges=[6, 12, 24, 48, 60],
            samples_per_horizon_bucket=2,
            cross_fit_folds=2,
            algorithms={
                "PM10": ["persistence", "ridge", "hist_gradient_boosting"],
                "PM2.5": ["persistence", "ridge", "hist_gradient_boosting"],
                "temperature_c": ["persistence", "ridge"],
                "precipitation_mm": ["hurdle_hist_gradient_boosting"],
            },
            fit_quantiles=False,
            max_wall_time_seconds=1800,
        )
    )
    full: HourlyTrainingProfileConfig = Field(
        default_factory=lambda: HourlyTrainingProfileConfig(
            maximum_training_days_by_target={
                "PM10": 730,
                "PM2.5": 730,
                "temperature_c": 1095,
                "precipitation_mm": 1095,
            },
            maximum_rows_per_target=600_000,
            validation_max_rows=120_000,
            always_keep_recent_days=120,
            horizon_bucket_edges=[6, 12, 24, 48, 60],
            samples_per_horizon_bucket=3,
            cross_fit_folds=4,
            algorithms={},
            fit_quantiles=True,
            max_wall_time_seconds=7200,
        )
    )


class IncrementalResidualConfig(BaseModel):
    """Fast residual correction fitted from already verified forecasts."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    minimum_verified_rows: int = Field(default=500, ge=20, le=5_000_000)
    maximum_rows_per_update: int = Field(default=50_000, ge=100, le=2_000_000)
    lookback_days: int = Field(default=120, ge=7, le=3650)
    minimum_mae_improvement_fraction: float = Field(default=0.01, ge=0.0, le=1.0)
    alpha: float = Field(default=0.0001, gt=0.0, le=10.0)
    eta0: float = Field(default=0.01, gt=0.0, le=10.0)
    random_state: int = 42


class HourlyDriftConfig(BaseModel):
    """Lightweight model-health gate based on verified forecast errors."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    recent_window_rows: int = Field(default=500, ge=20, le=1_000_000)
    reference_window_rows: int = Field(default=1500, ge=20, le=5_000_000)
    minimum_verified_rows: int = Field(default=250, ge=20, le=5_000_000)
    mae_relative_increase_threshold: float = Field(default=0.20, ge=0.0, le=10.0)
    bias_absolute_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "PM10": 8.0,
            "PM2.5": 5.0,
            "temperature_c": 2.5,
            "precipitation_mm": 2.0,
        }
    )


class HourlyForecastingConfig(BaseModel):
    """Exact-target-time, horizon-conditioned multi-target forecasting."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Legacy model-horizon bounds.  They remain accepted for backwards
    # compatibility with existing config.yaml files.  HF20 introduces an
    # explicit serving horizon and an optional wider model-horizon ceiling.
    minimum_horizon_hours: int = Field(default=1, ge=1, le=168)
    maximum_horizon_hours: int = Field(default=48, ge=1, le=168)
    step_hours: int = Field(default=1, ge=1, le=24)
    serving_horizon_hours: int | None = Field(default=None, ge=1, le=168)
    maximum_source_delay_hours: int = Field(default=12, ge=0, le=72)
    maximum_model_horizon_hours: int | None = Field(default=None, ge=1, le=240)
    targets: list[str] = Field(
        default_factory=lambda: ["PM10", "PM2.5", "temperature_c", "precipitation_mm"]
    )
    spatial_targets: list[str] = Field(
        default_factory=lambda: [
            "PM10",
            "PM2.5",
            "temperature_c",
            "precipitation_probability",
            "precipitation_mm",
        ]
    )
    default_air_target_algorithms: list[str] = Field(
        default_factory=lambda: [
            "persistence",
            "historical_mean",
            "ridge",
            "polynomial_ridge",
            "hist_gradient_boosting",
            "mlp",
        ]
    )
    target_algorithms: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "PM10": [
                "persistence",
                "historical_mean",
                "ridge",
                "polynomial_ridge",
                "hist_gradient_boosting",
                "mlp",
            ],
            "PM2.5": [
                "persistence",
                "historical_mean",
                "ridge",
                "polynomial_ridge",
                "hist_gradient_boosting",
                "mlp",
            ],
            "temperature_c": [
                "persistence",
                "historical_mean",
                "ridge",
                "polynomial_ridge",
                "hist_gradient_boosting",
                "mlp",
            ],
            "precipitation_mm": ["hurdle_hist_gradient_boosting"],
        }
    )
    quantile_method: str = "hist_gradient_boosting_quantile"
    quantiles: list[float] = Field(default_factory=lambda: [0.1, 0.5, 0.9])
    minimum_training_rows: int = Field(default=500, ge=10)
    minimum_unique_origin_times: int = Field(default=72, ge=2)
    validation_fraction: float = Field(default=0.2, gt=0.0, lt=0.5)
    minimum_mae_improvement_fraction: float = Field(default=0.01, ge=0.0, le=1.0)
    cross_fit_folds: int = Field(default=4, ge=2, le=12)
    maximum_training_days: int = Field(default=1095, ge=7, le=7300)
    maximum_training_rows_per_target: int = Field(
        default=1_000_000, ge=10_000, le=20_000_000
    )
    random_state: int = 42
    exact_target_time_required: bool = True
    temporal_interpolation: Literal["none", "linear", "pchip"] = "pchip"
    allow_temporal_extrapolation: bool = False
    use_predicted_weather_for_pm: bool = True
    training_policy: HourlyTrainingPolicyConfig = Field(
        default_factory=HourlyTrainingPolicyConfig
    )
    incremental_residual: IncrementalResidualConfig = Field(
        default_factory=IncrementalResidualConfig
    )
    drift: HourlyDriftConfig = Field(default_factory=HourlyDriftConfig)
    precipitation: PrecipitationForecastConfig = Field(default_factory=PrecipitationForecastConfig)

    @model_validator(mode="after")
    def validate_hourly_settings(self) -> "HourlyForecastingConfig":
        if self.minimum_horizon_hours > self.maximum_horizon_hours:
            raise ValueError("minimum_horizon_hours cannot exceed maximum_horizon_hours")
        if self.model_horizon_maximum < self.minimum_horizon_hours:
            raise ValueError(
                "maximum_model_horizon_hours cannot be below minimum_horizon_hours"
            )
        time_contract_configured = bool(
            {"serving_horizon_hours", "maximum_model_horizon_hours"}
            & self.model_fields_set
        )
        if time_contract_configured:
            # The serving grid starts at the next full hour.  With a source
            # exactly ``maximum_source_delay_hours`` old, model lead 1 is one
            # hour farther than the source age; therefore 48 serving hours and
            # a 12-hour SLA require model horizons through h60.
            required = (
                self.serving_horizon_count
                + self.maximum_source_delay_hours
            )
            if self.model_horizon_maximum < required:
                # Existing 1.7.0 runtime YAML files serialise legacy 48-hour
                # fields. Upgrade them in memory rather than rejecting a valid
                # configuration after HF20 introduces the serving/model split.
                self.maximum_model_horizon_hours = required
        if not self.targets:
            raise ValueError("hourly_forecasting.targets cannot be empty")
        cleaned = sorted(set(float(value) for value in self.quantiles))
        if any(value <= 0 or value >= 1 for value in cleaned):
            raise ValueError("Quantiles must be strictly between 0 and 1")
        self.quantiles = cleaned
        return self

    @property
    def serving_horizon_count(self) -> int:
        return int(self.serving_horizon_hours or self.maximum_horizon_hours)

    @property
    def model_horizon_maximum(self) -> int:
        return int(
            self.maximum_model_horizon_hours or self.maximum_horizon_hours
        )

    @property
    def model_horizons_hours(self) -> list[int]:
        return list(
            range(
                self.minimum_horizon_hours,
                self.model_horizon_maximum + 1,
                self.step_hours,
            )
        )

    @property
    def serving_horizons_hours(self) -> list[int]:
        return list(range(1, self.serving_horizon_count + 1))

    @property
    def horizons_hours(self) -> list[int]:
        """Backward-compatible alias for model-training horizons."""

        return self.model_horizons_hours


class DocumentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    publish_to_object_storage: bool = True
    platform_title: str = "Dokumentacja Smog AI"
    processing_markdown: Path = Path("docs/platform/TECHNICAL_PROCESSING_PL.md")
    processing_latex: Path = Path("docs/latex/DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex")
    mathematics_markdown: Path = Path("docs/platform/MATHEMATICAL_MODEL_PL.md")
    model_plugin_markdown: Path = Path("docs/platform/MODEL_PLUGIN_GUIDE_PL.md")
    mathematics_latex: Path = Path("docs/latex/DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex")
    hf20_markdown: Path = Path("docs/platform/HF20_TIME_CONTRACT_MLOPS_PL.md")
    hf20_latex: Path = Path("docs/latex/DODATEK_TECHNICZNY_HF20_TIME_CONTRACT_MLOPS_PL.tex")


class SpatialConfig(BaseModel):
    """Configuration for locally precomputed Poland-wide forecast surfaces.

    Surface generation is a local pipeline concern. The public FastAPI and
    Streamlit components never call a trained model. They may evaluate the same
    deterministic IDW bridge at an exact user point using station forecasts
    already published through the configured ObjectStore bridge.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    algorithm: Literal["idw", "rbf"] = "idw"
    boundary_geojson: Path = Path(__file__).parent / "resources" / "poland_boundary.geojson"
    places_csv: Path = Path(__file__).parent / "resources" / "polish_places.csv"
    projected_crs: str = "EPSG:2180"
    grid_resolution_km: float = Field(default=8.0, ge=2.0, le=50.0)
    idw_power: float = Field(default=2.0, ge=0.5, le=8.0)
    idw_distance_smoothing_m: float = Field(default=100.0, ge=0.001, le=50_000.0)
    exact_station_threshold_m: float = Field(default=10.0, ge=0.0, le=10_000.0)
    nearest_stations: int = Field(default=8, ge=1, le=64)
    minimum_stations: int = Field(default=3, ge=1, le=64)
    maximum_distance_km: float = Field(default=220.0, ge=10.0, le=1000.0)
    confidence_distance_km: float = Field(default=85.0, ge=1.0, le=500.0)
    confidence_minimum: float = Field(default=0.08, ge=0.0, le=1.0)
    publish_station_points: bool = True
    publish_boundary: bool = True
    local_cache_dir: Path = Path("spatial")
    max_surface_age_hours: int = Field(default=36, ge=1, le=720)

    @model_validator(mode="after")
    def validate_neighbour_counts(self) -> "SpatialConfig":
        if self.minimum_stations > self.nearest_stations:
            raise ValueError("spatial.minimum_stations cannot exceed nearest_stations")
        return self


class PublicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    transport: Literal["object_store", "http", "both"] = "object_store"
    api_url: str = "https://example.org/api/v1"
    api_token_env: str = "PUBLISH_API_TOKEN"
    timeout_seconds: float = 45.0
    max_attempts: int = 12
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 21600
    dead_letter_after_attempts: int = 12
    snapshot_history_days: int = 14
    gzip_compresslevel: int = 6

    def token(self) -> str | None:
        return os.getenv(self.api_token_env)


class NLPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["rule_based", "openai_compatible"] = "rule_based"
    model: str = "gpt-5.4-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LLM_API_KEY"
    timeout_seconds: float = 30.0
    max_retries: int = 2
    temperature: float = 0.0
    allow_rule_based_fallback: bool = True
    default_country: str = "Polska"
    default_timezone: str = "Europe/Warsaw"

    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)




class MLflowConfig(BaseModel):
    """Optional model experiment tracking and local comparison export.

    Tracking is disabled by default and remains local unless an explicit
    tracking URI and publication policy are configured.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strict: bool = False
    tracking_uri: str = ""
    experiment_name: str = "smog-ai-hourly"
    registry_enabled: bool = False
    registered_model_prefix: str = "smog-ai-hourly"
    log_model_artifacts: bool = Field(
        default=False,
        validation_alias=AliasChoices("log_model_artifacts", "log_model_artifact"),
    )
    maximum_runs_per_target: int = Field(default=100, ge=1, le=10_000)
    local_artifact_dir: Path = Path("mlflow")
    comparison_path: Path = Path("reports/mlflow/model-comparison.json")
    publish_comparison_to_object_storage: bool = False
    ui_url: str | None = None



class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["none", "langfuse"] = "none"
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"
    base_url_env: str = "LANGFUSE_BASE_URL"
    environment: str = "development"
    release: str = "1.7.0"
    prompt_template_version: str = "air-query-v1"
    feedback_enabled: bool = True
    local_feedback_path: Path = Path("feedback/prompt-feedback.jsonl")
    flush_on_request: bool = False


class LockingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = 3600
    heartbeat_seconds: int = 30
    windows_mutex_prefix: str = r"Global\SmogAI"


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_free_disk_gb: float = 2.0
    max_last_collection_age_hours: int = 6
    max_last_forecast_age_hours: int = 8
    publication_probe_enabled: bool = True
    source_api_probe_enabled: bool = False
    object_storage_probe_enabled: bool = True


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_keep: int = 7
    weekly_keep: int = 8
    monthly_keep: int = 12


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "test", "production"] = "development"
    display_timezone: str = "Europe/Warsaw"
    source_host_id: str = "local-development"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    imgw_archive: ImgwArchiveConfig = Field(default_factory=ImgwArchiveConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    data_validation: DataValidationConfig = Field(default_factory=DataValidationConfig)
    object_storage: ObjectStorageConfig = Field(default_factory=ObjectStorageConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    data_flow: DataFlowConfig = Field(default_factory=DataFlowConfig)
    training_snapshot: TrainingSnapshotConfig = Field(
        default_factory=TrainingSnapshotConfig
    )
    air_parameters: AirParametersConfig = Field(default_factory=AirParametersConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    model_platform: ModelPlatformConfig = Field(default_factory=ModelPlatformConfig)
    hourly_forecasting: HourlyForecastingConfig = Field(default_factory=HourlyForecastingConfig)
    documentation: DocumentationConfig = Field(default_factory=DocumentationConfig)
    spatial: SpatialConfig = Field(default_factory=SpatialConfig)
    publication: PublicationConfig = Field(default_factory=PublicationConfig)
    nlp: NLPConfig = Field(default_factory=NLPConfig)
    mlflow: MLflowConfig = Field(default_factory=MLflowConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    locking: LockingConfig = Field(default_factory=LockingConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)

    @model_validator(mode="after")
    def validate_air_parameter_roles(self) -> "AppConfig":
        # Local import avoids a config -> registry import cycle at module load.
        from smog_ai.air_parameters import (
            WEATHER_DERIVED_TARGETS,
            WEATHER_TARGETS,
            create_air_parameter_registry,
        )

        registry = create_air_parameter_registry(self)

        canonical_targets: list[str] = []
        for raw_target in self.hourly_forecasting.targets:
            target = str(raw_target).strip()
            if target in WEATHER_TARGETS:
                canonical = target
            else:
                canonical = registry.resolve(target)
                definition = registry.get(canonical)
                if definition is None:
                    raise ValueError(
                        f"Hourly target {target!r} is not a configured air or weather parameter"
                    )
                if not definition.forecast_target:
                    raise ValueError(
                        f"Air parameter {canonical!r} is listed as an hourly target but "
                        "air_parameters.parameters.<code>.forecast_target is false"
                    )
                self.hourly_forecasting.target_algorithms.setdefault(
                    canonical,
                    list(
                        definition.algorithms
                        or self.hourly_forecasting.default_air_target_algorithms
                    ),
                )
            if canonical not in canonical_targets:
                canonical_targets.append(canonical)

        for target in canonical_targets:
            algorithms = self.hourly_forecasting.target_algorithms.get(target, [])
            if not algorithms:
                raise ValueError(
                    f"No model candidates configured for hourly target {target!r}"
                )
        self.hourly_forecasting.targets = canonical_targets

        canonical_spatial: list[str] = []
        for raw_target in self.hourly_forecasting.spatial_targets:
            target = str(raw_target).strip()
            if target in WEATHER_TARGETS or target in WEATHER_DERIVED_TARGETS:
                canonical = target
            else:
                canonical = registry.resolve(target)
                definition = registry.get(canonical)
                if definition is None:
                    raise ValueError(
                        f"Spatial target {target!r} is not a configured parameter"
                    )
                if not definition.spatial_surface:
                    raise ValueError(
                        f"Air parameter {canonical!r} is listed as a spatial target but "
                        "spatial_surface is false"
                    )
            if canonical not in canonical_spatial:
                canonical_spatial.append(canonical)
        self.hourly_forecasting.spatial_targets = canonical_spatial
        return self

    @property
    def database_url(self) -> str:
        # Test configurations must never inherit the production database path
        # from the caller's shell.  This guard is intentionally enforced in the
        # application layer (not only in pytest fixtures), because a developer
        # may run tests from a terminal that has loaded C:\ProgramData\SmogAI\smog-ai.env.
        if self.environment == "test":
            return sqlite_url_for_path(self.paths.database_path)

        configured = os.getenv("SMOG_AI_DATABASE_URL")
        if configured:
            return normalize_sqlite_url(configured)
        return sqlite_url_for_path(self.paths.database_path)

    def ensure_directories(self) -> None:
        self.paths.ensure()
        runtime_root = self.paths.data_dir.parent.resolve()
        if not self.data_validation.reports_dir.is_absolute():
            self.data_validation.reports_dir = (runtime_root / self.data_validation.reports_dir).resolve()
        self.data_validation.reports_dir.mkdir(parents=True, exist_ok=True)
        if not self.spatial.local_cache_dir.is_absolute():
            self.spatial.local_cache_dir = (runtime_root / self.spatial.local_cache_dir).resolve()
        self.spatial.local_cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.imgw_archive.cache_dir.is_absolute():
            self.imgw_archive.cache_dir = (runtime_root / self.imgw_archive.cache_dir).resolve()
        self.imgw_archive.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.training_snapshot.root_dir.is_absolute():
            self.training_snapshot.root_dir = (
                runtime_root / self.training_snapshot.root_dir
            ).resolve()
        self.training_snapshot.root_dir.mkdir(parents=True, exist_ok=True)
        if not self.mlflow.local_artifact_dir.is_absolute():
            self.mlflow.local_artifact_dir = (
                runtime_root / self.mlflow.local_artifact_dir
            ).resolve()
        self.mlflow.local_artifact_dir.mkdir(parents=True, exist_ok=True)
        if not self.mlflow.comparison_path.is_absolute():
            self.mlflow.comparison_path = (
                runtime_root / self.mlflow.comparison_path
            ).resolve()
        self.mlflow.comparison_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.observability.local_feedback_path.is_absolute():
            self.observability.local_feedback_path = (
                runtime_root / self.observability.local_feedback_path
            ).resolve()
        self.observability.local_feedback_path.parent.mkdir(parents=True, exist_ok=True)
        for field_name in (
            "processing_markdown",
            "processing_latex",
            "mathematics_markdown",
            "model_plugin_markdown",
            "mathematics_latex",
            "hf20_markdown",
            "hf20_latex",
        ):
            candidate = getattr(self.documentation, field_name)
            if not candidate.is_absolute():
                setattr(self.documentation, field_name, (project_root() / candidate).resolve())

        if self.object_storage.backend == "local":
            if not self.object_storage.local_root.is_absolute():
                self.object_storage.local_root = (runtime_root / self.object_storage.local_root).resolve()
            self.object_storage.local_root.mkdir(parents=True, exist_ok=True)


def sqlite_url_for_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    return f"sqlite:///{resolved.as_posix()}"


def normalize_sqlite_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("\\", "/")
    return url


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def project_root() -> Path:
    """Resolve the checkout root without assuming a fixed installation directory."""
    explicit = os.getenv("SMOG_AI_PROJECT_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    # smog_ai/config.py -> smog_ai -> repository/package root
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    explicit = os.getenv("SMOG_AI_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "SmogAI" / "config.yaml"
    return Path("/etc/smog-ai/config.yaml")


def default_env_path() -> Path:
    explicit = os.getenv("SMOG_AI_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "SmogAI" / "smog-ai.env"
    return Path("/etc/smog-ai/smog-ai.env")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _runtime_base(config_path: Path) -> Path:
    explicit = os.getenv("SMOG_AI_DATA_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return config_path.parent.resolve()


def _resolve_paths(payload: dict[str, Any], config_path: Path) -> dict[str, Any]:
    base = _runtime_base(config_path)
    path_section = payload.setdefault("paths", {})
    for key in (
        "data_dir",
        "database_path",
        "models_dir",
        "snapshots_dir",
        "logs_dir",
        "backups_dir",
        "temp_dir",
    ):
        if key in path_section:
            candidate = Path(str(path_section[key])).expanduser()
            if not candidate.is_absolute():
                path_section[key] = str((base / candidate).resolve())
    if "imgw_metadata_csv" in path_section:
        candidate = Path(str(path_section["imgw_metadata_csv"])).expanduser()
        if not candidate.is_absolute():
            path_section["imgw_metadata_csv"] = str((project_root() / candidate).resolve())
    validation = payload.setdefault("data_validation", {})
    if "reports_dir" in validation:
        candidate = Path(str(validation["reports_dir"])).expanduser()
        if not candidate.is_absolute():
            validation["reports_dir"] = str((base / candidate).resolve())
    training_snapshot = payload.setdefault("training_snapshot", {})
    if "root_dir" in training_snapshot:
        candidate = Path(str(training_snapshot["root_dir"])).expanduser()
        if not candidate.is_absolute():
            training_snapshot["root_dir"] = str((base / candidate).resolve())
    mlflow = payload.setdefault("mlflow", {})
    for key in ("local_artifact_dir", "comparison_path"):
        if key in mlflow:
            candidate = Path(str(mlflow[key])).expanduser()
            if not candidate.is_absolute():
                mlflow[key] = str((base / candidate).resolve())
    observability = payload.setdefault("observability", {})
    if "local_feedback_path" in observability:
        candidate = Path(str(observability["local_feedback_path"])).expanduser()
        if not candidate.is_absolute():
            observability["local_feedback_path"] = str((base / candidate).resolve())
    storage = payload.setdefault("object_storage", {})
    if "local_root" in storage:
        candidate = Path(str(storage["local_root"])).expanduser()
        if not candidate.is_absolute():
            storage["local_root"] = str((base / candidate).resolve())
    documentation = payload.setdefault("documentation", {})
    for key in (
        "processing_markdown",
        "processing_latex",
        "mathematics_markdown",
        "model_plugin_markdown",
        "mathematics_latex",
        "hf20_markdown",
        "hf20_latex",
    ):
        if key in documentation:
            candidate = Path(str(documentation[key])).expanduser()
            if not candidate.is_absolute():
                documentation[key] = str((project_root() / candidate).resolve())
    spatial = payload.setdefault("spatial", {})
    for key in ("boundary_geojson", "places_csv"):
        if key in spatial:
            candidate = Path(str(spatial[key])).expanduser()
            if not candidate.is_absolute():
                spatial[key] = str((project_root() / candidate).resolve())
    if "local_cache_dir" in spatial:
        candidate = Path(str(spatial["local_cache_dir"])).expanduser()
        if not candidate.is_absolute():
            spatial["local_cache_dir"] = str((base / candidate).resolve())
    return payload


def _apply_environment_overrides(payload: dict[str, Any]) -> None:
    mapping: list[tuple[str, tuple[str, ...]]] = [
        ("SMOG_AI_ENV", ("environment",)),
        ("SMOG_AI_SOURCE_HOST_ID", ("source_host_id",)),
        ("DISPLAY_TIMEZONE", ("display_timezone",)),
        ("PUBLISH_API_URL", ("publication", "api_url")),
        ("SMOG_AI_PUBLICATION_TRANSPORT", ("publication", "transport")),
        ("SMOG_AI_REQUIRE_PANDERA", ("data_validation", "require_pandera")),
        ("SMOG_AI_OBJECT_STORE_BACKEND", ("object_storage", "backend")),
        ("SMOG_AI_OBJECT_STORE_BUCKET", ("object_storage", "bucket")),
        ("SMOG_AI_OBJECT_STORE_ENDPOINT", ("object_storage", "endpoint_url")),
        ("SMOG_AI_OBJECT_STORE_REGION", ("object_storage", "region")),
        ("SMOG_AI_OBJECT_STORE_PREFIX", ("object_storage", "prefix")),
        ("SMOG_AI_OBJECT_STORE_LOCAL_ROOT", ("object_storage", "local_root")),
        ("SMOG_AI_DATA_FLOW_MODE", ("data_flow", "training_mode")),
        (
            "SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL",
            ("data_flow", "mirror_operational_to_object_store"),
        ),
        (
            "SMOG_AI_GIOS_HISTORY_CACHE_MODE",
            ("data_flow", "history_cache_mode"),
        ),
        (
            "SMOG_AI_GIOS_HISTORY_CACHE_PREFIX",
            ("data_flow", "history_cache_prefix"),
        ),
        (
            "SMOG_AI_TRAINING_SNAPSHOT_ENABLED",
            ("training_snapshot", "enabled"),
        ),
        (
            "SMOG_AI_TRAINING_SNAPSHOT_ROOT",
            ("training_snapshot", "root_dir"),
        ),
        (
            "SMOG_AI_TRAINING_SNAPSHOT_SELECTOR",
            ("training_snapshot", "default_selector"),
        ),
        (
            "SMOG_AI_TRAINING_SNAPSHOT_REUSE_MINUTES",
            ("training_snapshot", "reuse_latest_minutes"),
        ),
        (
            "SMOG_AI_TRAINING_SNAPSHOT_MIRROR_MANIFEST",
            ("training_snapshot", "mirror_manifest_to_object_storage"),
        ),
        ("SMOG_AI_TRAINING_INPUT_SOURCE", ("training", "input_source")),
        ("SMOG_AI_HOURLY_FORECASTING_ENABLED", ("hourly_forecasting", "enabled")),
        ("SMOG_AI_HOURLY_MAX_HORIZON", ("hourly_forecasting", "maximum_horizon_hours")),
        ("SMOG_AI_HOURLY_SERVING_HORIZON", ("hourly_forecasting", "serving_horizon_hours")),
        ("SMOG_AI_HOURLY_MAX_SOURCE_DELAY", ("hourly_forecasting", "maximum_source_delay_hours")),
        ("SMOG_AI_HOURLY_MAX_MODEL_HORIZON", ("hourly_forecasting", "maximum_model_horizon_hours")),
        ("SMOG_AI_HOURLY_TRAINING_PROFILE", ("hourly_forecasting", "training_policy", "default_profile")),
        ("SMOG_AI_SPATIAL_ENABLED", ("spatial", "enabled")),
        ("SMOG_AI_SPATIAL_ALGORITHM", ("spatial", "algorithm")),
        ("SMOG_AI_SPATIAL_GRID_RESOLUTION_KM", ("spatial", "grid_resolution_km")),
        ("SMOG_AI_ALLOW_DATABASE_FALLBACK", ("training", "allow_database_fallback")),
        ("SMOG_AI_LLM_PROVIDER", ("nlp", "provider")),
        ("SMOG_AI_LLM_MODEL", ("nlp", "model")),
        ("SMOG_AI_LLM_BASE_URL", ("nlp", "base_url")),
        ("SMOG_AI_OBSERVABILITY_BACKEND", ("observability", "backend")),
        ("SMOG_AI_MLFLOW_ENABLED", ("mlflow", "enabled")),
        ("SMOG_AI_MLFLOW_TRACKING_URI", ("mlflow", "tracking_uri")),
        ("SMOG_AI_MLFLOW_EXPERIMENT", ("mlflow", "experiment_name")),
        ("SMOG_AI_MLFLOW_UI_URL", ("mlflow", "ui_url")),
        ("SMOG_AI_MLFLOW_PUBLISH_COMPARISON", ("mlflow", "publish_comparison_to_object_storage")),
    ]
    for environment_name, path in mapping:
        value = os.getenv(environment_name)
        if value is None:
            continue
        target = payload
        for part in path[:-1]:
            target = target.setdefault(part, {})
        if environment_name in {
            "SMOG_AI_REQUIRE_PANDERA",
            "SMOG_AI_ALLOW_DATABASE_FALLBACK",
            "SMOG_AI_SPATIAL_ENABLED",
            "SMOG_AI_HOURLY_FORECASTING_ENABLED",
            "SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL",
            "SMOG_AI_TRAINING_SNAPSHOT_ENABLED",
            "SMOG_AI_TRAINING_SNAPSHOT_MIRROR_MANIFEST",
            "SMOG_AI_MLFLOW_ENABLED",
            "SMOG_AI_MLFLOW_PUBLISH_COMPARISON",
        }:
            target[path[-1]] = value.strip().lower() in {"1", "true", "yes", "on"}
        elif environment_name in {"SMOG_AI_SPATIAL_GRID_RESOLUTION_KM"}:
            target[path[-1]] = float(value)
        elif environment_name in {
            "SMOG_AI_HOURLY_MAX_HORIZON",
            "SMOG_AI_HOURLY_SERVING_HORIZON",
            "SMOG_AI_HOURLY_MAX_SOURCE_DELAY",
            "SMOG_AI_HOURLY_MAX_MODEL_HORIZON",
            "SMOG_AI_TRAINING_SNAPSHOT_REUSE_MINUTES",
        }:
            target[path[-1]] = int(value)
        else:
            target[path[-1]] = value


def load_config(config_path: Path | str | None = None, env_path: Path | str | None = None) -> AppConfig:
    if env_path is not None:
        _load_env_file(Path(env_path))
    else:
        _load_env_file(default_env_path())

    requested = Path(config_path).expanduser() if config_path is not None else default_config_path()
    if not requested.exists():
        candidates = [project_root() / "config.example.yaml", Path.cwd() / "config.example.yaml"]
        fallback = next((path for path in candidates if path.exists()), candidates[0])
        if fallback.exists() and os.getenv("SMOG_AI_ENV", "development") != "production":
            requested = fallback
        else:
            raise ConfigurationError(
                f"Configuration file does not exist: {requested}. "
                "Copy config.example.yaml and set SMOG_AI_CONFIG."
            )
    try:
        payload: dict[str, Any] = yaml.safe_load(requested.read_text(encoding="utf-8-sig")) or {}
        payload = _expand_env(payload)
        _apply_environment_overrides(payload)
        payload = _resolve_paths(payload, requested)
        config = AppConfig.model_validate(payload)
        config.ensure_directories()
        return config
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration in {requested}: {exc}") from exc
