# Dodawanie własnej metody modelowania

## Cel

Platforma udostępnia Bridge `ModelProvider`, aby algorytm nie był zaszyty w
pipeline. Provider otrzymuje wyłącznie ramkę cech, cel i jawny kontekst.

## Minimalny provider

```python
from dataclasses import dataclass
import numpy as np
from sklearn.linear_model import HuberRegressor
from smog_ai.modeling import PredictionBundle

@dataclass
class HuberProvider:
    name: str = "huber"
    task: str = "regression"

    def fit(self, X, y, *, context):
        model = HuberRegressor().fit(X, y)
        return {
            "feature_columns": list(context.feature_columns),
            "model": model,
        }

    def predict(self, artifact, X, *, context):
        columns = artifact["feature_columns"]
        return PredictionBundle(
            np.asarray(artifact["model"].predict(X[columns]), dtype=float)
        )

    def describe(self, artifact):
        return {"provider": self.name, "family": "robust_regression"}


def register_models(registry):
    registry.register(HuberProvider())
```

## Moduł konfiguracyjny

```yaml
model_platform:
  plugin_modules:
    - my_company.smog_models
```

## Entry point pakietu Python

```toml
[project.entry-points."smog_ai.model_providers"]
huber = "my_company.smog_models:HuberProvider"
```

## Provider przez `module:object`

```yaml
model_platform:
  external_factories:
    - name: huber
      import_string: my_company.smog_models:HuberProvider
      enabled: true
```

## Włączenie w porównaniu modeli

```yaml
hourly_forecasting:
  target_algorithms:
    PM10: [persistence, hist_gradient_boosting, huber]
    PM2.5: [persistence, hist_gradient_boosting, huber]
```

Provider powinien być deterministyczny dla zadanego `random_state`, zwracać
wyniki o tej samej liczbie wierszy i nie korzystać bezpośrednio z bazy,
DigitalOcean ani FastAPI.

## Gotowy przykład w repozytorium

Kompletny, uruchamialny provider Huber znajduje się w:

```text
examples/custom_model_plugin.py
```

Można go aktywować bez instalowania osobnego pakietu:

```yaml
model_platform:
  plugin_modules: [examples.custom_model_plugin]

hourly_forecasting:
  target_algorithms:
    PM10: [persistence, robust_huber]
```
