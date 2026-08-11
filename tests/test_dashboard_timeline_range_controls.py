from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "server" / "dashboard" / "app.py"


def _source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_dashboard_source_is_valid_python() -> None:
    ast.parse(_source())


def test_dashboard_has_synchronized_timeline_range_selector() -> None:
    source = _source()
    assert "def _timeline_range_control(" in source
    assert 'st.select_slider(' in source
    assert '"Zakres godzinowy obu wykresów"' in source
    assert "DEFAULT_TIMELINE_WINDOW_HOURS" in source
    assert "Zmiana nie pobiera danych" in source


def test_plotly_charts_have_interactive_range_tools() -> None:
    source = _source()
    assert '"rangeslider": {' in source
    assert '"rangeselector": {' in source
    assert '"label": "6 h"' in source
    assert '"label": "12 h"' in source
    assert '"label": "24 h"' in source
    assert '"label": "Całość"' in source
    assert '"scrollZoom": True' in source
    assert 'dragmode="pan"' in source
    assert 'hovermode="x unified"' in source


def test_both_detail_charts_share_selected_range() -> None:
    source = _source()
    assert "range_start, range_end = _timeline_range_control(" in source
    assert source.count("range_start=range_start") >= 2
    assert source.count("range_end=range_end") >= 2
    assert source.count("config=PLOTLY_CHART_CONFIG") >= 2
