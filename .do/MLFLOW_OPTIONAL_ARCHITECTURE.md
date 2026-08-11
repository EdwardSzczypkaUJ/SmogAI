# Optional MLflow deployment — not enabled by default

HF20 deliberately does not add an MLflow component to `.do/app.yaml`.
A shared MLflow UI/Registry would require a separate cost-approved design:

- one MLflow web service;
- a DB-backed tracking/registry backend (preferably managed PostgreSQL);
- a dedicated DigitalOcean Spaces prefix for MLflow artifacts;
- authentication and private-network rules;
- `SMOG_AI_MLFLOW_UI_URL` exposed to the Smog AI dashboard.

Until that decision is made, MLflow runs locally and the production application
uses the small versioned `model-comparison.json` artifact. This gives in-app
comparison without paying for an always-on MLflow service.
