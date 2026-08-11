# HF20.2 — MLflow Availability Guard

MLflow jest komponentem opcjonalnym. Włączenie go w konfiguracji nie oznacza,
że serwer HTTP rzeczywiście działa. Przed rozpoczęciem treningu trzeba
rozróżnić pięć stanów:

- `ready`,
- `disabled`,
- `not_installed`,
- `not_running`,
- `invalid_configuration`.

Runner nie uruchamia właściwego treningu w trybie `required`, dopóki
preflight nie zwróci `ready`. W trybie `disabled` ustawia tymczasowe
nadpisanie `SMOG_AI_MLFLOW_ENABLED=false`; po zakończeniu przywraca
poprzednie zmienne środowiskowe.

Nie są zmieniane dane, snapshoty, modele ani ustawienia DigitalOcean.
