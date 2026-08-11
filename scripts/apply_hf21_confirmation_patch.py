from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


MARKER = "# HF21_OPENAI_COORDINATE_TIME_CONFIRMATION_V1"

ASSIGNMENT = '                st.session_state["query_result"] = result'
QUERY_RESULT = '    query_result = st.session_state.get("query_result")'

CAPTURE = '''                location_validation = result.get("location_validation") or {}
                time_validation = result.get("time_validation") or {}
                confirmation_required = bool(
                    location_validation.get("confirmation_required")
                    or time_validation.get("confirmation_required")
                )
                if confirmation_required:
                    st.session_state["pending_query_confirmation"] = {
                        "result": result,
                        "question": question,
                    }
                    st.session_state.pop("query_result", None)
                else:
                    st.session_state["query_result"] = result'''

CONFIRMATION_UI = '''    # HF21_OPENAI_COORDINATE_TIME_CONFIRMATION_V1
    pending_confirmation = st.session_state.get("pending_query_confirmation")
    if pending_confirmation:
        pending_result = pending_confirmation.get("result") or {}
        pending_intent = pending_result.get("intent") or {}
        pending_place = pending_result.get("place") or {}
        location_check = pending_result.get("location_validation") or {}
        time_check = pending_result.get("time_validation") or {}
        coordinate_candidate = location_check.get("candidate") or {}

        proposed_name = str(
            coordinate_candidate.get("name")
            or pending_place.get("name")
            or pending_intent.get("location")
            or "Proponowany punkt"
        )
        proposed_latitude = float(
            coordinate_candidate.get("latitude", pending_place.get("latitude", 50.0))
        )
        proposed_longitude = float(
            coordinate_candidate.get("longitude", pending_place.get("longitude", 19.0))
        )
        proposed_time = str(
            time_check.get("candidate_target_time")
            or pending_intent.get("target_time")
            or ""
        )

        st.warning(
            "Rozpoznana lokalizacja lub termin wymagają potwierdzenia. "
            "Prognoza nie zostanie pokazana, dopóki nie zatwierdzisz punktu i czasu."
        )
        reference = location_check.get("reference") or {}
        distance = location_check.get("distance_to_reference_km")
        check_rows = {
            "Nazwa rozpoznana": proposed_name,
            "Proponowane współrzędne": f"{proposed_latitude:.6f}, {proposed_longitude:.6f}",
            "Proponowany termin": proposed_time,
            "Podstawa współrzędnych": coordinate_candidate.get("basis") or "brak informacji",
            "Pewność współrzędnych": coordinate_candidate.get("confidence"),
            "Punkt referencyjny": reference.get("name") or "brak niezależnego punktu",
            "Odległość od punktu referencyjnego [km]": distance,
            "Czas parsera kontrolnego": time_check.get("reference_target_time"),
            "Różnica czasu [min]": time_check.get("difference_minutes"),
            "Powód kontroli lokalizacji": location_check.get("reason"),
            "Powód kontroli czasu": time_check.get("reason"),
        }
        st.json(check_rows, expanded=False)

        with st.form("hf21-coordinate-time-confirmation", clear_on_submit=False):
            confirmation_name = st.text_input(
                "Potwierdzona nazwa punktu",
                value=proposed_name,
            )
            confirmation_columns = st.columns(2)
            with confirmation_columns[0]:
                confirmation_latitude = st.number_input(
                    "Potwierdzone latitude",
                    min_value=-90.0,
                    max_value=90.0,
                    value=proposed_latitude,
                    format="%.6f",
                )
            with confirmation_columns[1]:
                confirmation_longitude = st.number_input(
                    "Potwierdzone longitude",
                    min_value=-180.0,
                    max_value=180.0,
                    value=proposed_longitude,
                    format="%.6f",
                )
            confirmation_time = st.text_input(
                "Potwierdzony termin ISO 8601",
                value=proposed_time,
                help="Przykład: 2026-08-12T15:17:00+02:00",
            )
            confirmation_submit = st.form_submit_button(
                "Zatwierdź punkt i termin — oblicz prognozę",
                type="primary",
                use_container_width=True,
            )

        if confirmation_submit:
            with st.spinner("Obliczam prognozę dla zatwierdzonego punktu i czasu…"):
                try:
                    confirmed_payload = {
                        "text": pending_confirmation.get("question") or pending_result.get("question"),
                        "latitude": confirmation_latitude,
                        "longitude": confirmation_longitude,
                        "place_name": confirmation_name,
                        "location_source": "confirmed_openai_candidate",
                        "target_time": confirmation_time,
                        "time_source": "confirmed_openai_candidate",
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

        if st.button("Odrzuć propozycję", use_container_width=True):
            st.session_state.pop("pending_query_confirmation", None)
            st.session_state.pop("query_result", None)
            st.rerun()

'''


def patch_dashboard(path: Path) -> Path:
    source = path.read_text(encoding="utf-8-sig")
    if MARKER in source:
        raise RuntimeError("Dashboard already contains the HF21 confirmation patch")
    if source.count(ASSIGNMENT) != 1:
        raise RuntimeError(
            "Expected exactly one query-result assignment; dashboard was not modified"
        )
    if source.count(QUERY_RESULT) != 1:
        raise RuntimeError(
            "Expected exactly one query-result rendering marker; dashboard was not modified"
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.before-hf21-confirmation-{timestamp}.bak")
    shutil.copy2(path, backup)

    updated = source.replace(ASSIGNMENT, CAPTURE, 1)
    updated = updated.replace(QUERY_RESULT, CONFIRMATION_UI + QUERY_RESULT, 1)
    path.write_text(updated, encoding="utf-8", newline="\n")
    compile(updated, str(path), "exec")
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dashboard",
        type=Path,
        default=Path("server/dashboard/app.py"),
    )
    args = parser.parse_args()
    dashboard = args.dashboard.resolve()
    backup = patch_dashboard(dashboard)
    print(f"Patched: {dashboard}")
    print(f"Backup:  {backup}")
    print(f"Marker:  {MARKER}")


if __name__ == "__main__":
    main()
