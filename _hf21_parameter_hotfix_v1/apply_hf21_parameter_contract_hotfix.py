from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


MARKER_QUERY = "# HF21_PARAMETER_CONTRACT_V1"
MARKER_DASHBOARD = "# HF21_DASHBOARD_PARAMETER_KEYS_V1"

QUERY_OLD = '''            requested_parameters = list(dict.fromkeys([
                *interpretation.intent.pollutants,
                "temperature_c",
                "precipitation_probability",
                "precipitation_mm",
            ]))'''

QUERY_NEW = '''            # HF21_PARAMETER_CONTRACT_V1
            parameter_aliases = {
                "TEMPERATURE_C": "temperature_c",
                "PRECIPITATION_MM": "precipitation_mm",
                "PRECIPITATION_PROBABILITY": "precipitation_probability",
                "PM25": "PM2.5",
            }
            requested_parameters: list[str] = []
            requested_parameter_keys: set[str] = set()
            for raw_parameter in [
                *interpretation.intent.pollutants,
                "temperature_c",
                "precipitation_probability",
                "precipitation_mm",
            ]:
                raw_text = str(raw_parameter).strip()
                parameter = parameter_aliases.get(raw_text.upper(), raw_text)
                identity = parameter.casefold()
                if identity not in requested_parameter_keys:
                    requested_parameter_keys.add(identity)
                    requested_parameters.append(parameter)'''

DASHBOARD_HELPER = '''

# HF21_DASHBOARD_PARAMETER_KEYS_V1
def _ui_parameter_key(value: Any) -> str:
    raw = str(value or "").strip()
    aliases = {
        "TEMPERATURE_C": "temperature_c",
        "PRECIPITATION_MM": "precipitation_mm",
        "PRECIPITATION_PROBABILITY": "precipitation_probability",
        "PM25": "PM2.5",
    }
    return aliases.get(raw.upper(), raw)
'''

DASHBOARD_FORECASTS_OLD = '''    forecasts = {str(row.get("parameter")): row for row in result.get("forecasts", [])}'''
DASHBOARD_FORECASTS_NEW = '''    forecasts = {
        _ui_parameter_key(row.get("parameter")): row
        for row in result.get("forecasts", [])
    }'''

DASHBOARD_EXACT_OLD = '''        (item for item in result.get("forecasts", []) if item.get("parameter") == parameter),'''
DASHBOARD_EXACT_NEW = '''        (
            item
            for item in result.get("forecasts", [])
            if _ui_parameter_key(item.get("parameter")) == _ui_parameter_key(parameter)
        ),'''

DASHBOARD_SELECTED_OLD = '''                if row.get("parameter") == parameter
                and row.get("target_time") == selected_entry.get("target_time")'''
DASHBOARD_SELECTED_NEW = '''                if _ui_parameter_key(row.get("parameter")) == _ui_parameter_key(parameter)
                and row.get("target_time") == selected_entry.get("target_time")'''

DASHBOARD_FALLBACK_OLD = '''                    if row.get("parameter") == parameter'''
DASHBOARD_FALLBACK_NEW = '''                    if _ui_parameter_key(row.get("parameter")) == _ui_parameter_key(parameter)'''


def _backup(path: Path, timestamp: str) -> Path:
    backup = path.with_name(f"{path.name}.before-hf21-parameter-contract-{timestamp}.bak")
    shutil.copy2(path, backup)
    return backup


def patch_query(path: Path, timestamp: str) -> Path | None:
    source = path.read_text(encoding="utf-8-sig")
    if MARKER_QUERY in source:
        return None
    if source.count(QUERY_OLD) != 1:
        raise RuntimeError(f"Nie znaleziono jednoznacznego bloku parametrów w {path}")
    backup = _backup(path, timestamp)
    updated = source.replace(QUERY_OLD, QUERY_NEW, 1)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8", newline="\n")
    return backup


def patch_dashboard(path: Path, timestamp: str) -> Path | None:
    source = path.read_text(encoding="utf-8-sig")
    if MARKER_DASHBOARD in source:
        return None
    if source.count(DASHBOARD_FORECASTS_OLD) != 1:
        raise RuntimeError(f"Nie znaleziono słownika prognoz w {path}")
    if source.count(DASHBOARD_EXACT_OLD) != 1:
        raise RuntimeError(f"Nie znaleziono selektora dokładnego punktu w {path}")
    if source.count(DASHBOARD_SELECTED_OLD) != 1:
        raise RuntimeError(f"Nie znaleziono selektora czasu powierzchni w {path}")
    if source.count(DASHBOARD_FALLBACK_OLD) < 1:
        raise RuntimeError(f"Nie znaleziono zapasowego selektora parametru w {path}")

    backup = _backup(path, timestamp)
    updated = source.replace(
        "\ndef _render_forecast_cards(",
        DASHBOARD_HELPER + "\n\ndef _render_forecast_cards(",
        1,
    )
    updated = updated.replace(DASHBOARD_FORECASTS_OLD, DASHBOARD_FORECASTS_NEW, 1)
    updated = updated.replace(DASHBOARD_EXACT_OLD, DASHBOARD_EXACT_NEW, 1)
    updated = updated.replace(DASHBOARD_SELECTED_OLD, DASHBOARD_SELECTED_NEW, 1)
    updated = updated.replace(DASHBOARD_FALLBACK_OLD, DASHBOARD_FALLBACK_NEW)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8", newline="\n")
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Naprawia duplikaty nazw parametrów API i ich wybór w dashboardzie."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.project_root.resolve()
    query = root / "server" / "application" / "query.py"
    dashboard = root / "server" / "dashboard" / "app.py"
    if not query.exists() or not dashboard.exists():
        raise RuntimeError(f"To nie jest katalog projektu HF21: {root}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    query_backup = patch_query(query, timestamp)
    dashboard_backup = patch_dashboard(dashboard, timestamp)
    print(f"Project root: {root}")
    print(f"Query: {'patched' if query_backup else 'already patched'}")
    print(f"Dashboard: {'patched' if dashboard_backup else 'already patched'}")
    if query_backup:
        print(f"Query backup: {query_backup}")
    if dashboard_backup:
        print(f"Dashboard backup: {dashboard_backup}")
    print("Marker API: " + MARKER_QUERY)
    print("Marker dashboard: " + MARKER_DASHBOARD)


if __name__ == "__main__":
    main()
