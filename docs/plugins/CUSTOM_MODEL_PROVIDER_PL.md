# Dodawanie własnej metody modelowania

## 1. Implementacja

```python
from smog_ai.modeling import PredictionBundle

class MyRegressorProvider:
    name = "my_regressor"
    task = "regression"

    def fit(self, X, y, *, context):
        estimator = ...
        estimator.fit(X[list(context.feature_columns)], y)
        return {
            "estimator": estimator,
            "feature_columns": list(context.feature_columns),
        }

    def predict(self, artifact, X, *, context):
        columns = artifact["feature_columns"]
        values = artifact["estimator"].predict(X[columns])
        return PredictionBundle(values)

    def describe(self, artifact):
        return {"provider": self.name, "library": "my-library"}
```

## 2. Rejestracja modułu

```python
def register_models(registry):
    registry.register(MyRegressorProvider())
```

Konfiguracja:

```yaml
model_platform:
  plugin_modules:
    - my_package.smog_models
```

## 3. Entry point

W `pyproject.toml` zewnętrznego pakietu:

```toml
[project.entry-points."smog_ai.model_providers"]
my-regressor = "my_package.smog_models:register_models"
```

## 4. Użycie dla celu

```yaml
hourly_forecasting:
  target_algorithms:
    PM10:
      - persistence
      - my_regressor
```

Provider nie może korzystać bezpośrednio z sesji SQLAlchemy ani ze Spaces. Otrzymuje wyłącznie ramkę cech, cel i jawny kontekst. Dzięki temu pozostaje testowalny i wymienny.
