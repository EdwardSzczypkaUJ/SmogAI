from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "server" / "dashboard" / "app.py"


def _source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _helpers() -> dict[str, Any]:
    source = _source()
    module = ast.parse(source)
    names = {
        "_CITY_LABEL_SETTINGS",
        "_mercator_world_position",
        "_viewport_position",
        "_label_bounds",
        "_boxes_overlap",
        "_choose_label_offset",
        "_same_place",
        "_declutter_city_labels",
    }
    nodes: list[ast.stmt] = []
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            nodes.append(node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in names:
                nodes.append(node)
        elif isinstance(node, ast.Assign):
            assigned = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if assigned & names:
                nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                nodes.append(node)
    namespace: dict[str, Any] = {"math": math, "Any": Any}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(DASHBOARD), "exec"), namespace)
    return namespace


def _places() -> list[dict[str, Any]]:
    return [
        {"name": "Kraków", "latitude": 50.0647, "longitude": 19.9450, "population": 804000},
        {"name": "Katowice", "latitude": 50.2649, "longitude": 19.0238, "population": 279000},
        {"name": "Sosnowiec", "latitude": 50.2863, "longitude": 19.1041, "population": 187000},
        {"name": "Gliwice", "latitude": 50.2945, "longitude": 18.6714, "population": 174000},
        {"name": "Zabrze", "latitude": 50.3249, "longitude": 18.7857, "population": 153000},
        {"name": "Bytom", "latitude": 50.3484, "longitude": 18.9157, "population": 150000},
        {"name": "Tychy", "latitude": 50.1372, "longitude": 18.9664, "population": 126000},
        {"name": "Częstochowa", "latitude": 50.8118, "longitude": 19.1203, "population": 213000},
        {"name": "Bielsko-Biała", "latitude": 49.8224, "longitude": 19.0444, "population": 166000},
        {"name": "Opole", "latitude": 50.6751, "longitude": 17.9213, "population": 127000},
        {"name": "Kielce", "latitude": 50.8661, "longitude": 20.6286, "population": 183000},
        {"name": "Rzeszów", "latitude": 50.0412, "longitude": 21.9991, "population": 198000},
    ]


def test_dashboard_source_is_valid_python() -> None:
    ast.parse(_source())


def test_dashboard_exposes_collision_aware_density_control() -> None:
    source = _source()
    assert "def _declutter_city_labels(" in source
    assert "def _choose_label_offset(" in source
    assert '"Gęstość etykiet miast"' in source
    assert 'CITY_LABEL_DENSITIES = ("Minimalna", "Automatyczna", "Większa")' in source
    assert "reserved_label_boxes" in source


def test_auto_labels_do_not_overlap_and_selected_city_is_not_duplicated() -> None:
    helpers = _helpers()
    selected_place = {
        "name": "Kraków",
        "latitude": 50.0647,
        "longitude": 19.9450,
    }
    rows = helpers["_declutter_city_labels"](
        _places(),
        selected_place=selected_place,
        center_latitude=50.0647,
        center_longitude=19.9450,
        zoom=7.0,
        density="Automatyczna",
        reserved_boxes=[],
    )
    assert rows
    assert all(row["name"] != "Kraków" for row in rows)
    bounds = [tuple(row["_screen_bounds"]) for row in rows]
    for index, left in enumerate(bounds):
        for right in bounds[index + 1 :]:
            assert not helpers["_boxes_overlap"](left, right)


def test_label_density_caps_are_respected() -> None:
    helpers = _helpers()
    kwargs = {
        "selected_place": None,
        "center_latitude": 50.3,
        "center_longitude": 19.1,
        "zoom": 7.0,
        "reserved_boxes": [],
    }
    minimal = helpers["_declutter_city_labels"](_places(), density="Minimalna", **kwargs)
    automatic = helpers["_declutter_city_labels"](_places(), density="Automatyczna", **kwargs)
    dense = helpers["_declutter_city_labels"](_places(), density="Większa", **kwargs)
    assert len(minimal) <= 9
    assert len(automatic) <= 16
    assert len(dense) <= 24
    assert len(minimal) <= len(automatic) <= len(dense)


def test_reserved_selected_label_forces_alternative_city_placement() -> None:
    helpers = _helpers()
    reserved = [
        helpers["_label_bounds"](
            text="Katowice",
            latitude=50.2649,
            longitude=19.0238,
            font_size=20,
            pixel_offset=(0, -34),
            center_latitude=50.2649,
            center_longitude=19.0238,
            zoom=7.0,
            collision_padding=10,
        )
    ]
    rows = helpers["_declutter_city_labels"](
        _places(),
        selected_place={"name": "Katowice", "latitude": 50.2649, "longitude": 19.0238},
        center_latitude=50.2649,
        center_longitude=19.0238,
        zoom=7.0,
        density="Automatyczna",
        reserved_boxes=reserved,
    )
    assert all(row["name"] != "Katowice" for row in rows)
    for row in rows:
        assert not helpers["_boxes_overlap"](tuple(row["_screen_bounds"]), reserved[0])
