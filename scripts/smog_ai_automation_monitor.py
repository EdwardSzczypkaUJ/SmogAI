from __future__ import annotations

# HF21_MODEL_DECISION_UI_REPORT_MONITOR_V1
# HF21_MONITOR_LIVE_TIME_ETA_HISTORY_REPORT_V1
# HF21_STREAMLIT_WIDTH_HISTORY_STATUS_CELL_V2
# HF21_MONITOR_FUTURE_STAGE_ETA_V1
# HF21_MONITOR_ACTUAL_LOG_SIZE_V2
# HF21_MONITOR_TERMINAL_CANDIDATE_STATUS_V3

import html
import json
import os
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


st.set_page_config(page_title="SmogAI — automat", page_icon="🌦️", layout="wide")
runtime = Path(os.environ.get("SMOG_AI_DATA_ROOT", r"C:\ProgramData\SmogAI"))
root = runtime / "logs" / "automation"
try:
    MONITOR_REFRESH_SECONDS = min(
        300,
        max(2, int(os.environ.get("SMOG_AI_MONITOR_REFRESH_SECONDS", "30"))),
    )
except (TypeError, ValueError):
    MONITOR_REFRESH_SECONDS = 30
DISPLAY_TIMEZONE = "Europe/Warsaw"
WARSAW_ZONE = ZoneInfo(DISPLAY_TIMEZONE)
TERMINAL_RUN_STATUSES = {
    "success", "failed", "cancelled", "canceled", "warning", "partial_success",
    "interrupted", "przerwany",
}


def refresh_interval(multiplier: int = 1) -> str:
    return f"{min(900, MONITOR_REFRESH_SECONDS * max(1, multiplier))}s"

# Operational estimates used only until a comparable successful stage exists.
STAGE_FALLBACK_SECONDS = {
    "parameter-catalog": 30.0,
    "collect-gios": 180.0,
    "collect-imgw": 120.0,
    "validate": 600.0,
    "match-stations": 180.0,
    "data-range-audit": 120.0,
    "plan-missing-ranges": 120.0,
    "fill-missing-ranges": 1_800.0,
    "build-features": 600.0,
    "training-delta-plan": 30.0,
    "training-delta-build": 90.0,
    "training-delta-preflight": 30.0,
    "training-delta-verify": 900.0,
    "create-training-snapshot": 900.0,
    "snapshot-train-hourly": 1_800.0,
    "audit-hourly-models": 120.0,
    "audit-hourly-serving-contract": 120.0,
    "update-hourly-residuals": 180.0,
    "predict": 300.0,
    "predict-hourly": 300.0,
    "verify": 180.0,
    "build-spatial-surfaces": 900.0,
    "validate-spatial-surfaces": 180.0,
    "storage-health": 120.0,
}


def normalized_stage_statuses(
    stages: list[dict[str, Any]], run_status: Any
) -> list[dict[str, Any]]:
    """Only the newest active stage may remain ``running`` in history."""
    rows = [dict(item) for item in stages]
    running = [index for index, item in enumerate(rows) if item.get("status") == "running"]
    keep = running[-1] if running and str(run_status).lower() == "running" else None
    for index in running:
        if index != keep:
            rows[index]["status"] = "przerwany"
            rows[index]["status_original"] = "running"
    return rows


def status_palette(value: Any) -> tuple[str, str, int]:
    """One status palette shared by stage, model and history cells."""
    normalized = str(value or "").strip().lower()
    if normalized in {
        "success", "passed", "ok", "completed", "accepted", "active",
        "zatwierdzony", "wybrany",
    }:
        return "#14532d", "#dcfce7", 700
    if normalized in {
        "running", "in_progress", "started", "candidate_running",
        "w trakcie", "trening", "trenuje",
    }:
        return "#1e3a8a", "#dbeafe", 700
    if normalized in {
        "warning", "partial", "partial_success", "experimental",
        "eksperymentalny",
    }:
        return "#713f12", "#fef3c7", 700
    if normalized in {
        "failed", "error", "cancelled", "canceled", "interrupted",
        "przerwany", "nieudany", "odrzucony",
    }:
        return "#7f1d1d", "#fee2e2", 700
    if normalized in {"wytrenowany", "candidate_trained"}:
        return "#164e63", "#cffafe", 700
    if normalized in {
        "pominięty", "pominięty — limit czasu", "candidate_skipped_budget"
    }:
        return "#374151", "#e5e7eb", 700
    return "#334155", "#e2e8f0", 650


def render_table(rows: list[dict[str, Any]], height: int = 420) -> None:
    if wrap_table_text:
        frame = pd.DataFrame(rows)
        if frame.empty:
            st.caption("Brak danych.")
            return
        status_columns = [
            column
            for column in ("Status", "status", "Decyzja")
            if column in frame.columns
        ]

        def status_style(value: Any) -> str:
            background, foreground, weight = status_palette(value)
            return (
                f"background-color:{background};color:{foreground};"
                f"font-weight:{weight};white-space:nowrap"
            )

        styled = frame.style.format(escape="html")
        temporal_columns = [
            column
            for column in frame.columns
            if any(
                token in str(column).strip().lower()
                for token in (
                    "czas",
                    "data",
                    "start",
                    "koniec",
                    "termin",
                    "timestamp",
                    "aktualizacja",
                )
            )
        ]
        if temporal_columns:
            styled = styled.set_properties(
                subset=temporal_columns,
                **{
                    "white-space": "nowrap",
                    "word-break": "normal",
                    "overflow-wrap": "normal",
                    "min-width": "10.5rem",
                },
            )
        stacked_columns = [
            column
            for column in frame.columns
            if column in {
                "Etap / polecenie",
                "Czas",
                "Jakość / ostrzeżenia",
                "Cele",
                "Log",
                "Przebieg / profil",
                "Czas przebiegu",
            }
        ]
        if stacked_columns:
            styled = styled.set_properties(
                subset=stacked_columns,
                **{
                    "white-space": "pre-line",
                    "word-break": "normal",
                    "overflow-wrap": "anywhere",
                    "vertical-align": "top",
                    "line-height": "1.35",
                    "min-width": "11rem",
                },
            )
        if status_columns:
            # pandas 2.x uses map; older supported installations use applymap.
            status_subset = status_columns
            if hasattr(styled, "map"):
                styled = styled.map(status_style, subset=status_subset)
            else:
                styled = styled.applymap(status_style, subset=status_subset)
            styled = styled.set_properties(
                subset=status_subset,
                **{
                    "white-space": "nowrap",
                    "word-break": "normal",
                    "overflow-wrap": "normal",
                    "min-width": "7.5rem",
                    "text-align": "center",
                },
            )
        table_html = styled.hide(axis="index").to_html()
        st.markdown(
            (
                f'<div class="smog-table-wrap" style="max-height:{height}px">'
                f"{table_html}</div>"
            ),
            unsafe_allow_html=True,
        )
        return
    kwargs: dict[str, Any] = {
        "hide_index": True,
        "height": height,
        "width": table_width,
    }
    st.dataframe(rows, **kwargs)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@st.cache_data(show_spinner=False)
def read_report_bytes(path_text: str, modified_ns: int) -> bytes:  # noqa: ARG001
    return Path(path_text).read_bytes()


def resolve_report_files(run: dict[str, Any]) -> dict[str, Path]:
    """Resolve reports from run.json and from the deterministic report directory."""
    run_id = str(run.get("run_id") or "").strip()
    declared = dict(run.get("final_report") or {})
    report_root = runtime / "reports" / "automation" / run_id
    names = {"html": "summary.html", "json": "summary.json", "markdown": "summary.md"}
    resolved: dict[str, Path] = {}
    for kind, filename in names.items():
        candidates: list[Path] = []
        if declared.get(kind):
            candidates.append(Path(str(declared[kind])))
        if run_id:
            candidates.append(report_root / filename)
        resolved_path = next((path for path in candidates if path.exists()), None)
        if resolved_path is not None:
            resolved[kind] = resolved_path
    return resolved


def render_report_controls(
    run: dict[str, Any], *, key_prefix: str, show_path: bool = True
) -> None:
    files = resolve_report_files(run)
    if not files:
        st.caption("Raport dla tego przebiegu nie został jeszcze wygenerowany.")
        return
    terminal = str(run.get("status") or "").lower() in {
        "success", "failed", "cancelled", "canceled", "warning", "partial_success"
    }
    label = "Raport końcowy" if terminal else "Raport częściowy"
    st.success(f"{label} jest dostępny.")
    primary_path = files.get("html") or files.get("markdown") or files.get("json")
    if show_path and primary_path is not None:
        st.code(str(primary_path), language=None)

    columns = st.columns(4)
    show_html = False
    if files.get("html") is not None:
        show_html = columns[0].toggle(
            "Pokaż raport",
            key=f"{key_prefix}-show-html",
            help="Wyświetla raport HTML bezpośrednio w monitorze.",
        )
        html_path = files["html"]
        columns[1].download_button(
            "Pobierz HTML",
            data=read_report_bytes(str(html_path), html_path.stat().st_mtime_ns),
            file_name=f"SmogAI-{run.get('run_id')}-report.html",
            mime="text/html",
            key=f"{key_prefix}-download-html",
        )
    if files.get("json") is not None:
        json_path = files["json"]
        columns[2].download_button(
            "Pobierz JSON",
            data=read_report_bytes(str(json_path), json_path.stat().st_mtime_ns),
            file_name=f"SmogAI-{run.get('run_id')}-report.json",
            mime="application/json",
            key=f"{key_prefix}-download-json",
        )
    if files.get("markdown") is not None:
        markdown_path = files["markdown"]
        columns[3].download_button(
            "Pobierz Markdown",
            data=read_report_bytes(str(markdown_path), markdown_path.stat().st_mtime_ns),
            file_name=f"SmogAI-{run.get('run_id')}-report.md",
            mime="text/markdown",
            key=f"{key_prefix}-download-markdown",
        )
    if show_html and files.get("html") is not None:
        html_path = files["html"]
        html_text = read_report_bytes(
            str(html_path), html_path.stat().st_mtime_ns
        ).decode("utf-8-sig", errors="replace")
        components.html(html_text, height=900, scrolling=True)


@st.cache_data(ttl=3, show_spinner=False)
def read_resource_samples(path_text: str, modified_ns: int) -> list[dict[str, Any]]:  # noqa: ARG001
    """Read the complete telemetry file once per modification, shared by all charts."""
    path = Path(path_text)
    samples: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                try:
                    payload = json.loads(raw_line)
                    if isinstance(payload, dict):
                        samples.append(payload)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    return samples


def complete_resource_history(run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = str(run.get("run_id") or "")
    path = root / "runs" / run_id / "resources.jsonl"
    if path.exists():
        try:
            return read_resource_samples(str(path), path.stat().st_mtime_ns)
        except OSError:
            pass
    return list(run.get("resource_history") or [])


def render_resource_chart(
    history: list[dict[str, Any]],
    stage_marker_map: dict[str, str] | None = None,
) -> None:
    frame = pd.DataFrame(history).copy()
    frame["czas"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dt.tz_convert(DISPLAY_TIMEZONE)
    frame = frame.dropna(subset=["czas"]).sort_values("czas")
    if len(frame) < 2:
        st.caption("Za mało próbek do narysowania historii obciążenia.")
        return
    frame["RAM procesu [MB]"] = pd.to_numeric(
        frame.get("process_ram_bytes"), errors="coerce"
    ).fillna(0) / 1024**2
    frame["RAM systemu [%]"] = pd.to_numeric(
        frame.get("system_ram_used_percent"), errors="coerce"
    ).fillna(0)
    frame["dysk odczyt [MB/s]"] = pd.to_numeric(
        frame.get("disk_read_bps"), errors="coerce"
    ).fillna(0) / 1024**2
    frame["dysk zapis [MB/s]"] = pd.to_numeric(
        frame.get("disk_write_bps"), errors="coerce"
    ).fillna(0) / 1024**2
    frame["sieć pobieranie [MB/s]"] = pd.to_numeric(
        frame.get("network_received_bps"), errors="coerce"
    ).fillna(0) / 1024**2
    frame["sieć wysyłanie [MB/s]"] = pd.to_numeric(
        frame.get("network_sent_bps"), errors="coerce"
    ).fillna(0) / 1024**2
    display_frame = frame.copy()
    plotted_columns = (
        "process_cpu_percent",
        "system_cpu_percent",
        "RAM procesu [MB]",
        "RAM systemu [%]",
        "dysk odczyt [MB/s]",
        "dysk zapis [MB/s]",
        "sieć pobieranie [MB/s]",
        "sieć wysyłanie [MB/s]",
    )
    if relative_resource_chart:
        for column in plotted_columns:
            maximum = float(pd.to_numeric(frame[column], errors="coerce").max() or 0)
            display_frame[column] = (
                pd.to_numeric(frame[column], errors="coerce").fillna(0)
                / maximum * 100.0
                if maximum > 0
                else 0.0
            )

    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.065,
        specs=[
            [{"secondary_y": False}],
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=(
            "CPU",
            "Pamięć RAM",
            "Transfer dyskowy",
            "Transfer sieciowy",
        ),
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["process_cpu_percent"],
            name="CPU procesu [%]", mode="lines", line={"color": "#38bdf8"},
        ),
        row=1, col=1, secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["system_cpu_percent"],
            name="CPU systemu [%]", mode="lines",
            line={"color": "#ef4444", "dash": "dot"},
        ),
        row=1, col=1, secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["RAM procesu [MB]"],
            name="RAM procesu [MB]", mode="lines", line={"color": "#f59e0b"},
        ),
        row=2, col=1, secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["RAM systemu [%]"],
            name="RAM systemu [%]", mode="lines",
            line={"color": "#fb7185", "dash": "dot"},
        ),
        row=2, col=1, secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["dysk odczyt [MB/s]"],
            name="Dysk odczyt [MB/s]", mode="lines", line={"color": "#06b6d4"},
        ),
        row=3, col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["dysk zapis [MB/s]"],
            name="Dysk zapis [MB/s]", mode="lines", line={"color": "#eab308"},
        ),
        row=3, col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["sieć pobieranie [MB/s]"],
            name="Sieć pobieranie [MB/s]", mode="lines", line={"color": "#22c55e"},
        ),
        row=4, col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=display_frame["czas"], y=display_frame["sieć wysyłanie [MB/s]"],
            name="Sieć wysyłanie [MB/s]", mode="lines", line={"color": "#a78bfa"},
        ),
        row=4, col=1,
    )

    marker_map = dict(stage_marker_map or {})
    fallback_marker = len(marker_map) + 1
    previous_stage: str | None = None
    for row in frame.to_dict("records"):
        stage = str(row.get("stage") or "nieznany etap")
        if stage == previous_stage:
            continue
        marker = marker_map.get(stage)
        if marker is None:
            marker = f"E{fallback_marker}"
            fallback_marker += 1
        for chart_row in (1, 2, 3, 4):
            figure.add_vline(
                x=row["czas"], line_width=1, line_dash="dot",
                line_color="rgba(226,232,240,.45)", row=chart_row, col=1,
            )
        figure.add_annotation(
            x=row["czas"], y=1.02, xref="x", yref="paper", text=marker,
            showarrow=False, font={"size": 10, "color": "#f8fafc"},
            bgcolor="rgba(15,23,42,.85)", bordercolor="#64748b",
        )
        previous_stage = stage

    figure.update_xaxes(
        nticks=10,
        tickformat="%d.%m<br>%H:%M",
        showgrid=True,
        gridcolor="rgba(148,163,184,.14)",
        row=4,
        col=1,
    )
    figure.update_layout(
        height=900,
        margin={"l": 20, "r": 20, "t": 65, "b": 20},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.08},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    if relative_resource_chart:
        for chart_row in (1, 2, 3, 4):
            figure.update_yaxes(
                title_text="Względnie [% max]", range=[0, 105],
                row=chart_row, col=1, secondary_y=False,
            )
        figure.update_yaxes(
            title_text="Względnie [% max]", range=[0, 105],
            row=2, col=1, secondary_y=True,
        )
    else:
        figure.update_yaxes(title_text="CPU [%]", row=1, col=1)
        figure.update_yaxes(title_text="Proces [MB]", row=2, col=1, secondary_y=False)
        figure.update_yaxes(title_text="System [%]", row=2, col=1, secondary_y=True)
        figure.update_yaxes(title_text="Dysk [MB/s]", row=3, col=1)
        figure.update_yaxes(title_text="Sieć [MB/s]", row=4, col=1)
    st.plotly_chart(figure, width="stretch", config={"displaylogo": False})


def format_timestamp(value: Any) -> str:
    if not value:
        return "—"
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return str(value)
    return parsed.tz_convert(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def actual_log_size_bytes(stage: dict[str, Any]) -> int | None:
    """Read the real consolidated log size, with live stdout/stderr fallback."""

    raw_path = str(stage.get("log_path") or "").strip()
    if raw_path:
        log_path = Path(raw_path)
        try:
            if log_path.is_file():
                return int(log_path.stat().st_size)
        except OSError:
            pass
        total = 0
        found = False
        for live_path in (
            log_path.with_suffix(".stdout"),
            log_path.with_suffix(".stderr"),
        ):
            try:
                if live_path.is_file():
                    total += int(live_path.stat().st_size)
                    found = True
            except OSError:
                continue
        if found:
            return total
    try:
        return int(stage.get("stdout_bytes", 0) or 0) + int(
            stage.get("stderr_bytes", 0) or 0
        )
    except (TypeError, ValueError):
        return None


def format_log_size(value: int | None) -> str:
    if value is None:
        return "—"
    size = max(0, int(value))
    exact = f"{size:,}".replace(",", " ")
    if size < 1024:
        return f"{exact} B"
    return f"{size / 1024.0:.1f} KiB ({exact} B)"


def timestamp_epoch(value: Any) -> float | None:
    if not value:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed.timestamp())


def elapsed_for_run(run: dict[str, Any], *, live: bool = True) -> float | None:
    """Return final elapsed time or a wall-clock live counter for an active run."""
    status = str(run.get("status") or "").lower()
    started = timestamp_epoch(run.get("started_at"))
    if live and status == "running" and started is not None:
        return max(0.0, float(pd.Timestamp.now(tz="UTC").timestamp()) - started)
    for key in ("elapsed_seconds", "duration_seconds"):
        try:
            if run.get(key) is not None:
                return max(0.0, float(run[key]))
        except (TypeError, ValueError):
            pass
    finished = timestamp_epoch(run.get("finished_at"))
    if started is not None and finished is not None:
        return max(0.0, finished - started)
    return None


def elapsed_for_stage(stage: dict[str, Any], run_status: Any) -> float | None:
    try:
        if stage.get("duration_seconds") is not None:
            return max(0.0, float(stage["duration_seconds"]))
    except (TypeError, ValueError):
        pass
    started = timestamp_epoch(stage.get("started_at"))
    if started is None:
        return None
    finished = timestamp_epoch(stage.get("finished_at"))
    if finished is not None:
        return max(0.0, finished - started)
    if str(stage.get("status") or "").lower() == "running" and str(run_status).lower() == "running":
        return max(0.0, float(pd.Timestamp.now(tz="UTC").timestamp()) - started)
    return None


def current_model_elapsed(run: dict[str, Any]) -> float | None:
    current = dict(run.get("current_model") or {})
    if not current:
        return None
    started = timestamp_epoch(current.get("started_at"))
    if started is None:
        current_key = (str(current.get("target")), str(current.get("provider")))
        for candidate in reversed(list(run.get("candidate_plan") or [])):
            candidate_key = (
                str(candidate.get("target")), str(candidate.get("provider"))
            )
            if candidate_key == current_key and candidate.get("started_at"):
                started = timestamp_epoch(candidate.get("started_at"))
                break
    if started is None:
        return None
    finished = timestamp_epoch(current.get("finished_at"))
    endpoint = finished or float(pd.Timestamp.now(tz="UTC").timestamp())
    return max(0.0, endpoint - started)


def history_runs(limit: int = 30) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    runs_root = root / "runs"
    if not runs_root.exists():
        return records
    paths = sorted(
        runs_root.glob("*/run.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    for path in paths:
        try:
            item = read_json(path)
            item.setdefault("run_id", path.parent.name)
            records.append(item)
        except Exception:
            continue
    return records


def historical_eta(run: dict[str, Any]) -> dict[str, Any] | None:
    """Estimate total and current-stage ETA from comparable completed runs."""
    if str(run.get("status") or "").lower() != "running":
        return None
    elapsed = elapsed_for_run(run)
    if elapsed is None:
        return None
    current_id = str(run.get("run_id") or "")
    profile = str(run.get("profile") or "")
    comparable = [
        item
        for item in history_runs(limit=60)
        if str(item.get("run_id") or "") != current_id
        and str(item.get("profile") or "") == profile
        and str(item.get("status") or "").lower() in {
            "success", "warning", "partial_success"
        }
        and elapsed_for_run(item, live=False) is not None
    ]
    if not comparable:
        return None
    durations = [float(elapsed_for_run(item, live=False) or 0.0) for item in comparable]
    center = float(median(durations))
    remaining = max(0.0, center - elapsed)
    now_epoch = float(pd.Timestamp.now(tz="UTC").timestamp())
    finish = pd.Timestamp(now_epoch + remaining, unit="s", tz="UTC").tz_convert(
        DISPLAY_TIMEZONE
    )
    if len(durations) >= 4:
        series = pd.Series(durations, dtype="float64")
        low_total = float(series.quantile(0.25))
        high_total = float(series.quantile(0.75))
    else:
        low_total = center * 0.85
        high_total = center * 1.15
    low_finish = pd.Timestamp(
        now_epoch + max(0.0, low_total - elapsed), unit="s", tz="UTC"
    ).tz_convert(DISPLAY_TIMEZONE)
    high_finish = pd.Timestamp(
        now_epoch + max(0.0, high_total - elapsed), unit="s", tz="UTC"
    ).tz_convert(DISPLAY_TIMEZONE)
    confidence = "wysoka" if len(durations) >= 8 else "średnia" if len(durations) >= 3 else "niska"

    stage_remaining: float | None = None
    stages = list(run.get("stages") or [])
    current_index = max(0, int(run.get("current_stage_index") or 0) - 1)
    if current_index < len(stages):
        current_stage = stages[current_index]
        stage_key = str(current_stage.get("command") or current_stage.get("name") or "")
        previous_stage_durations: list[float] = []
        for item in comparable:
            for prior_stage in item.get("stages") or []:
                prior_key = str(prior_stage.get("command") or prior_stage.get("name") or "")
                if prior_key != stage_key:
                    continue
                value = elapsed_for_stage(prior_stage, item.get("status"))
                if value is not None:
                    previous_stage_durations.append(float(value))
                    break
        stage_elapsed = elapsed_for_stage(current_stage, run.get("status"))
        if previous_stage_durations and stage_elapsed is not None:
            stage_remaining = max(0.0, float(median(previous_stage_durations)) - stage_elapsed)

    return {
        "remaining_seconds": remaining,
        "finish": finish.strftime("%H:%M"),
        "range": f"{low_finish.strftime('%H:%M')}–{high_finish.strftime('%H:%M')}",
        "confidence": confidence,
        "samples": len(durations),
        "stage_remaining_seconds": stage_remaining,
    }


def future_stage_eta(
    run: dict[str, Any], stages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Predict duration and wall-clock boundaries for every stage in the run."""

    profile = str(run.get("profile") or "")
    current_run_id = str(run.get("run_id") or "")
    history = [
        item
        for item in history_runs(limit=80)
        if str(item.get("run_id") or "") != current_run_id
        and str(item.get("profile") or "") == profile
        and str(item.get("status") or "").lower()
        in {"success", "warning", "partial_success"}
    ]
    durations_by_key: dict[str, list[float]] = {}
    for historical_run in history:
        for historical_stage in historical_run.get("stages") or []:
            if str(historical_stage.get("status") or "").lower() not in {
                "success", "warning", "partial_success"
            }:
                continue
            key = str(
                historical_stage.get("command")
                or historical_stage.get("name")
                or ""
            )
            value = elapsed_for_stage(
                historical_stage, historical_run.get("status")
            )
            if key and value is not None and value > 0:
                durations_by_key.setdefault(key, []).append(float(value))

    now_epoch = float(pd.Timestamp.now(tz="UTC").timestamp())
    cursor = now_epoch
    predictions: list[dict[str, Any]] = []
    for stage in stages:
        key = str(stage.get("command") or stage.get("name") or "")
        samples = durations_by_key.get(key) or []
        if samples:
            estimated_duration = float(median(samples))
            source = "historia"
        else:
            estimated_duration = float(STAGE_FALLBACK_SECONDS.get(key, 300.0))
            source = "wartość początkowa"
        status = str(stage.get("status") or "pending").lower()
        elapsed = elapsed_for_stage(stage, run.get("status"))
        predicted_start: float | None = None
        predicted_finish: float | None = None
        remaining: float | None = None

        if status in {"success", "warning", "partial_success"}:
            predicted_start = timestamp_epoch(stage.get("started_at"))
            predicted_finish = timestamp_epoch(stage.get("finished_at"))
            if elapsed is not None:
                estimated_duration = elapsed
            remaining = 0.0
        elif status == "running":
            predicted_start = timestamp_epoch(stage.get("started_at")) or now_epoch
            remaining = max(0.0, estimated_duration - float(elapsed or 0.0))
            predicted_finish = now_epoch + remaining
            cursor = predicted_finish
        elif status in {
            "failed", "error", "cancelled", "interrupted", "przerwany"
        }:
            predicted_start = timestamp_epoch(stage.get("started_at"))
            predicted_finish = timestamp_epoch(stage.get("finished_at"))
            remaining = 0.0
        else:
            predicted_start = cursor
            remaining = estimated_duration
            predicted_finish = predicted_start + estimated_duration
            cursor = predicted_finish

        predictions.append(
            {
                "estimated_duration_seconds": estimated_duration,
                "remaining_seconds": remaining,
                "predicted_start_epoch": predicted_start,
                "predicted_finish_epoch": predicted_finish,
                "source": source,
                "samples": len(samples),
            }
        )
    return predictions


def eta_clock(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(
        DISPLAY_TIMEZONE
    ).strftime("%H:%M:%S")


def stage_eta_text(stage: dict[str, Any], prediction: dict[str, Any]) -> str:
    status = str(stage.get("status") or "pending").lower()
    duration = format_duration(prediction.get("estimated_duration_seconds"))
    source = str(prediction.get("source") or "—")
    samples = int(prediction.get("samples") or 0)
    basis = f"{source}, próbki: {samples}" if samples else source
    if status in {"success", "warning", "partial_success"}:
        return f"Rzeczywisty czas: {duration}"
    if status == "running":
        return (
            f"Pozostało około: {format_duration(prediction.get('remaining_seconds'))}\n"
            f"Przewidywany koniec: {eta_clock(prediction.get('predicted_finish_epoch'))}\n"
            f"Podstawa: {basis}"
        )
    if status in {
        "failed", "error", "cancelled", "interrupted", "przerwany"
    }:
        return "Brak dalszej prognozy"
    return (
        f"Szacowany czas: {duration}\n"
        f"Start około: {eta_clock(prediction.get('predicted_start_epoch'))}\n"
        f"Koniec około: {eta_clock(prediction.get('predicted_finish_epoch'))}\n"
        f"Podstawa: {basis}"
    )


def history_status_cell(value: Any) -> str:
    """Fill the complete history status cell using the stage-table palette."""
    background, foreground, weight = status_palette(value)
    return (
        f"<div class='smog-history-status-cell' style='background:{background};"
        f"color:{foreground};font-weight:{weight}'>"
        f"{html.escape(str(value or '?'))}</div>"
    )


def render_history_rows(
    records: list[dict[str, Any]], *, current_run_id: str
) -> None:
    """Render report actions in the same visual row as every historical run."""
    if not records:
        st.caption("Brak zapisanych przebiegów.")
        return
    header = st.columns([2.7, 1.1, 2.5, 0.8, 1.8])
    for column, label in zip(
        header,
        ("Przebieg / profil", "Status", "Czas przebiegu", "Postęp", "Raport"),
        strict=True,
    ):
        column.markdown(f"**{label}**")

    normalized_records: list[dict[str, Any]] = []
    for item in records:
        row = dict(item)
        run_id = str(row.get("run_id") or "brak")
        row_status = str(row.get("status") or "?")
        if row_status.lower() == "running" and run_id != current_run_id:
            row_status = "przerwany"
        row["display_status"] = row_status
        normalized_records.append(row)
        with st.container(border=True):
            columns = st.columns([2.7, 1.1, 2.5, 0.8, 1.8])
            columns[0].markdown(
                f"`{run_id}`  \nProfil: **{row.get('profile') or '—'}**"
            )
            columns[1].markdown(history_status_cell(row_status), unsafe_allow_html=True)
            columns[2].markdown(
                f"Start: {format_timestamp(row.get('started_at'))}  \n"
                f"Koniec: {format_timestamp(row.get('finished_at'))}  \n"
                f"Trwanie: **{format_duration(elapsed_for_run(row))}**"
            )
            columns[3].markdown(
                f"**{float(row.get('overall_percent', 0) or 0):.1f}%**"
            )
            files = resolve_report_files(row)
            if not files:
                columns[4].caption("Jeszcze niedostępny")
                continue
            action_columns = columns[4].columns(2)
            if action_columns[0].button(
                "Otwórz",
                key=f"history-open-{run_id}",
                help="Pokaż raport tego przebiegu pod historią.",
            ):
                st.session_state["automation-history-report-run-id"] = run_id
            primary = files.get("html") or files.get("markdown") or files.get("json")
            if primary is not None:
                mime = {
                    ".html": "text/html",
                    ".md": "text/markdown",
                    ".json": "application/json",
                }.get(primary.suffix.lower(), "application/octet-stream")
                action_columns[1].download_button(
                    "Pobierz",
                    data=read_report_bytes(str(primary), primary.stat().st_mtime_ns),
                    file_name=f"SmogAI-{run_id}-report{primary.suffix}",
                    mime=mime,
                    key=f"history-download-{run_id}",
                )

    selected_id = st.session_state.get("automation-history-report-run-id")
    if selected_id:
        selected = next(
            (
                item
                for item in normalized_records
                if str(item.get("run_id") or "brak") == str(selected_id)
            ),
            None,
        )
        if selected is not None:
            st.markdown(f"#### Raport przebiegu `{selected_id}`")
            render_report_controls(
                selected,
                key_prefix=f"history-report-{selected_id}",
            )


def render_resource_chart_section(
    history: list[dict[str, Any]],
    kind: str,
    stage_markers: list[dict[str, Any]],
) -> None:
    definitions: dict[str, tuple[str, list[tuple[str, str, str, float]]]] = {
        "cpu": (
            "CPU",
            [
                ("CPU procesu [%]", "process_cpu_percent", "#38bdf8", 1.0),
                ("CPU systemu [%]", "system_cpu_percent", "#ef4444", 1.0),
            ],
        ),
        "ram": (
            "Pamięć RAM",
            [
                ("RAM procesu [MB]", "process_ram_bytes", "#f59e0b", 1024**2),
                ("RAM systemu [%]", "system_ram_used_percent", "#fb7185", 1.0),
            ],
        ),
        "disk": (
            "Transfer dyskowy",
            [
                ("Odczyt [MB/s]", "disk_read_bps", "#06b6d4", 1024**2),
                ("Zapis [MB/s]", "disk_write_bps", "#eab308", 1024**2),
            ],
        ),
        "network": (
            "Transfer sieciowy",
            [
                ("Pobieranie [MB/s]", "network_received_bps", "#22c55e", 1024**2),
                ("Wysyłanie [MB/s]", "network_sent_bps", "#a78bfa", 1024**2),
            ],
        ),
    }
    title, series = definitions[kind]
    frame = pd.DataFrame(history).copy()
    if frame.empty or "timestamp" not in frame:
        st.caption(f"{title}: brak próbek.")
        return
    frame["czas"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dt.tz_convert(DISPLAY_TIMEZONE)
    frame = frame.dropna(subset=["czas"]).sort_values("czas")
    if len(frame) < 2:
        st.caption(f"{title}: za mało próbek.")
        return
    # Drawing thousands of SVG/WebGL points adds no information at screen
    # resolution. Keep the full time range but bound the rendering workload.
    if len(frame) > 1200:
        positions = [round(index * (len(frame) - 1) / 1199) for index in range(1200)]
        frame = frame.iloc[positions].copy()
    figure = go.Figure()
    for label, key, color, divisor in series:
        values = pd.to_numeric(frame.get(key), errors="coerce").fillna(0) / divisor
        maximum = float(values.max() or 0)
        plotted = values / maximum * 100.0 if relative_resource_chart and maximum > 0 else values
        suffix = f" · max {maximum:.2f}" if relative_resource_chart else ""
        figure.add_trace(
            go.Scatter(
                x=frame["czas"],
                y=plotted,
                name=label + suffix,
                mode="lines",
                line={"color": color, "width": 2.2},
            )
        )
    first_time = frame["czas"].iloc[0]
    last_time = frame["czas"].iloc[-1]
    for index, stage in enumerate(stage_markers):
        started = pd.to_datetime(stage.get("started_at"), utc=True, errors="coerce")
        if not pd.isna(started):
            started = started.tz_convert(DISPLAY_TIMEZONE)
        if pd.isna(started) or started < first_time or started > last_time:
            continue
        marker = str(stage.get("marker") or f"E{index + 1}")
        stage_name = str(stage.get("name") or "etap")
        figure.add_vline(
            x=started, line_width=1, line_dash="dot",
            line_color="rgba(226,232,240,.50)",
        )
        figure.add_annotation(
            x=started, y=1.0, xref="x", yref="paper", text=marker,
            showarrow=False, yshift=10 + (index % 3) * 15,
            font={"size": 10, "color": "#f8fafc"},
            bgcolor="rgba(15,23,42,.85)", bordercolor="#64748b",
        )
        # An invisible marker provides the full stage name in hover without
        # adding another permanent label to the already dense chart.
        figure.add_trace(
            go.Scatter(
                x=[started], y=[100 if relative_resource_chart else None],
                mode="markers", marker={"size": 10, "opacity": 0.01},
                name=f"{marker}: {stage_name}",
                hovertemplate=f"{marker}: {stage_name}<br>%{{x}}<extra></extra>",
                showlegend=False,
            )
        )
    figure.update_xaxes(
        nticks=10, tickformat="%d.%m<br>%H:%M",
        showgrid=True, gridcolor="rgba(148,163,184,.14)",
    )
    figure.update_yaxes(
        title_text="Względnie [% max]" if relative_resource_chart else "Wartość",
        range=[0, 105] if relative_resource_chart else None,
        showgrid=True,
        gridcolor="rgba(148,163,184,.14)",
    )
    figure.update_layout(
        title=title,
        height=360,
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        uirevision=f"smog-ai-{kind}",
    )
    st.plotly_chart(
        figure,
        width="stretch",
        key=f"resource-chart-{kind}",
        config={"displaylogo": False},
    )


def load_model_sections(run: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trained = list(run.get("completed_models") or [])
    comparison_path = runtime / "reports" / "mlflow" / "model-comparison.json"
    comparison: dict[str, Any] = {}
    if comparison_path.exists():
        try:
            comparison = read_json(comparison_path)
        except Exception:
            comparison = {}
    if not trained:
        trained = list(comparison.get("candidate_runs") or [])
    selected = [row for row in trained if row.get("selected")]
    active = [row for row in list(comparison.get("models") or []) if row.get("active")]
    known = {
        (
            str(row.get("target")),
            str(row.get("provider")),
            str(row.get("model_version") or row.get("version")),
        )
        for row in selected
    }
    for row in active:
        key = (
            str(row.get("target")),
            str(row.get("provider")),
            str(row.get("model_version") or row.get("version")),
        )
        if key not in known:
            selected.append(row)
            known.add(key)
    return trained, selected


def model_display_rows(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        metrics = model.get("metrics") or {}
        target = model.get("target") or model.get("tags", {}).get("target") or "—"
        provider = model.get("provider") or model.get("algorithm") or "—"
        selected = bool(model.get("selected") or model.get("active"))
        score = model.get("score")
        if score is None:
            score = metrics.get("mae")
        quality = model.get("quality_status") or metrics.get("quality_status") or "—"
        version = model.get("model_version") or model.get("version") or "—"
        raw_status = str(
            model.get("status") or ("active" if model.get("active") else "—")
        )
        status = {
            "success": "zatwierdzony" if selected else "wytrenowany",
            "candidate_trained": "wytrenowany",
            "candidate_running": "w trakcie",
            "candidate_failed": "nieudany",
            "candidate_skipped_budget": "pominięty — limit czasu",
            "active": "zatwierdzony",
        }.get(raw_status.lower(), raw_status)
        rows.append(
            {
                "Cel / model": f"{target}\n{provider}",
                "Status": status,
                "Wybrany": "tak" if selected else "nie",
                "Wynik / jakość": (
                    f"Wynik/MAE: {score if score is not None else '—'}\n"
                    f"Jakość: {quality}"
                ),
                "Wersja / czas": (
                    f"{version}\n"
                    f"{format_timestamp(model.get('created_at') or model.get('finished_at'))}"
                ),
            }
        )
    return rows


def live_candidate_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    plan = list(run.get("candidate_plan") or [])
    completed = list(run.get("completed_models") or [])
    current = run.get("current_model") or {}
    run_terminal = str(run.get("status") or "").lower() in TERMINAL_RUN_STATUSES
    training_stage = next(
        (
            stage
            for stage in run.get("stages") or []
            if stage.get("command") == "snapshot-train-hourly"
        ),
        {},
    )
    training_terminal = str(training_stage.get("status") or "").lower() in {
        "success", "warning", "partial_success", "failed"
    }
    completed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in completed:
        key = (str(item.get("target")), str(item.get("provider")))
        previous = completed_by_key.get(key)
        if previous is None or item.get("selected"):
            completed_by_key[key] = item
    rows: list[dict[str, Any]] = []
    for item in plan:
        key = (str(item.get("target")), str(item.get("provider")))
        # New progress files carry status in the plan itself.  completed_models
        # remains a compatibility and final-report source.
        result = dict(item)
        result.update(completed_by_key.get(key) or {})
        is_current = (
            key == (str(current.get("target")), str(current.get("provider")))
            and str(current.get("phase") or "").startswith("candidate")
        )
        plan_status = str(result.get("status") or "")
        if result.get("selected"):
            status = "wybrany"
        elif plan_status == "candidate_failed":
            status = "nieudany"
        elif plan_status in {
            "candidate_trained",
            "success",
            "success_budget_truncated",
            "success_quality_experimental",
        }:
            status = "wytrenowany"
        elif plan_status == "candidate_skipped_budget":
            status = "pominięty — limit czasu"
        elif run_terminal and (plan_status == "candidate_running" or is_current):
            status = "nieukończony"
        elif plan_status == "candidate_running" or is_current:
            status = "w trakcie"
        elif training_terminal:
            # Compatibility for runs created before the trainer started
            # persisting candidate_skipped_budget explicitly.
            status = "pominięty po zakończeniu etapu"
        else:
            status = "oczekuje"
        rows.append(
            {
                "Cel / kandydat": f"{item.get('target') or '—'}\n{item.get('provider') or '—'}",
                "Pozycja": (
                    f"{item.get('candidate_index') or '—'} / "
                    f"{item.get('candidate_total') or '—'}"
                ),
                "Status": status,
                "Wynik / wersja": (
                    f"MAE: {result.get('score', '—')}\n"
                    f"{result.get('model_version') or result.get('mlflow_run_id') or '—'}"
                ),
            }
        )
    return rows


def render_models_content(*, show_heading: bool = True) -> None:
    try:
        run = load_current()
    except Exception:
        st.caption("Aktualizuję informacje o modelach…")
        return
    if not run:
        return
    trained, selected = load_model_sections(run)
    if show_heading:
        st.subheader("Modele szkolone i wybrane")
    declared_approved = {
        str(target)
        for stage in run.get("stages") or []
        for target in stage.get("approved_targets") or []
    }
    declared_experimental = {
        str(target)
        for stage in run.get("stages") or []
        for target in stage.get("experimental_targets") or []
    }
    approved_models: list[str] = []
    experimental_models: list[str] = []
    selected_by_target: dict[str, dict[str, Any]] = {}
    for model in selected:
        target = str(model.get("target") or model.get("tags", {}).get("target") or "")
        if not target:
            continue
        selected_by_target[target] = model
        metrics = dict(model.get("metrics") or {})
        quality = str(
            model.get("quality_status") or metrics.get("quality_status") or "accepted"
        ).lower()
        if (
            target in declared_experimental
            or quality == "experimental"
            or model.get("experimental")
        ):
            experimental_models.append(target)
        else:
            approved_models.append(target)
    approved_models = list(dict.fromkeys(approved_models))
    experimental_models = list(dict.fromkeys(experimental_models))
    if approved_models:
        st.success("Zatwierdzone modele: " + ", ".join(approved_models))
    if experimental_models:
        st.warning("Modele eksperymentalne: " + ", ".join(experimental_models))
    elif selected:
        st.info("Modele eksperymentalne: brak wśród aktualnie wybranych modeli.")

    publication = dict(run.get("publication") or {})
    published_parameters = [str(value) for value in publication.get("parameters") or []]
    if published_parameters:
        publication_rows: list[dict[str, Any]] = []
        for parameter in published_parameters:
            model_target = (
                "precipitation_mm"
                if parameter == "precipitation_probability"
                else parameter
            )
            source_model = selected_by_target.get(model_target) or {}
            metrics = dict(source_model.get("metrics") or {})
            quality = str(
                source_model.get("quality_status")
                or metrics.get("quality_status")
                or "accepted"
            ).lower()
            is_experimental = bool(
                parameter in declared_experimental
                or model_target in declared_experimental
                or quality == "experimental"
                or source_model.get("experimental")
            )
            publication_rows.append({
                "Cel Serving v2": parameter,
                "Decyzja": "EKSPERYMENTALNY" if is_experimental else "ZATWIERDZONY",
                "Model źródłowy": model_target,
                "Provider": source_model.get("provider") or source_model.get("algorithm") or "—",
                "Sposób": (
                    "wyjście klasyfikacyjne hurdle"
                    if parameter == "precipitation_probability"
                    else "wyjście bezpośrednie"
                ),
            })
        st.markdown("**Cele opublikowane w Serving v2**")
        render_table(publication_rows, height=300)
        st.caption(
            f"Release: {publication.get('release_id') or '—'} · "
            f"powierzchnie: {publication.get('surface_count') or '—'}"
        )
    experimental_policy = (run.get("input_contract") or {}).get("experimental_targets")
    if experimental_policy:
        st.caption(
            "Polityka przebiegu — cele dopuszczone jako eksperymentalne: "
            + ("wszystkie (*)" if experimental_policy == "*" else str(experimental_policy))
        )
    live_rows = live_candidate_rows(run)
    processed_count = sum(
        1 for item in live_rows if item.get("Status") not in {"oczekuje", "w trakcie"}
    )
    if live_rows:
        st.markdown(
            f"**Plan kandydatów: {processed_count}/{len(live_rows)} przetworzonych**"
        )
        render_table(live_rows, height=420)
    else:
        st.caption("Plan kandydatów pojawi się po rozpoczęciu etapu treningu.")
    st.markdown(f"**Kandydaci i modele przetworzone ({len(trained)})**")
    if trained:
        render_table(model_display_rows(trained), height=340)
    else:
        st.caption("Trening jeszcze nie przekazał listy kandydatów.")
    st.markdown(f"**Modele wybrane lub aktywne ({len(selected)})**")
    if selected:
        render_table(model_display_rows(selected), height=340)
    else:
        st.caption("Nie wybrano jeszcze modelu w tym przebiegu.")


def render_chart_content(kind: str) -> None:
    try:
        run = load_current()
    except Exception:
        st.caption("Aktualizuję próbki wykresu…")
        return
    if not run:
        st.caption("Brak aktywnego przebiegu.")
        return
    stage_markers = [
        {
            "marker": f"E{index}",
            "name": item.get("name"),
            "started_at": item.get("started_at"),
        }
        for index, item in enumerate(run.get("stages", []), start=1)
        if item.get("started_at")
    ]
    render_resource_chart_section(
        complete_resource_history(run),
        kind,
        stage_markers,
    )


def load_current() -> dict[str, Any] | None:
    current = root / "current.json"
    if not current.exists():
        return None
    pointer = read_json(current)
    return read_json(Path(pointer["status_path"]))


def render_status_content() -> None:
    try:
        run = load_current()
    except Exception:
        # Plik jest wymieniany atomowo, ale system antywirusowy może przez moment
        # blokować uchwyt. Nie czyścimy wtedy całej strony.
        st.caption("Aktualizuję status…")
        return
    if run is None:
        st.info("Nie uruchomiono jeszcze automatu.")
        return

    stage_items = normalized_stage_statuses(
        list(run.get("stages", [])), run.get("status")
    )
    stage_predictions = future_stage_eta(run, stage_items)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    status_value = str(run.get("status", "?"))
    status_color = {
        "success": "#22c55e", "running": "#38bdf8", "warning": "#f59e0b",
        "failed": "#ef4444", "interrupted": "#94a3b8", "przerwany": "#94a3b8",
    }.get(status_value.lower(), "#cbd5e1")
    c1.markdown(
        f"<small>Status</small><div style='color:{status_color};font-size:1.8rem;"
        f"font-weight:750;white-space:nowrap'>{status_value}</div>",
        unsafe_allow_html=True,
    )
    c2.metric("Profil", run.get("profile", "?"))
    c3.metric("Etap", f"{run.get('current_stage_index', 0)}/{run.get('stage_count', 0)}")
    c4.metric("Postęp", f"{float(run.get('overall_percent', 0)):.1f}%")
    run_elapsed = elapsed_for_run(run)
    c5.metric("Czas trwania", format_duration(run_elapsed))
    eta = historical_eta(run)
    predicted_finish = next(
        (
            item.get("predicted_finish_epoch")
            for item in reversed(stage_predictions)
            if item.get("predicted_finish_epoch") is not None
        ),
        None,
    )
    if predicted_finish is not None and status_value.lower() == "running":
        c6.metric("ETA całości", eta_clock(predicted_finish)[:5])
    elif eta:
        c6.metric("ETA całości", eta["finish"])
    elif status_value.lower() in TERMINAL_RUN_STATUSES:
        c6.metric("Zakończono", format_timestamp(run.get("finished_at"))[11:16])
    else:
        c6.metric("ETA całości", "brak historii")
    st.progress(float(run.get("overall_percent", 0)) / 100, text=run.get("current_stage", ""))
    st.progress(float(run.get("stage_percent", 0)) / 100, text=run.get("current_task", ""))
    current_stage_index = max(0, int(run.get("current_stage_index") or 0) - 1)
    if current_stage_index < len(stage_items):
        stage_elapsed = elapsed_for_stage(
            stage_items[current_stage_index], run.get("status")
        )
        stage_caption = f"Czas bieżącego etapu: {format_duration(stage_elapsed)}"
        if eta and eta.get("stage_remaining_seconds") is not None:
            stage_caption += (
                " · przewidywany czas pozostały etapu: "
                + format_duration(eta["stage_remaining_seconds"])
            )
        st.caption(stage_caption)
    if eta:
        st.caption(
            f"Przewidywane zakończenie: {eta['range']} · "
            f"wiarygodność: {eta['confidence']} · podobne przebiegi: {eta['samples']}"
        )
    if run.get("current_detail"):
        st.info(run["current_detail"])
    current_model = run.get("current_model") or {}
    if current_model:
        phase = str(current_model.get("phase") or "running")
        phase_color = (
            "#22c55e" if phase in {"selected", "success", "completed"}
            else "#ef4444" if phase in {"failed", "error"}
            else "#f59e0b" if phase in {"experimental", "warning"}
            else "#38bdf8"
        )
        details = " · ".join(
            f"{key}={value}" for key, value in current_model.items()
        )
        model_elapsed = current_model_elapsed(run)
        if model_elapsed is not None:
            details += f" · czas modelu={format_duration(model_elapsed)}"
        st.markdown(
            f"<div style='border-left:5px solid {phase_color};padding:.55rem .8rem;"
            f"background:#0f2130;border-radius:.35rem'><b style='color:{phase_color}'>"
            f"Aktualny trening: {phase}</b><br><span>{details}</span></div>",
            unsafe_allow_html=True,
        )
    resource = run.get("resource_current") or {}
    if resource:
        r1, r2, r3, r4, r5, r6 = st.columns(6)
        r1.metric("CPU procesu", f"{float(resource.get('process_cpu_percent', 0)):.1f}%")
        r2.metric("CPU systemu", f"{float(resource.get('system_cpu_percent', 0)):.1f}%")
        r3.metric("RAM procesu", f"{float(resource.get('process_ram_bytes', 0))/1024**2:.0f} MB")
        r4.metric("RAM systemu", f"{float(resource.get('system_ram_used_percent', 0)):.1f}%")
        r5.metric("Dysk R/W", f"{float(resource.get('disk_read_bps', 0))/1024**2:.1f} / {float(resource.get('disk_write_bps', 0))/1024**2:.1f} MB/s")
        r6.metric("Sieć ↓/↑", f"{float(resource.get('network_received_bps', 0))/1024**2:.2f} / {float(resource.get('network_sent_bps', 0))/1024**2:.2f} MB/s")
    elif (run.get("resource_summary") or {}).get("error"):
        st.caption("Telemetria zasobów: " + str(run["resource_summary"]["error"]))


def render_details_content(*, section: str = "all") -> None:
    try:
        run = load_current()
    except Exception:
        st.caption("Aktualizuję szczegóły przebiegu…")
        return
    if run is None:
        return
    stage_items = normalized_stage_statuses(
        list(run.get("stages", [])), run.get("status")
    )
    stage_predictions = future_stage_eta(run, stage_items)
    if section in {"all", "diagnostics"}:
        plan = run.get("download_plan") or {}
        st.markdown("**Plan pobierania danych**")
        st.json(plan, expanded=True)
        if run.get("error"):
            st.error(run["error"])
        if run.get("failure_diagnostic"):
            st.markdown("**Szczegółowa diagnostyka ostatniego błędu**")
            st.json(run["failure_diagnostic"], expanded=True)
        warnings = list(dict.fromkeys(run.get("warnings", [])))
        if warnings:
            st.markdown(f"**Ostrzeżenia wcześniejszych etapów ({len(warnings)})**")
            for warning in warnings:
                st.markdown(f"- {warning}")
        st.markdown("**Raport przebiegu**")
        render_report_controls(
            run,
            key_prefix=f"current-report-{run.get('run_id') or 'unknown'}",
        )

    if section == "diagnostics":
        return

    rows = []
    for index, item in enumerate(stage_items, start=1):
        prediction = stage_predictions[index - 1]
        checkpoint = (
            "\nWznowiono z checkpointu"
            if item.get("restored_from_checkpoint", False)
            else ""
        )
        warning = item.get("warning") or "—"
        quality_errors = item.get("quality_errors")
        quality_text = "—" if quality_errors in (None, "", 0) else str(quality_errors)
        approved = ", ".join(item.get("approved_targets") or []) or "—"
        experimental = ", ".join(item.get("experimental_targets") or []) or "—"
        log_size = actual_log_size_bytes(item)
        rows.append(
            {
                "Etap / polecenie": (
                    f"E{index}  {item.get('name') or '—'}\n"
                    f"{item.get('command') or '—'}{checkpoint}"
                ),
                "Status": item.get("status"),
                "Czas": (
                    f"Start: {format_timestamp(item.get('started_at'))}\n"
                    f"Koniec: {format_timestamp(item.get('finished_at'))}\n"
                    f"Trwanie: {format_duration(elapsed_for_stage(item, run.get('status')))}"
                ),
                "Prognoza / ETA": stage_eta_text(item, prediction),
                "Jakość / ostrzeżenia": (
                    f"Błędy jakości: {quality_text}\n"
                    f"Ostrzeżenie: {warning}"
                ),
                "Cele": (
                    f"Zatwierdzone: {approved}\n"
                    f"Eksperymentalne: {experimental}"
                ),
                "Szczegóły": item.get("description") or "—",
                "Log": f"{format_log_size(log_size)}\n{item.get('log_path') or '—'}",
            }
        )
    st.caption(
        "Znaczniki E1, E2… są wspólne dla tej tabeli i pionowych linii na wykresie. "
        "Tabela ma poziomy pasek przewijania."
    )
    render_table(rows, height=520)

    current_run_id = str(run.get("run_id") or "")
    history_records = history_runs(limit=30)
    st.subheader("Historia")
    render_history_rows(history_records, current_run_id=current_run_id)
    st.caption(
        f"Ostatnia aktualizacja statusu: {format_timestamp(run.get('updated_at'))} "
        f"(Europe/Warsaw) · run_id: {run.get('run_id', 'brak')}"
    )


st.title("SmogAI — monitoring automatu")
st.caption(
    f"Runtime: {runtime} · bazowy interwał odświeżania: "
    f"{MONITOR_REFRESH_SECONDS} s"
)
table_width = st.slider("Minimalna szerokość szerokich tabel [px]", 800, 3000, 1600, 100)
wrap_table_text = st.toggle(
    "Zawijaj długi tekst w komórkach tabel",
    value=True,
    help="Wyłącz, aby użyć zwartej tabeli z poziomym suwakiem.",
)
relative_resource_chart = st.toggle(
    "Względna skala wykresów zasobów (0–100% maksimum każdej serii)",
    value=True,
    help=(
        "Ułatwia porównanie kształtu serii o różnych jednostkach. Wyłącz, aby "
        "zobaczyć wartości bezwzględne w %, MB i MB/s."
    ),
)
st.markdown(
    """
    <style>
    [data-testid="stDataFrame"] { overflow-x: auto !important; max-width: 100%; }
    [data-testid="stDataFrame"] > div { overflow-x: auto !important; }
    .smog-table-wrap { overflow: auto; max-width: 100%; border: 1px solid #334155; border-radius: .5rem; }
    .smog-table-wrap table { border-collapse: collapse; width: 100%; min-width: 900px; table-layout: auto; }
    .smog-table-wrap th, .smog-table-wrap td {
      border-bottom: 1px solid #334155; padding: .45rem .6rem; text-align: left;
      vertical-align: top; white-space: normal; overflow-wrap: anywhere; word-break: break-word;
      max-width: 34rem;
    }
    .smog-table-wrap th { position: sticky; top: 0; z-index: 1; background: #111827; }
    .smog-history-status-cell {
      min-height: 6rem; width: calc(100% + 1rem); margin: -.5rem;
      padding: .75rem .5rem; display: flex; align-items: center;
      justify-content: center; text-align: center; border-radius: .35rem;
      box-sizing: border-box; white-space: nowrap;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Streamlit >= 1.37 odświeża wyłącznie ten fragment DOM. Nie przeładowuje strony,
# więc nagłówek, pozycja przewijania i pozostałe komponenty nie migają.
if hasattr(st, "fragment"):
    @st.fragment(run_every=refresh_interval())
    def status_fragment() -> None:
        render_status_content()

    status_fragment()

    with st.expander("3. Wykresy obciążenia", expanded=True):
        chart_left, chart_right = st.columns(2)

        @st.fragment(run_every=refresh_interval(2))
        def cpu_fragment() -> None:
            render_chart_content("cpu")

        @st.fragment(run_every=refresh_interval(2))
        def ram_fragment() -> None:
            render_chart_content("ram")

        @st.fragment(run_every=refresh_interval(3))
        def disk_fragment() -> None:
            render_chart_content("disk")

        @st.fragment(run_every=refresh_interval(3))
        def network_fragment() -> None:
            render_chart_content("network")

        with chart_left:
            cpu_fragment()
            disk_fragment()
        with chart_right:
            ram_fragment()
            network_fragment()

    @st.fragment(run_every=refresh_interval(2))
    def models_fragment() -> None:
        render_models_content(show_heading=False)

    with st.expander("4. Modele szkolone i wybrane", expanded=False):
        models_fragment()

    @st.fragment(run_every=refresh_interval(4))
    def diagnostics_fragment() -> None:
        render_details_content(section="diagnostics")

    with st.expander("5. Plan, ostrzeżenia i diagnostyka", expanded=False):
        diagnostics_fragment()

    @st.fragment(run_every=refresh_interval())
    def stages_fragment() -> None:
        render_details_content(section="stages")

    stages_fragment()
else:
    st.warning("Ta wersja Streamlit nie obsługuje płynnego auto-odświeżania. Zaktualizuj Streamlit lub użyj przycisku.")
    if st.button("Odśwież status", type="primary"):
        st.rerun()
    render_status_content()
    with st.expander("3. Wykresy obciążenia", expanded=True):
        for chart_kind in ("cpu", "ram", "disk", "network"):
            render_chart_content(chart_kind)
    with st.expander("4. Modele szkolone i wybrane", expanded=False):
        render_models_content(show_heading=False)
    with st.expander("5. Plan, ostrzeżenia i diagnostyka", expanded=False):
        render_details_content(section="diagnostics")
    render_details_content(section="stages")
