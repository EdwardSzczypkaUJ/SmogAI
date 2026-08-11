from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Iterable

from smog_ai.modeling.contracts import ModelFitContext, ModelPredictContext, ModelProvider, PredictionBundle

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AliasedModelProvider:
    """Expose an external provider under a configuration-defined name."""

    name: str
    delegate: ModelProvider

    @property
    def task(self):  # type: ignore[no-untyped-def]
        return self.delegate.task

    def fit(self, X, y, *, context: ModelFitContext):  # type: ignore[no-untyped-def]
        return self.delegate.fit(X, y, context=context)

    def predict(
        self,
        artifact: Any,
        X,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        return self.delegate.predict(artifact, X, context=context)

    def describe(self, artifact: Any) -> dict[str, Any]:
        payload = dict(self.delegate.describe(artifact))
        payload["provider"] = self.name
        payload["delegate_provider"] = self.delegate.name
        return payload


class ModelProviderRegistry:
    """Registry/factory for built-in and external forecasting methods.

    External modules can expose ``register_models(registry)``. Installed Python
    packages can alternatively publish entry points in the configured group.
    A provider only depends on pandas frames and the neutral context contracts,
    not on SQLAlchemy, FastAPI, Streamlit or DigitalOcean.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider, *, replace: bool = False) -> None:
        if not isinstance(provider, ModelProvider):
            raise TypeError(
                "Model provider must implement name, task, fit, predict and describe"
            )
        name = str(provider.name).strip()
        if not name:
            raise ValueError("Model provider name cannot be empty")
        if name in self._providers and not replace:
            raise ValueError(f"Model provider already registered: {name}")
        self._providers[name] = provider

    def register_alias(
        self,
        name: str,
        provider: ModelProvider,
        *,
        replace: bool = False,
    ) -> None:
        alias = str(name).strip()
        if not alias:
            raise ValueError("Model provider alias cannot be empty")
        self.register(AliasedModelProvider(alias, provider), replace=replace)

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._providers)) or "none"
            raise KeyError(
                f"Unknown model provider {name!r}; available: {available}"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._providers)

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "task": str(self._providers[name].task),
                "implementation": (
                    f"{self._providers[name].__class__.__module__}."
                    f"{self._providers[name].__class__.__qualname__}"
                ),
            }
            for name in self.names()
        ]

    @staticmethod
    def _instantiate_provider(value: Any) -> ModelProvider | None:
        if isinstance(value, ModelProvider):
            return value
        if inspect.isclass(value):
            candidate = value()
            if isinstance(candidate, ModelProvider):
                return candidate
        return None

    def load_import_string(
        self,
        import_string: str,
        *,
        alias: str | None = None,
        replace: bool = False,
    ) -> None:
        if ":" not in import_string:
            self.load_modules([import_string])
            return
        module_name, object_name = import_string.split(":", 1)
        module = importlib.import_module(module_name)
        loaded = getattr(module, object_name)
        provider = self._instantiate_provider(loaded)
        if provider is not None:
            if alias and alias != provider.name:
                self.register_alias(alias, provider, replace=replace)
            else:
                self.register(provider, replace=replace)
            return
        if callable(loaded):
            result = loaded(self)
            provider = self._instantiate_provider(result)
            if provider is not None:
                if alias and alias != provider.name:
                    self.register_alias(alias, provider, replace=replace)
                else:
                    self.register(provider, replace=replace)
            return
        raise TypeError(
            f"Import string {import_string!r} must resolve to a provider, "
            "provider class or registration hook"
        )

    def load_modules(self, module_names: Iterable[str]) -> None:
        for module_name in module_names:
            module = importlib.import_module(module_name)
            hook = getattr(module, "register_models", None)
            if not callable(hook):
                raise TypeError(
                    f"Model plugin module {module_name!r} must expose "
                    "register_models(registry)"
                )
            hook(self)
            logger.info("Loaded model-provider module %s", module_name)

    def load_entry_points(self, group: str) -> None:
        try:
            candidates = metadata.entry_points(group=group)
        except TypeError:  # pragma: no cover - old importlib.metadata
            candidates = metadata.entry_points().get(group, [])
        for entry_point in candidates:
            loaded = entry_point.load()
            provider = self._instantiate_provider(loaded)
            if provider is not None:
                self.register(provider)
            elif callable(loaded):
                result = loaded(self)
                returned = self._instantiate_provider(result)
                if returned is not None:
                    self.register(returned)
            else:
                raise TypeError(
                    f"Entry point {entry_point.name!r} does not expose a model provider"
                )
            logger.info("Loaded model-provider entry point %s", entry_point.name)


def create_model_registry(
    *,
    plugin_modules: Iterable[str] = (),
    entry_point_group: str = "smog_ai.model_providers",
    load_entry_points: bool = True,
) -> ModelProviderRegistry:
    from smog_ai.modeling.providers import register_builtin_models

    registry = ModelProviderRegistry()
    register_builtin_models(registry)
    registry.load_modules(plugin_modules)
    if load_entry_points:
        registry.load_entry_points(entry_point_group)
    return registry
