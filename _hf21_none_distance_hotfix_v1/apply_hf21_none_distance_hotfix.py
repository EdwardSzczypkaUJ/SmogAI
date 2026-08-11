from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


MARKER = "# HF21_NONE_DISTANCE_HOTFIX_V1"

OLD = '''        if recommend_reference:
            st.error(
                f"Punkty różnią się o {float(distance):.2f} km, czyli więcej niż "
                f"próg {threshold:.1f} km. Domyślnie wybrano punkt niezależnego resolvera."
            )
        elif reference_available:'''

NEW = '''        # HF21_NONE_DISTANCE_HOTFIX_V1
        if recommend_reference:
            if distance is None:
                st.warning(
                    "Resolver zwrócił punkt kontrolny, ale nie podał odległości "
                    "między kandydatami. Domyślnie wybrano punkt niezależnego resolvera; "
                    "sprawdź współrzędne przed zatwierdzeniem."
                )
            else:
                st.error(
                    f"Punkty różnią się o {float(distance):.2f} km, czyli więcej niż "
                    f"próg {threshold:.1f} km. Domyślnie wybrano punkt niezależnego resolvera."
                )
        elif reference_available:'''


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Naprawia TypeError float(None) w potwierdzaniu lokalizacji HF21."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.project_root.resolve()
    dashboard = root / "server" / "dashboard" / "app.py"
    if not dashboard.exists():
        raise RuntimeError(f"Nie znaleziono dashboardu: {dashboard}")

    source = dashboard.read_text(encoding="utf-8-sig")
    if MARKER in source:
        print(f"Already patched: {dashboard}")
        return
    if source.count(OLD) != 1:
        raise RuntimeError(
            "Nie znaleziono dokładnie jednego wadliwego bloku float(distance). "
            "Nie zmieniono pliku."
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = dashboard.with_name(
        f"{dashboard.name}.before-hf21-none-distance-{timestamp}.bak"
    )
    shutil.copy2(dashboard, backup)
    updated = source.replace(OLD, NEW, 1)
    compile(updated, str(dashboard), "exec")
    dashboard.write_text(updated, encoding="utf-8", newline="\n")
    print(f"Patched: {dashboard}")
    print(f"Backup:  {backup}")
    print(f"Marker:  {MARKER}")


if __name__ == "__main__":
    main()
