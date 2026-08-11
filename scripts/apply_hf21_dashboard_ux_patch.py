from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path


MARKER = "# HF21_DASHBOARD_LOCATION_MODEL_UX_V2"
OLD_CONFIRMATION_MARKER = "# HF21_OPENAI_COORDINATE_TIME_CONFIRMATION_V1"
QUERY_RESULT = '    query_result = st.session_state.get("query_result")'

IMPORT_ANCHOR = "import pydeck as pdk\nimport streamlit as st"
IMPORT_REPLACEMENT = """import pydeck as pdk
import streamlit as st
import folium
from streamlit_folium import st_folium"""

FORM_ANCHOR = '    with st.form("natural-language-query", clear_on_submit=False):'

MAP_PICKER = '''    # HF21_DASHBOARD_LOCATION_MODEL_UX_V2
    if use_exact_point:
        st.markdown("### Wybierz dokładny punkt")
        st.caption(
            "Kliknij dowolne miejsce na mapie. Współrzędne zostaną wpisane do pól "
            "poniżej; możesz je jeszcze poprawić ręcznie przed wysłaniem zapytania."
        )
        picker_latitude = float(st.session_state.get("point_latitude", 50.66699))
        picker_longitude = float(st.session_state.get("point_longitude", 16.18620))
        picker_map = folium.Map(
            location=[picker_latitude, picker_longitude],
            zoom_start=11,
            tiles="OpenStreetMap",
            control_scale=True,
        )
        folium.Marker(
            [picker_latitude, picker_longitude],
            tooltip="Aktualnie wybrany dokładny punkt",
            icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
        ).add_to(picker_map)
        picker_event = st_folium(
            picker_map,
            key="hf21-exact-point-picker",
            height=440,
            use_container_width=True,
            returned_objects=["last_clicked"],
        )
        clicked_point = (picker_event or {}).get("last_clicked") or {}
        clicked_latitude = clicked_point.get("lat")
        clicked_longitude = clicked_point.get("lng")
        if clicked_latitude is not None and clicked_longitude is not None:
            clicked_latitude = round(float(clicked_latitude), 6)
            clicked_longitude = round(float(clicked_longitude), 6)
            previous_click = st.session_state.get("hf21_last_map_click")
            current_click = (clicked_latitude, clicked_longitude)
            if previous_click != current_click:
                st.session_state["hf21_last_map_click"] = current_click
                st.session_state["point_latitude"] = clicked_latitude
                st.session_state["point_longitude"] = clicked_longitude
                st.session_state.setdefault("point_name", "Punkt wybrany na mapie")
                st.rerun()
        st.success(
            "Wybrany punkt: "
            f"{float(st.session_state.get('point_latitude', picker_latitude)):.6f}, "
            f"{float(st.session_state.get('point_longitude', picker_longitude)):.6f}"
        )

'''

CONFIRMATION_UI = '''    # HF21_OPENAI_COORDINATE_TIME_CONFIRMATION_V1
    pending_confirmation = st.session_state.get("pending_query_confirmation")
    if pending_confirmation:
        pending_result = pending_confirmation.get("result") or {}
        pending_intent = pending_result.get("intent") or {}
        pending_place = pending_result.get("place") or {}
        location_check = pending_result.get("location_validation") or {}
        time_check = pending_result.get("time_validation") or {}
        coordinate_candidate = location_check.get("candidate") or {}
        reference = location_check.get("reference") or {}

        candidate_name = str(
            coordinate_candidate.get("name")
            or pending_place.get("name")
            or pending_intent.get("location")
            or "Punkt zaproponowany przez OpenAI"
        )
        candidate_latitude = float(
            coordinate_candidate.get("latitude", pending_place.get("latitude", 50.0))
        )
        candidate_longitude = float(
            coordinate_candidate.get("longitude", pending_place.get("longitude", 19.0))
        )
        reference_available = (
            reference.get("latitude") is not None
            and reference.get("longitude") is not None
        )
        reference_name = str(reference.get("name") or candidate_name)
        reference_latitude = (
            float(reference["latitude"]) if reference_available else candidate_latitude
        )
        reference_longitude = (
            float(reference["longitude"]) if reference_available else candidate_longitude
        )
        reference_source = str(reference.get("source") or "niezależny resolver")
        distance = location_check.get("distance_to_reference_km")
        threshold = float(location_check.get("automatic_acceptance_threshold_km") or 3.0)

        openai_time = str(
            time_check.get("candidate_target_time")
            or pending_intent.get("target_time")
            or ""
        )
        parser_time = str(time_check.get("reference_target_time") or "")
        time_difference = time_check.get("difference_minutes")

        recommend_reference = bool(
            reference_available
            and (distance is None or float(distance) > threshold)
        )
        recommend_parser_time = bool(
            parser_time
            and time_difference is not None
            and float(time_difference) > 1.0
        )
        confirmation_token = "|".join(
            [
                candidate_name,
                f"{candidate_latitude:.6f}",
                f"{candidate_longitude:.6f}",
                openai_time,
                f"{reference_latitude:.6f}",
                f"{reference_longitude:.6f}",
                parser_time,
            ]
        )
        if st.session_state.get("hf21_confirmation_token") != confirmation_token:
            st.session_state["hf21_confirmation_token"] = confirmation_token
            st.session_state["hf21_confirmation_name"] = (
                reference_name if recommend_reference else candidate_name
            )
            st.session_state["hf21_confirmation_latitude"] = (
                reference_latitude if recommend_reference else candidate_latitude
            )
            st.session_state["hf21_confirmation_longitude"] = (
                reference_longitude if recommend_reference else candidate_longitude
            )
            st.session_state["hf21_confirmation_time"] = (
                parser_time if recommend_parser_time else openai_time
            )
            st.session_state["hf21_confirmation_location_source"] = (
                "confirmed_independent_reference"
                if recommend_reference
                else "confirmed_openai_candidate"
            )
            st.session_state["hf21_confirmation_time_source"] = (
                "confirmed_deterministic_parser"
                if recommend_parser_time
                else "confirmed_openai_candidate"
            )

        st.warning(
            "OpenAI zaproponowało lokalizację lub czas, których nie można było "
            "bezpiecznie przyjąć automatycznie. Porównaj źródła przed obliczeniem prognozy."
        )
        if recommend_reference:
            st.error(
                f"Punkty różnią się o {float(distance):.2f} km, czyli więcej niż "
                f"próg {threshold:.1f} km. Domyślnie wybrano punkt niezależnego resolvera."
            )
        elif reference_available:
            st.success(
                "Niezależna kontrola lokalizacji jest dostępna. "
                f"Odległość między punktami: {float(distance or 0.0):.2f} km."
            )
        else:
            st.error(
                "Nie znaleziono niezależnego punktu referencyjnego. "
                "Sprawdź współrzędne ręcznie albo wybierz punkt na mapie."
            )

        location_rows = [
            {
                "Źródło": "OpenAI — propozycja",
                "Nazwa": candidate_name,
                "Latitude": candidate_latitude,
                "Longitude": candidate_longitude,
                "Precyzja": coordinate_candidate.get("precision"),
                "Pewność": coordinate_candidate.get("confidence"),
                "Podstawa": coordinate_candidate.get("basis"),
            }
        ]
        if reference_available:
            location_rows.append(
                {
                    "Źródło": f"Kontrola — {reference_source}",
                    "Nazwa": reference_name,
                    "Latitude": reference_latitude,
                    "Longitude": reference_longitude,
                    "Precyzja": reference.get("precision") or "punkt referencyjny",
                    "Pewność": reference.get("match_score"),
                    "Podstawa": "niezależne wyszukiwanie lokalizacji",
                }
            )
        st.markdown("### Porównanie lokalizacji")
        st.dataframe(pd.DataFrame(location_rows), hide_index=True, use_container_width=True)

        source_buttons = st.columns(2)
        with source_buttons[0]:
            if st.button("Użyj propozycji OpenAI", use_container_width=True):
                st.session_state["hf21_confirmation_name"] = candidate_name
                st.session_state["hf21_confirmation_latitude"] = candidate_latitude
                st.session_state["hf21_confirmation_longitude"] = candidate_longitude
                st.session_state["hf21_confirmation_location_source"] = "confirmed_openai_candidate"
                st.rerun()
        with source_buttons[1]:
            if st.button(
                f"Użyj punktu {reference_source}",
                disabled=not reference_available,
                use_container_width=True,
            ):
                st.session_state["hf21_confirmation_name"] = reference_name
                st.session_state["hf21_confirmation_latitude"] = reference_latitude
                st.session_state["hf21_confirmation_longitude"] = reference_longitude
                st.session_state["hf21_confirmation_location_source"] = "confirmed_independent_reference"
                st.rerun()

        st.markdown("### Porównanie czasu")
        time_rows = [
            {"Źródło": "OpenAI — propozycja", "Termin ISO 8601": openai_time},
        ]
        if parser_time:
            time_rows.append(
                {"Źródło": "Deterministyczny parser czasu", "Termin ISO 8601": parser_time}
            )
        st.dataframe(pd.DataFrame(time_rows), hide_index=True, use_container_width=True)
        if time_difference is not None:
            st.caption(f"Różnica między parserami: {float(time_difference):.2f} min.")

        time_buttons = st.columns(2)
        with time_buttons[0]:
            if st.button("Użyj czasu OpenAI", use_container_width=True):
                st.session_state["hf21_confirmation_time"] = openai_time
                st.session_state["hf21_confirmation_time_source"] = "confirmed_openai_candidate"
                st.rerun()
        with time_buttons[1]:
            if st.button(
                "Użyj czasu parsera kontrolnego",
                disabled=not bool(parser_time),
                use_container_width=True,
            ):
                st.session_state["hf21_confirmation_time"] = parser_time
                st.session_state["hf21_confirmation_time_source"] = "confirmed_deterministic_parser"
                st.rerun()

        with st.form("hf21-coordinate-time-confirmation", clear_on_submit=False):
            st.text_input("Wybrana nazwa punktu", key="hf21_confirmation_name")
            confirmation_columns = st.columns(2)
            with confirmation_columns[0]:
                st.number_input(
                    "Wybrane latitude",
                    min_value=-90.0,
                    max_value=90.0,
                    format="%.6f",
                    key="hf21_confirmation_latitude",
                )
            with confirmation_columns[1]:
                st.number_input(
                    "Wybrane longitude",
                    min_value=-180.0,
                    max_value=180.0,
                    format="%.6f",
                    key="hf21_confirmation_longitude",
                )
            st.text_input(
                "Wybrany termin ISO 8601",
                key="hf21_confirmation_time",
                help="Przykład: 2026-08-12T15:17:00+02:00",
            )
            confirmation_submit = st.form_submit_button(
                "Zatwierdź wybrany punkt i termin — oblicz prognozę",
                type="primary",
                use_container_width=True,
            )

        if confirmation_submit:
            with st.spinner("Obliczam prognozę dla zatwierdzonego punktu i czasu…"):
                try:
                    confirmed_payload = {
                        "text": pending_confirmation.get("question") or pending_result.get("question"),
                        "latitude": st.session_state["hf21_confirmation_latitude"],
                        "longitude": st.session_state["hf21_confirmation_longitude"],
                        "place_name": st.session_state["hf21_confirmation_name"],
                        "location_source": st.session_state.get(
                            "hf21_confirmation_location_source", "confirmed_user_edit"
                        ),
                        "target_time": st.session_state["hf21_confirmation_time"],
                        "time_source": st.session_state.get(
                            "hf21_confirmation_time_source", "confirmed_user_edit"
                        ),
                    }
                    confirmed_result = _request_json(
                        "query",
                        method="POST",
                        payload=confirmed_payload,
                        timeout=QUERY_TIMEOUT_SECONDS,
                    )
                    st.session_state["query_result"] = confirmed_result
                    st.session_state.pop("pending_query_confirmation", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się zatwierdzić punktu lub terminu: {exc}")

        if st.button("Odrzuć propozycje i wróć do zapytania", use_container_width=True):
            st.session_state.pop("pending_query_confirmation", None)
            st.session_state.pop("query_result", None)
            st.rerun()

'''

MODEL_CHART_MARKER = "# HF21_MODEL_COMPARISON_CHARTS_V2"
MODEL_TABLE = '''            st.dataframe(
                comparison_frame, hide_index=True, use_container_width=True
            )'''
MODEL_TABLE_WITH_CHARTS = MODEL_TABLE + '''

            # HF21_MODEL_COMPARISON_CHARTS_V2
            metric_columns = [
                column for column in ("MAE", "RMSE", "Bias")
                if column in comparison_frame.columns
                and comparison_frame[column].notna().any()
            ]
            if metric_columns:
                metric_figure = go.Figure()
                chart_rows = comparison_frame.copy()
                chart_rows["Model"] = (
                    chart_rows["Cel"].astype(str)
                    + " · "
                    + chart_rows["Provider"].astype(str)
                    + chart_rows["Aktywny"].map(lambda value: " · aktywny" if value else "")
                )
                for metric_name in metric_columns:
                    metric_figure.add_bar(
                        name=metric_name,
                        x=chart_rows["Model"],
                        y=chart_rows[metric_name],
                    )
                metric_figure.update_layout(
                    barmode="group",
                    title="Porównanie jakości aktywnych i historycznych modeli",
                    xaxis_title="Cel · provider",
                    yaxis_title="Wartość metryki (mniej = lepiej dla MAE/RMSE)",
                    legend_title="Metryka",
                )
                st.plotly_chart(
                    metric_figure,
                    use_container_width=True,
                    config=PLOTLY_CHART_CONFIG,
                )

            improvement_column = "Poprawa vs persistence"
            if (
                improvement_column in comparison_frame.columns
                and comparison_frame[improvement_column].notna().any()
            ):
                improvement_rows = comparison_frame.dropna(subset=[improvement_column]).copy()
                improvement_rows["Model"] = (
                    improvement_rows["Cel"].astype(str)
                    + " · "
                    + improvement_rows["Provider"].astype(str)
                )
                improvement_figure = go.Figure(
                    go.Bar(
                        x=improvement_rows["Model"],
                        y=improvement_rows[improvement_column],
                        marker_color=[
                            "#43e0c0" if float(value) >= 0 else "#ff6b6b"
                            for value in improvement_rows[improvement_column]
                        ],
                    )
                )
                improvement_figure.update_layout(
                    title="Poprawa względem prognozy persistence",
                    xaxis_title="Cel · provider",
                    yaxis_title="Poprawa",
                )
                st.plotly_chart(
                    improvement_figure,
                    use_container_width=True,
                    config=PLOTLY_CHART_CONFIG,
                )'''


def patch_dashboard(path: Path) -> Path:
    source = path.read_text(encoding="utf-8-sig")
    if MARKER in source:
        raise RuntimeError("Dashboard already contains the HF21 dashboard UX patch")
    if OLD_CONFIRMATION_MARKER not in source:
        raise RuntimeError("Apply the HF21 OpenAI confirmation patch first")
    if IMPORT_ANCHOR not in source:
        raise RuntimeError("Dashboard import anchor was not found")
    if FORM_ANCHOR not in source:
        raise RuntimeError("Natural-language form anchor was not found")

    confirmation_pattern = re.compile(
        rf"    {re.escape(OLD_CONFIRMATION_MARKER)}.*?(?={re.escape(QUERY_RESULT)})",
        flags=re.DOTALL,
    )
    if len(confirmation_pattern.findall(source)) != 1:
        raise RuntimeError("Expected exactly one existing confirmation UI block")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.before-hf21-dashboard-ux-{timestamp}.bak")
    shutil.copy2(path, backup)

    updated = source.replace(IMPORT_ANCHOR, IMPORT_REPLACEMENT, 1)
    updated = updated.replace(FORM_ANCHOR, MAP_PICKER + FORM_ANCHOR, 1)
    updated = confirmation_pattern.sub(CONFIRMATION_UI, updated, count=1)

    if MODEL_CHART_MARKER not in updated:
        if MODEL_TABLE not in updated:
            raise RuntimeError("Model-comparison table anchor was not found")
        updated = updated.replace(MODEL_TABLE, MODEL_TABLE_WITH_CHARTS, 1)

    # Force high-contrast ordinary city labels in the known TextLayer block.
    city_pattern = re.compile(
        r'(data=city_rows,.*?get_text="name",.*?get_color=)\[[^\]]+\](.*?get_background_color=)\[[^\]]+\]',
        flags=re.DOTALL,
    )
    if city_pattern.search(updated):
        updated = city_pattern.sub(
            r'\g<1>[5, 16, 28, 255]\g<2>[255, 255, 255, 248]',
            updated,
            count=1,
        )

    path.write_text(updated, encoding="utf-8", newline="\n")
    compile(updated, str(path), "exec")
    return backup


def _replace_if_present(path: Path, old: str, new: str) -> bool:
    if not path.exists():
        return False
    source = path.read_text(encoding="utf-8-sig")
    if new in source:
        return False
    if old not in source:
        raise RuntimeError(f"Expected patch anchor was not found in {path}")
    path.write_text(source.replace(old, new), encoding="utf-8", newline="\n")
    return True


def patch_supporting_files(project_root: Path) -> list[Path]:
    changed: list[Path] = []
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    def write_changed(path: Path, content: str) -> None:
        backup = path.with_name(
            f"{path.name}.before-hf21-dashboard-ux-{timestamp}.bak"
        )
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(content, encoding="utf-8", newline="\n")
        changed.append(path)

    settings = project_root / "server" / "api" / "settings.py"
    config = project_root / "smog_ai" / "config.py"
    resolver = project_root / "smog_ai" / "places" / "http_geocoder.py"
    pyproject = project_root / "pyproject.toml"
    requirements = project_root / "requirements-server.txt"

    for path in (settings, config):
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8-sig")
        if "gpt-4.1-mini" in source:
            write_changed(
                path,
                source.replace("gpt-4.1-mini", "gpt-5.4-mini"),
            )

    old_resolver = '''        try:
            return self.offline.resolve(primary_name)
        except ValueError:
            contextual = raw_text or ", ".join(
                value for value in (primary_name, context_name) if value
            )
            return self.remote.resolve(contextual)'''
    new_resolver = '''        contextual = raw_text or ", ".join(
            value for value in (primary_name, context_name) if value
        )
        try:
            # When HTTP geocoding is explicitly enabled, use it as the
            # independent verification source even for names already present
            # in the small bundled gazetteer. Results are cached locally.
            return self.remote.resolve(contextual)
        except ValueError:
            return self.offline.resolve(primary_name)'''
    if resolver.exists():
        resolver_source = resolver.read_text(encoding="utf-8-sig")
        if new_resolver not in resolver_source:
            if old_resolver not in resolver_source:
                raise RuntimeError(f"Resolver patch anchor was not found in {resolver}")
            write_changed(
                resolver,
                resolver_source.replace(old_resolver, new_resolver, 1),
            )

    old_dashboard_dependency = 'dashboard = ["streamlit>=1.41,<2"]'
    new_dashboard_dependency = '''dashboard = [
  "streamlit>=1.41,<2",
  "folium>=0.19,<1",
  "streamlit-folium>=0.24,<1",
]'''
    if pyproject.exists():
        pyproject_source = pyproject.read_text(encoding="utf-8-sig")
        if "streamlit-folium" not in pyproject_source:
            if old_dashboard_dependency not in pyproject_source:
                raise RuntimeError(f"Dashboard dependency anchor was not found in {pyproject}")
            write_changed(
                pyproject,
                pyproject_source.replace(
                    old_dashboard_dependency, new_dashboard_dependency, 1
                ),
            )

    if requirements.exists():
        requirements_source = requirements.read_text(encoding="utf-8-sig")
        additions = []
        if not re.search(r"(?m)^folium(?:[<=>].*)?$", requirements_source):
            additions.append("folium>=0.19,<1")
        if not re.search(r"(?m)^streamlit-folium(?:[<=>].*)?$", requirements_source):
            additions.append("streamlit-folium>=0.24,<1")
        if additions:
            write_changed(
                requirements,
                requirements_source.rstrip() + "\n" + "\n".join(additions) + "\n",
            )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard", type=Path, default=Path("server/dashboard/app.py"))
    args = parser.parse_args()
    dashboard = args.dashboard.resolve()
    backup = patch_dashboard(dashboard)
    project_root = dashboard.parent.parent.parent
    supporting_files = patch_supporting_files(project_root)
    print(f"Patched: {dashboard}")
    print(f"Backup:  {backup}")
    print(f"Marker:  {MARKER}")
    for path in supporting_files:
        print(f"Updated: {path}")


if __name__ == "__main__":
    main()
