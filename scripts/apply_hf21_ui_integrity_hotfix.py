from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


MARKER = "# HF21_UI_INTEGRITY_HOTFIX_V4"

EXACT_POINT_OLD = '''    if not row:
        return
    place = result.get("place") or {}'''

EXACT_POINT_NEW = '''    # HF21_UI_INTEGRITY_HOTFIX_V4
    forecast_missing_for_parameter = row is None
    row = row or {}
    place = result.get("place") or {}'''

TABLE_START_OLD = '''        [
            ("Punkt zapytania", place.get("name")),'''

TABLE_START_NEW = '''        [
            (
                "Status wybranego parametru",
                (
                    f"Brak opublikowanej prognozy dla {parameter}; "
                    "punkt lokalizacji nadal jest prawidłowo pokazany."
                    if forecast_missing_for_parameter
                    else "Prognoza dostępna"
                ),
            ),
            ("Punkt zapytania", place.get("name")),'''

ACTIVE_TABLE_OLD = '''        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)'''

ACTIVE_TABLE_NEW = '''        active_model_frame = pd.DataFrame(summary_rows)
        st.dataframe(active_model_frame, hide_index=True, use_container_width=True)

        active_metric_columns = [
            column
            for column in ("MAE", "RMSE")
            if column in active_model_frame.columns
            and active_model_frame[column].notna().any()
        ]
        if active_metric_columns:
            active_metric_figure = go.Figure()
            active_chart_rows = active_model_frame.copy()
            active_chart_rows["Model"] = (
                active_chart_rows["Cel"].astype(str)
                + " · "
                + active_chart_rows["Provider"].astype(str)
            )
            for metric_name in active_metric_columns:
                active_metric_figure.add_bar(
                    name=metric_name,
                    x=active_chart_rows["Model"],
                    y=active_chart_rows[metric_name],
                )
            active_metric_figure.update_layout(
                barmode="group",
                title="Jakość aktywnych modeli",
                xaxis_title="Cel · metoda",
                yaxis_title="Błąd (mniej = lepiej)",
                legend_title="Metryka",
            )
            st.plotly_chart(
                active_metric_figure,
                use_container_width=True,
                config=PLOTLY_CHART_CONFIG,
            )
        else:
            st.warning(
                "Aktywne modele nie mają opublikowanych metryk MAE/RMSE. "
                "Wyeksportuj lokalny artefakt porównania modeli."
            )'''


def patch_dashboard(path: Path) -> Path:
    source = path.read_text(encoding="utf-8-sig")
    if MARKER in source:
        raise RuntimeError("Dashboard already contains HF21 UI integrity hotfix v4")
    if source.count(EXACT_POINT_OLD) != 1:
        raise RuntimeError("Exact-point renderer anchor was not found exactly once")
    if source.count(TABLE_START_OLD) != 1:
        raise RuntimeError("Exact-point table anchor was not found exactly once")
    if source.count(ACTIVE_TABLE_OLD) != 1:
        raise RuntimeError("Active-model table anchor was not found exactly once")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.before-hf21-ui-v4-{timestamp}.bak")
    shutil.copy2(path, backup)

    updated = source.replace(EXACT_POINT_OLD, EXACT_POINT_NEW, 1)
    updated = updated.replace(TABLE_START_OLD, TABLE_START_NEW, 1)
    updated = updated.replace(ACTIVE_TABLE_OLD, ACTIVE_TABLE_NEW, 1)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8", newline="\n")
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard", type=Path, default=Path("server/dashboard/app.py"))
    args = parser.parse_args()
    dashboard = args.dashboard.resolve()
    backup = patch_dashboard(dashboard)
    print(f"Patched: {dashboard}")
    print(f"Backup:  {backup}")
    print(f"Marker:  {MARKER}")


if __name__ == "__main__":
    main()
