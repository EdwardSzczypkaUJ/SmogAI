# DigitalOcean App Platform

`app.yaml` uruchamia FastAPI i Streamlit. Oba komponenty czytają gotowe artefakty przez FastAPI/ObjectStore; nie ma joba ML ani bazy. Wartości `${...}` są podstawiane przez GitHub Actions. Lokalnie zweryfikuj spec:

```bash
python scripts/validate_digitalocean_spec.py .do/app.yaml
```
