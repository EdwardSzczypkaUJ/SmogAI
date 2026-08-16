from __future__ import annotations

# HF21_STREAMLIT_WIDTH_API_V2

import hashlib
import gzip
import json
import math
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st
import folium
from streamlit_folium import st_folium

# HF21_COHERENT_UI_API_FIX_V1
# HF21_MODEL_DECISION_UI_REPORT_MONITOR_V1

APP_VERSION = os.getenv("SMOG_AI_APP_VERSION", "1.7.0")
CUSTOMER_NAME = os.getenv("SMOG_AI_CUSTOMER_NAME", "Smog AI Polska")
API_URL = os.getenv(
    "SMOG_AI_DASHBOARD_API_URL", "http://127.0.0.1:8000/api/v1"
).rstrip("/")
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Warsaw")
QUERY_TIMEOUT_SECONDS = float(os.getenv("SMOG_AI_DASHBOARD_QUERY_TIMEOUT_SECONDS", "120"))
TIMELINE_TIMEOUT_SECONDS = float(
    os.getenv("SMOG_AI_DASHBOARD_TIMELINE_TIMEOUT_SECONDS", "180")
)
DEFAULT_QUESTION = (
    "Jutro o 12:00 jadę do Katowic. Jakie będą PM10, PM2.5, "
    "temperatura i opady?"
)
DEFAULT_TIMELINE_WINDOW_HOURS = max(2, min(48, int(
    os.getenv("SMOG_AI_DASHBOARD_DEFAULT_TIMELINE_WINDOW_HOURS", "12")
)))
CITY_LABEL_DENSITIES = ("Minimalna", "Automatyczna", "Większa")
DEFAULT_CITY_LABEL_DENSITY = os.getenv(
    "SMOG_AI_DASHBOARD_CITY_LABEL_DENSITY", "Automatyczna"
)
if DEFAULT_CITY_LABEL_DENSITY not in CITY_LABEL_DENSITIES:
    DEFAULT_CITY_LABEL_DENSITY = "Automatyczna"
PLOTLY_CHART_CONFIG: dict[str, Any] = {
    "displaylogo": False,
    "responsive": True,
    "scrollZoom": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}

# Default public list price for gpt-4.1-mini (USD per 1M tokens).  Production
# can override both values in the environment without rebuilding the image.
OPENAI_INPUT_USD_PER_1M = float(
    os.getenv("SMOG_AI_OPENAI_INPUT_USD_PER_1M", "0.40")
)
OPENAI_OUTPUT_USD_PER_1M = float(
    os.getenv("SMOG_AI_OPENAI_OUTPUT_USD_PER_1M", "1.60")
)
USD_PLN_RATE = float(os.getenv("SMOG_AI_USD_PLN_RATE", "0") or 0)
LANGFUSE_PLAN_NAME = os.getenv("SMOG_AI_LANGFUSE_PLAN_NAME", "Hobby / własne konto")
LANGFUSE_MONTHLY_USD = float(os.getenv("SMOG_AI_LANGFUSE_MONTHLY_USD", "0") or 0)
DIGITALOCEAN_APP_MONTHLY_USD = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_APP_MONTHLY_USD", "5") or 0
)
DIGITALOCEAN_SPACES_MONTHLY_USD = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_SPACES_MONTHLY_USD", "5") or 0
)
DIGITALOCEAN_OVERAGE_MONTHLY_USD = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_OVERAGE_MONTHLY_USD", "0") or 0
)
EXPECTED_MONTHLY_QUERIES = int(
    os.getenv("SMOG_AI_EXPECTED_MONTHLY_QUERIES", "1000") or 0
)
DIGITALOCEAN_SPACES_STORAGE_USED_GB = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_SPACES_STORAGE_USED_GB", "0") or 0
)
DIGITALOCEAN_SPACES_TRANSFER_USED_GB = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_SPACES_TRANSFER_USED_GB", "0") or 0
)
DIGITALOCEAN_APP_TRANSFER_USED_GB = float(
    os.getenv("SMOG_AI_DIGITALOCEAN_APP_TRANSFER_USED_GB", "0") or 0
)
DIGITALOCEAN_USAGE_SOURCE = os.getenv(
    "SMOG_AI_DIGITALOCEAN_USAGE_SOURCE", "not_connected"
)


def _query_cost(response: dict[str, Any]) -> dict[str, Any]:
    interpretation = dict(response.get("interpretation") or {})
    prompt_tokens = int(interpretation.get("prompt_tokens") or 0)
    completion_tokens = int(interpretation.get("completion_tokens") or 0)
    provider = str(interpretation.get("provider") or "unknown")
    model = str(interpretation.get("model") or "—")
    if provider != "openai_compatible" or not (prompt_tokens or completion_tokens):
        return {
            "provider": provider, "model": model,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "usd": 0.0 if provider != "openai_compatible" else None,
            "priced": provider != "openai_compatible",
        }
    usd = (
        prompt_tokens * OPENAI_INPUT_USD_PER_1M
        + completion_tokens * OPENAI_OUTPUT_USD_PER_1M
    ) / 1_000_000
    return {
        "provider": provider, "model": model,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "usd": usd, "priced": True,
    }


def _money(value: float) -> str:
    label = f"${value:.4f}" if 0 < abs(value) < 0.01 else f"${value:.2f}"
    if USD_PLN_RATE > 0:
        pln = value * USD_PLN_RATE
        label += (
            f" / {pln:.4f} zł" if 0 < abs(pln) < 0.01 else f" / {pln:.2f} zł"
        )
    return label


def _render_cost_center(quality: dict[str, Any], current_response: dict[str, Any]) -> None:
    primary = dict(quality.get("primary") or {})
    remote = dict(quality.get("remote") or {})
    interactions = dict(primary.get("interactions") or {})
    current_cost = _query_cost(current_response) if current_response else {
        "provider": "—", "model": "—", "prompt_tokens": 0,
        "completion_tokens": 0, "usd": None, "priced": False,
    }

    st.subheader("Koszty i wykorzystanie usług")
    st.caption(
        "Zmierzone dane są oznaczone jako pomiar. Wartości planów i transferu "
        "wpisane przez `.env` są oznaczone jako konfiguracja, a nie faktura."
    )
    if primary.get("updated_at"):
        st.caption(f"Agregat Object Store zaktualizowany: {_local_time(primary['updated_at'])}")
    openai_tab, langfuse_tab, digitalocean_tab, summary_tab = st.tabs([
        "🤖 OpenAI", "🔭 Langfuse", "☁️ DigitalOcean", "Σ Podsumowanie",
    ])

    with openai_tab:
        prompt_total = int(interactions.get("prompt_tokens") or 0)
        completion_total = int(interactions.get("completion_tokens") or 0)
        tokenized = int(interactions.get("tokenized_interactions") or 0)
        measured_cost = (
            prompt_total * OPENAI_INPUT_USD_PER_1M
            + completion_total * OPENAI_OUTPUT_USD_PER_1M
        ) / 1_000_000
        cards = st.columns(5)
        cards[0].metric("Zapytania zapisane", int(interactions.get("count") or 0))
        cards[1].metric("Z licznikiem tokenów", tokenized)
        cards[2].metric("Tokeny wejściowe", f"{prompt_total:,}".replace(",", " "))
        cards[3].metric("Tokeny wyjściowe", f"{completion_total:,}".replace(",", " "))
        cards[4].metric("Koszt zapisanej historii", _money(measured_cost))
        if (
            current_cost.get("prompt_tokens")
            and not int(interactions.get("tokenized_interactions") or 0)
        ):
            st.warning(
                "Bieżąca odpowiedź zawiera tokeny, ale odczytany agregat historii "
                "jeszcze ich nie zawiera. Użyj przycisku odświeżenia; jeżeli stan "
                "pozostanie bez zmian, API zapisuje do innego prefixu Object Store."
            )
        if current_response:
            current_columns = st.columns(4)
            current_columns[0].metric("Bieżący model", current_cost["model"])
            current_columns[1].metric("Wejście", current_cost["prompt_tokens"])
            current_columns[2].metric("Wyjście", current_cost["completion_tokens"])
            current_columns[3].metric(
                "Koszt bieżącego zapytania",
                _money(float(current_cost["usd"]))
                if current_cost["usd"] is not None else "brak usage",
            )
        daily_rows = []
        for day, row in sorted(dict(interactions.get("daily") or {}).items()):
            prompt = int(row.get("prompt_tokens") or 0)
            completion = int(row.get("completion_tokens") or 0)
            daily_rows.append({
                "Data": day,
                "Zapytania": int(row.get("count") or 0),
                "Tokeny wejściowe": prompt,
                "Tokeny wyjściowe": completion,
                "Koszt USD": (
                    prompt * OPENAI_INPUT_USD_PER_1M
                    + completion * OPENAI_OUTPUT_USD_PER_1M
                ) / 1_000_000,
            })
        if daily_rows:
            daily = pd.DataFrame(daily_rows)
            token_figure = go.Figure()
            token_figure.add_bar(
                x=daily["Data"], y=daily["Tokeny wejściowe"], name="Wejście",
                marker_color="#4f7cff",
            )
            token_figure.add_bar(
                x=daily["Data"], y=daily["Tokeny wyjściowe"], name="Wyjście",
                marker_color="#43e0c0",
            )
            token_figure.add_scatter(
                x=daily["Data"], y=daily["Koszt USD"], name="Koszt USD",
                yaxis="y2", mode="lines+markers", line=dict(color="#ffbf69", width=3),
            )
            token_figure.update_layout(
                title="Historia tokenów i kosztu OpenAI", barmode="stack",
                yaxis_title="Tokeny",
                yaxis2=dict(title="USD", overlaying="y", side="right"),
                legend=dict(orientation="h"),
            )
            st.plotly_chart(token_figure, width="stretch", config=PLOTLY_CHART_CONFIG)
            st.dataframe(daily, hide_index=True, width="stretch")
        else:
            st.info("Historia tokenów pojawi się po przebudowie agregatu i nowych zapytaniach.")

    with langfuse_tab:
        feedback_count = int(primary.get("count") or 0)
        imported = int(dict(primary.get("feedback_labels") or {}).get("imported_from_langfuse") or 0)
        langfuse_cards = st.columns(4)
        langfuse_cards[0].metric("Plan (konfiguracja)", LANGFUSE_PLAN_NAME)
        langfuse_cards[1].metric("Koszt miesięczny", _money(LANGFUSE_MONTHLY_USD))
        langfuse_cards[2].metric("Oceny we własnym Store", feedback_count)
        langfuse_cards[3].metric("Zaimportowane z Langfuse", imported)
        st.metric(
            "Rekordy widoczne zdalnie w Langfuse",
            int(remote.get("count") or 0) if remote.get("status") == "ok" else "niedostępne",
        )
        st.caption(
            "Liczba rekordów zdalnych nie jest liczbą wszystkich requestów HTTP do "
            "Langfuse. Pełny licznik wywołań wymaga osobnej telemetrii transportu."
        )

    with digitalocean_tab:
        do_cards = st.columns(5)
        do_cards[0].metric("App Platform / mies.", _money(DIGITALOCEAN_APP_MONTHLY_USD))
        do_cards[1].metric("Spaces / mies.", _money(DIGITALOCEAN_SPACES_MONTHLY_USD))
        do_cards[2].metric("Spaces zajętość", f"{DIGITALOCEAN_SPACES_STORAGE_USED_GB:.2f} GB")
        do_cards[3].metric("Transfer Spaces", f"{DIGITALOCEAN_SPACES_TRANSFER_USED_GB:.2f} GB")
        do_cards[4].metric("Transfer aplikacji", f"{DIGITALOCEAN_APP_TRANSFER_USED_GB:.2f} GB")
        if DIGITALOCEAN_USAGE_SOURCE == "not_connected":
            st.warning(
                "Metryki użycia DigitalOcean nie są jeszcze połączone z API DigitalOcean. "
                "Wartości 0 GB oznaczają brak pomiaru, a nie brak transferu."
            )
        else:
            st.success(f"Źródło metryk DigitalOcean: {DIGITALOCEAN_USAGE_SOURCE}")
        usage_figure = go.Figure(go.Bar(
            x=["Zajętość Spaces", "Transfer Spaces", "Transfer aplikacji"],
            y=[
                DIGITALOCEAN_SPACES_STORAGE_USED_GB,
                DIGITALOCEAN_SPACES_TRANSFER_USED_GB,
                DIGITALOCEAN_APP_TRANSFER_USED_GB,
            ],
            marker_color=["#43e0c0", "#4f7cff", "#ffbf69"],
            texttemplate="%{y:.2f} GB", textposition="outside",
        ))
        usage_figure.update_layout(title="Wykorzystanie DigitalOcean", yaxis_title="GB")
        st.plotly_chart(usage_figure, width="stretch", config=PLOTLY_CHART_CONFIG)

    with summary_tab:
        prompt_total = int(interactions.get("prompt_tokens") or 0)
        completion_total = int(interactions.get("completion_tokens") or 0)
        tokenized = int(interactions.get("tokenized_interactions") or 0)
        measured_openai = (
            prompt_total * OPENAI_INPUT_USD_PER_1M
            + completion_total * OPENAI_OUTPUT_USD_PER_1M
        ) / 1_000_000
        average_openai = measured_openai / tokenized if tokenized else None
        # Prefer the average of all recorded, tokenized requests.  Immediately
        # after the first request the aggregate can still lag behind, so the
        # current request is a useful, explicitly labelled fallback.
        projection_basis = "średnia zapisanych zapytań"
        if average_openai is None and current_cost.get("usd") is not None:
            average_openai = float(current_cost["usd"])
            projection_basis = "bieżące zapytanie (agregat jeszcze pusty)"
        projected_openai = (
            average_openai * EXPECTED_MONTHLY_QUERIES
            if average_openai is not None else None
        )
        fixed_do = (
            DIGITALOCEAN_APP_MONTHLY_USD + DIGITALOCEAN_SPACES_MONTHLY_USD
            + DIGITALOCEAN_OVERAGE_MONTHLY_USD
        )
        rows = [
            {
                "Usługa": "OpenAI",
                "Koszt zmierzony USD": measured_openai,
                "Prognoza USD / miesiąc": projected_openai,
                "Podstawa": (
                    f"{projection_basis}; {EXPECTED_MONTHLY_QUERIES} zapytań/mies."
                    if projected_openai is not None else "brak zapisanych tokenów"
                ),
            },
            {
                "Usługa": "Langfuse",
                "Koszt zmierzony USD": None,
                "Prognoza USD / miesiąc": LANGFUSE_MONTHLY_USD,
                "Podstawa": f"konfiguracja planu: {LANGFUSE_PLAN_NAME}",
            },
            {
                "Usługa": "DigitalOcean",
                "Koszt zmierzony USD": None,
                "Prognoza USD / miesiąc": fixed_do,
                "Podstawa": "konfiguracja App Platform + Spaces + nadwyżki",
            },
        ]
        frame = pd.DataFrame(rows)
        st.dataframe(frame, hide_index=True, width="stretch")
        known = frame["Prognoza USD / miesiąc"].dropna().sum()
        summary_cards = st.columns(3)
        summary_cards[0].metric(
            "OpenAI — koszt zapisanej historii", _money(measured_openai)
        )
        summary_cards[1].metric(
            "OpenAI — prognoza miesięczna",
            _money(float(projected_openai)) if projected_openai is not None else "brak danych",
        )
        summary_cards[2].metric(
            "Suma prognozowana / miesiąc", _money(float(known))
        )
        st.caption(
            "Prognoza OpenAI = średni koszt zapisanego zapytania z tokenami × "
            f"{EXPECTED_MONTHLY_QUERIES} zapytań/mies. Koszty Langfuse i "
            "DigitalOcean pochodzą z konfiguracji i nie są fakturą."
        )

# deck.gl buduje własny atlas znaków. Jawny zestaw zapobiega zastępowaniu
# polskich liter pustymi kwadratami na warstwach TextLayer.
POLISH_MAP_CHARACTER_SET = "'" + (
    " abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789.,:;!?()-+/°%ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"
) + "'"

PARAMETER_META: dict[str, dict[str, Any]] = {
    "PM10": {
        "label": "PM10",
        "unit": "µg/m³",
        "decimals": 1,
        "scale": 150.0,
        "description": "stężenie pyłu PM10",
    },
    "PM2.5": {
        "label": "PM2.5",
        "unit": "µg/m³",
        "decimals": 1,
        "scale": 100.0,
        "description": "stężenie pyłu PM2.5",
    },
    "temperature_c": {
        "label": "Temperatura",
        "unit": "°C",
        "decimals": 1,
        "scale": 40.0,
        "description": "prognozowana temperatura",
    },
    "precipitation_probability": {
        "label": "Prawdopodobieństwo opadu",
        "unit": "%",
        "decimals": 0,
        "scale": 1.0,
        "description": "prawdopodobieństwo opadu",
    },
    "precipitation_mm": {
        "label": "Oczekiwany opad",
        "unit": "mm/6h",
        "decimals": 2,
        "scale": 8.0,
        "description": "oczekiwana suma opadu w okresie akumulacji",
    },
}
QUERY_PARAMETER_OPTIONS = (
    "PM10",
    "PM2.5",
    "temperature_c",
    "precipitation_probability",
    "precipitation_mm",
)


def _manifest_parameter_options(manifest: dict[str, Any]) -> tuple[str, ...]:
    published = [
        str(value)
        for value in manifest.get("parameters", [])
        if str(value).strip()
    ]
    if not published:
        published = [
            str(row.get("parameter"))
            for row in manifest.get("surfaces", [])
            if row.get("parameter")
        ]
    published_set = set(published)
    ordered = [
        parameter
        for parameter in QUERY_PARAMETER_OPTIONS
        if parameter in published_set
    ]
    ordered.extend(sorted(published_set - set(ordered), key=str.casefold))
    return tuple(ordered) or QUERY_PARAMETER_OPTIONS


def _manifest_parameter_quality(
    manifest: dict[str, Any], parameter: str
) -> dict[str, Any]:
    entries = [
        row
        for row in manifest.get("surfaces", [])
        if str(row.get("parameter")) == str(parameter)
    ]
    metadata_rows = [dict(row.get("metadata") or {}) for row in entries]
    experimental = any(
        bool(row.get("experimental"))
        or str(row.get("quality_status") or "").lower() == "experimental"
        for row in metadata_rows
    )
    reasons = next(
        (
            list(row.get("experimental_reason") or [])
            for row in metadata_rows
            if row.get("experimental_reason")
        ),
        [],
    )
    return {
        "experimental": experimental,
        "quality_status": "experimental" if experimental else "accepted",
        "experimental_reason": reasons,
    }


def _parameter_option_label(
    parameter: str, manifest: dict[str, Any]
) -> str:
    label = str(_parameter_meta(parameter)["label"])
    quality = _manifest_parameter_quality(manifest, parameter)
    return f"🧪 {label} — EKSPERYMENTALNY" if quality["experimental"] else label

st.set_page_config(
    page_title=f"{CUSTOMER_NAME} — prognoza godzinowa",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
:root {
  --ink: #eff8ff;
  --muted: #9eb1c4;
  --panel: rgba(10, 25, 43, .84);
  --line: rgba(154, 199, 225, .18);
  --accent: #43e0c0;
  --accent2: #7ab8ff;
}
.stApp {
  background:
    radial-gradient(circle at 15% 8%, rgba(46, 112, 150, .24), transparent 31rem),
    radial-gradient(circle at 88% 12%, rgba(27, 139, 125, .18), transparent 28rem),
    linear-gradient(180deg, #07131f 0%, #091827 58%, #06111c 100%);
  color: var(--ink);
}
.block-container { max-width: 1500px; padding-top: 1.2rem; padding-bottom: 3rem; }
.hero {
  border: 1px solid var(--line); border-radius: 24px; padding: 1.3rem 1.6rem;
  background: linear-gradient(135deg, rgba(20, 52, 78, .90), rgba(8, 27, 42, .80));
  box-shadow: 0 18px 60px rgba(0,0,0,.25); margin-bottom: 1rem;
}
.hero h1 { margin: .15rem 0 .4rem 0; font-size: clamp(2.0rem, 4vw, 4rem); line-height: .98; }
.hero p { color: var(--muted); max-width: 1050px; margin: 0; font-size: 1.03rem; }
.pill { display: inline-block; border: 1px solid rgba(67,224,192,.35); color: #9ff7e5;
  background: rgba(24,112,101,.18); border-radius: 999px; padding: .3rem .7rem;
  font-size: .75rem; letter-spacing: .08em; font-weight: 700; }
.info-card { border: 1px solid var(--line); border-radius: 18px; padding: .85rem 1rem;
  min-height: 110px; background: var(--panel); }
.info-card .label { color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; }
.info-card .value { font-size: 1.65rem; font-weight: 750; margin-top: .2rem; }
.info-card .sub { color: var(--muted); font-size: .82rem; margin-top: .25rem; }
.section-card { border: 1px solid var(--line); border-radius: 20px; padding: 1rem 1.1rem;
  background: rgba(8, 23, 39, .82); margin: .5rem 0 1rem 0; }
.exact { color: #78efd5; font-weight: 700; }
.approx { color: #ffd27d; font-weight: 700; }
[data-testid="stMetric"] { background: rgba(8, 24, 40, .78); border: 1px solid var(--line);
  border-radius: 16px; padding: .7rem .9rem; }
[data-testid="stTabs"] button { font-weight: 700; }
[data-testid="stDataFrame"] { overflow-x: auto !important; max-width: 100%; }
[data-testid="stDataFrame"] [role="gridcell"] {
  white-space: normal !important; overflow-wrap: anywhere !important;
}
.small-note { color: var(--muted); font-size: .82rem; }
</style>
""",
    unsafe_allow_html=True,
)


def _url(path: str, params: dict[str, Any] | None = None) -> str:
    result = f"{API_URL}/{path.lstrip('/')}"
    if params:
        clean = {key: value for key, value in params.items() if value is not None}
        result += "?" + urllib.parse.urlencode(clean)
    return result


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _url(path, params),
        data=body,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise TimeoutError(
            f"Przekroczono limit {timeout:.0f} s dla {_url(path, params)}"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError(
                f"Przekroczono limit {timeout:.0f} s dla {_url(path, params)}"
            ) from exc
        raise ConnectionError(str(exc.reason)) from exc


def _request_text(path: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(_url(path), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise TimeoutError(
            f"Przekroczono limit {timeout:.0f} s dla {_url(path)}"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError(
                f"Przekroczono limit {timeout:.0f} s dla {_url(path)}"
            ) from exc
        raise ConnectionError(str(exc.reason)) from exc


@st.cache_data(ttl=30, show_spinner=False)
def load_health() -> dict[str, Any]:
    return dict(_request_json("health"))


@st.cache_data(ttl=45, show_spinner=False)
def load_manifest() -> dict[str, Any]:
    return dict(_request_json("spatial/manifest"))


@st.cache_data(ttl=86400, show_spinner=False)
def load_boundary() -> dict[str, Any]:
    return dict(_request_json("spatial/boundary"))


@st.cache_data(ttl=3600, show_spinner=False)
def load_places() -> list[dict[str, Any]]:
    return list(_request_json("spatial/places"))


@st.cache_data(ttl=60, show_spinner=False)
def load_models() -> dict[str, Any]:
    return dict(_request_json("models"))


@st.cache_data(ttl=60, show_spinner=False)
def load_model_comparison() -> dict[str, Any]:
    return dict(_request_json("models/compare"))


@st.cache_data(ttl=60, show_spinner=False)
def load_quality_overview() -> dict[str, Any]:
    return dict(_request_json("quality/overview"))


@st.cache_data(ttl=300, show_spinner=False)
def load_document(path: str) -> str:
    return _request_text(path)


@st.cache_data(ttl=45, show_spinner=False)
def load_surface(
    parameter: str,
    target_time: str | None,
    horizon_hours: int | None,
) -> dict[str, Any]:
    return dict(
        _request_json(
            "spatial/surface",
            params={
                "parameter": parameter,
                "target_time": target_time,
                "horizon_hours": horizon_hours,
            },
            timeout=45,
        )
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_timeline(
    latitude: float,
    longitude: float,
    target_time: str,
    parameters: tuple[str, ...],
    place_name: str | None,
) -> dict[str, Any]:
    return dict(
        _request_json(
            "timeline",
            method="POST",
            payload={
                "latitude": latitude,
                "longitude": longitude,
                "target_time": target_time,
                "parameters": list(parameters),
                "daily_profile": True,
                "place_name": place_name,
            },
            timeout=TIMELINE_TIMEOUT_SECONDS,
        )
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _local_time(value: str | datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    parsed = value if isinstance(value, datetime) else _parse_time(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime(fmt)


def _parameter_meta(parameter: str) -> dict[str, Any]:
    return PARAMETER_META.get(
        parameter,
        {
            "label": parameter,
            "unit": "",
            "decimals": 2,
            "scale": 1.0,
            "description": parameter,
        },
    )


def _format_value(parameter: str, value: float | None) -> str:
    if value is None:
        return "brak"
    meta = _parameter_meta(parameter)
    if parameter == "precipitation_probability":
        return f"{float(value):.0%}"
    return f"{float(value):.{meta['decimals']}f} {meta['unit']}".strip()


def _surface_entries(manifest: dict[str, Any], parameter: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in manifest.get("surfaces", [])
        if str(row.get("parameter")) == parameter
    ]
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("target_time") or ""),
            int(row.get("horizon_hours") or 0),
        ),
    )


def _entry_label(entry: dict[str, Any]) -> str:
    target = _local_time(entry.get("target_time"), "%d.%m %H:%M")
    horizon = int(entry.get("horizon_hours") or 0)
    return f"{target}  ·  +{horizon} h"


@st.cache_data(show_spinner=False)
def _load_poland_dem() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "resources" / "poland_dem_grid.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _terrain_elevation_m(
    latitude: float,
    longitude: float,
    dem: dict[str, Any],
) -> float:
    """Bilinearly interpolate the bundled terrain DEM in real metres."""

    south = float(dem["south"])
    west = float(dem["west"])
    step = float(dem["step_degrees"])
    rows = int(dem["rows"])
    columns = int(dem["columns"])
    values = dem["elevation_m"]
    row_position = max(0.0, min(rows - 1.0, (latitude - south) / step))
    column_position = max(0.0, min(columns - 1.0, (longitude - west) / step))
    row0 = int(math.floor(row_position))
    column0 = int(math.floor(column_position))
    row1 = min(rows - 1, row0 + 1)
    column1 = min(columns - 1, column0 + 1)
    row_fraction = row_position - row0
    column_fraction = column_position - column0

    def value(row: int, column: int) -> float:
        return float(values[row * columns + column])

    lower = value(row0, column0) * (1.0 - column_fraction) + value(
        row0, column1
    ) * column_fraction
    upper = value(row1, column0) * (1.0 - column_fraction) + value(
        row1, column1
    ) * column_fraction
    return max(0.0, lower * (1.0 - row_fraction) + upper * row_fraction)


_CITY_LABEL_SETTINGS: dict[str, dict[str, float | int]] = {
    "Minimalna": {"max_labels": 9, "collision_padding": 15},
    "Automatyczna": {"max_labels": 16, "collision_padding": 10},
    "Większa": {"max_labels": 24, "collision_padding": 6},
}


def _mercator_world_position(
    latitude: float,
    longitude: float,
    zoom: float,
) -> tuple[float, float]:
    """Return Web-Mercator world coordinates in pixels for a deck.gl zoom."""

    latitude = max(-85.05112878, min(85.05112878, float(latitude)))
    longitude = float(longitude)
    world_size = 512.0 * (2.0**float(zoom))
    latitude_radians = math.radians(latitude)
    x = (longitude + 180.0) / 360.0 * world_size
    y = (
        1.0
        - math.asinh(math.tan(latitude_radians)) / math.pi
    ) / 2.0 * world_size
    return x, y


def _viewport_position(
    latitude: float,
    longitude: float,
    *,
    center_latitude: float,
    center_longitude: float,
    zoom: float,
    viewport_width: float = 1200.0,
    viewport_height: float = 680.0,
) -> tuple[float, float]:
    point_x, point_y = _mercator_world_position(latitude, longitude, zoom)
    center_x, center_y = _mercator_world_position(
        center_latitude,
        center_longitude,
        zoom,
    )
    return (
        point_x - center_x + viewport_width / 2.0,
        point_y - center_y + viewport_height / 2.0,
    )


def _label_bounds(
    *,
    text: str,
    latitude: float,
    longitude: float,
    font_size: float,
    pixel_offset: tuple[float, float] | list[float],
    center_latitude: float,
    center_longitude: float,
    zoom: float,
    collision_padding: float,
    viewport_width: float = 1200.0,
    viewport_height: float = 680.0,
) -> tuple[float, float, float, float]:
    point_x, point_y = _viewport_position(
        latitude,
        longitude,
        center_latitude=center_latitude,
        center_longitude=center_longitude,
        zoom=zoom,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    offset_x, offset_y = float(pixel_offset[0]), float(pixel_offset[1])
    label_width = max(50.0, min(250.0, len(str(text)) * font_size * 0.61 + 18.0))
    label_height = max(24.0, font_size * 1.62 + 10.0)
    center_x = point_x + offset_x
    center_y = point_y + offset_y
    return (
        center_x - label_width / 2.0 - collision_padding,
        center_y - label_height / 2.0 - collision_padding,
        center_x + label_width / 2.0 + collision_padding,
        center_y + label_height / 2.0 + collision_padding,
    )


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] <= right[0]
        or left[0] >= right[2]
        or left[3] <= right[1]
        or left[1] >= right[3]
    )


def _choose_label_offset(
    *,
    text: str,
    latitude: float,
    longitude: float,
    font_size: float,
    candidates: list[tuple[int, int]],
    reserved_boxes: list[tuple[float, float, float, float]],
    center_latitude: float,
    center_longitude: float,
    zoom: float,
    collision_padding: float = 6.0,
) -> tuple[list[int], tuple[float, float, float, float]] | None:
    for candidate in candidates:
        bounds = _label_bounds(
            text=text,
            latitude=latitude,
            longitude=longitude,
            font_size=font_size,
            pixel_offset=candidate,
            center_latitude=center_latitude,
            center_longitude=center_longitude,
            zoom=zoom,
            collision_padding=collision_padding,
        )
        if not any(_boxes_overlap(bounds, current) for current in reserved_boxes):
            return [int(candidate[0]), int(candidate[1])], bounds
    return None


def _same_place(
    row: dict[str, Any],
    selected_place: dict[str, Any] | None,
) -> bool:
    if not selected_place:
        return False
    row_name = str(row.get("name") or "").casefold().strip()
    selected_name = str(selected_place.get("name") or "").casefold().strip()
    if row_name and selected_name and row_name == selected_name:
        return True
    try:
        return (
            abs(float(row["latitude"]) - float(selected_place["latitude"])) < 0.015
            and abs(float(row["longitude"]) - float(selected_place["longitude"])) < 0.02
        )
    except (KeyError, TypeError, ValueError):
        return False


def _declutter_city_labels(
    places: list[dict[str, Any]],
    *,
    selected_place: dict[str, Any] | None,
    center_latitude: float,
    center_longitude: float,
    zoom: float,
    density: str,
    reserved_boxes: list[tuple[float, float, float, float]] | None = None,
    viewport_width: float = 1200.0,
    viewport_height: float = 680.0,
) -> list[dict[str, Any]]:
    """Greedily choose readable labels without screen-space collisions.

    The selected city, selected station and grid point reserve their own boxes
    before ordinary city names are placed. The algorithm is deterministic and
    intentionally conservative in the dense Silesian/Krakow agglomerations.
    """

    settings = _CITY_LABEL_SETTINGS.get(
        density,
        _CITY_LABEL_SETTINGS["Automatyczna"],
    )
    max_labels = int(settings["max_labels"])
    collision_padding = float(settings["collision_padding"])
    occupied = list(reserved_boxes or [])
    accepted: list[dict[str, Any]] = []
    candidates = sorted(
        (
            row
            for row in places
            if row.get("latitude") is not None
            and row.get("longitude") is not None
            and not _same_place(row, selected_place)
        ),
        key=lambda row: (
            -int(row.get("population") or 0),
            str(row.get("name") or ""),
        ),
    )
    offset_candidates = [
        (0, -15),
        (0, 15),
        (30, -15),
        (-30, -15),
        (32, 15),
        (-32, 15),
    ]

    for row in candidates:
        screen_x, screen_y = _viewport_position(
            float(row["latitude"]),
            float(row["longitude"]),
            center_latitude=center_latitude,
            center_longitude=center_longitude,
            zoom=zoom,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )
        if not (
            -100.0 <= screen_x <= viewport_width + 100.0
            and -80.0 <= screen_y <= viewport_height + 80.0
        ):
            continue

        population = int(row.get("population") or 0)
        font_size = 24 if population >= 500_000 else 21 if population >= 180_000 else 18
        selected = _choose_label_offset(
            text=str(row.get("name") or ""),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            font_size=font_size,
            candidates=offset_candidates,
            reserved_boxes=occupied,
            center_latitude=center_latitude,
            center_longitude=center_longitude,
            zoom=zoom,
            collision_padding=collision_padding,
        )
        if selected is None:
            continue
        pixel_offset, bounds = selected
        accepted.append(
            {
                **row,
                "font_size": font_size,
                "pixel_offset": pixel_offset,
                "_screen_bounds": bounds,
            }
        )
        occupied.append(bounds)
        if len(accepted) >= max_labels:
            break
    return accepted


def build_map(
    *,
    boundary: dict[str, Any],
    surface: dict[str, Any],
    places: list[dict[str, Any]],
    selected_place: dict[str, Any] | None,
    selected_forecast: dict[str, Any] | None,
    show_stations: bool,
    show_city_names: bool,
    city_label_density: str,
    show_confidence: bool,
    height_scale: float,
) -> pdk.Deck:
    height_scale = max(0.0, float(height_scale))
    view_3d = height_scale > 0.0
    parameter = str(surface.get("parameter") or "PM10")
    meta = _parameter_meta(parameter)
    view_latitude = float(selected_place["latitude"]) if selected_place else 52.05
    view_longitude = float(selected_place["longitude"]) if selected_place else 19.15
    view_zoom = 7.0 if selected_place else 5.25
    terrain_dem = _load_poland_dem()
    reserved_label_boxes: list[tuple[float, float, float, float]] = []

    selected_place_offset = [0, -34]
    if selected_place and selected_place.get("latitude") is not None:
        layout = _choose_label_offset(
            text=str(selected_place.get("label") or selected_place.get("name") or "punkt"),
            latitude=float(selected_place["latitude"]),
            longitude=float(selected_place["longitude"]),
            font_size=25,
            candidates=[(0, -34), (0, 36), (78, -20), (-78, -20)],
            reserved_boxes=reserved_label_boxes,
            center_latitude=view_latitude,
            center_longitude=view_longitude,
            zoom=view_zoom,
            collision_padding=8,
        )
        if layout is not None:
            selected_place_offset, bounds = layout
            reserved_label_boxes.append(bounds)

    cell_label_offset = [0, 18]
    if selected_forecast and selected_forecast.get("cell_latitude") is not None:
        layout = _choose_label_offset(
            text="punkt siatki",
            latitude=float(selected_forecast["cell_latitude"]),
            longitude=float(selected_forecast["cell_longitude"]),
            font_size=17,
            candidates=[(0, 18), (0, -22), (58, 16), (-58, 16)],
            reserved_boxes=reserved_label_boxes,
            center_latitude=view_latitude,
            center_longitude=view_longitude,
            zoom=view_zoom,
            collision_padding=6,
        )
        if layout is not None:
            cell_label_offset, bounds = layout
            reserved_label_boxes.append(bounds)

    cells: list[dict[str, Any]] = []
    for row in surface.get("grid") or []:
        if row.get("latitude") is None or row.get("longitude") is None:
            continue
        item = dict(row)
        value = float(item.get("value") or 0.0)
        confidence = float(item.get("confidence") or 0.0)
        alpha = int(item.get("color_a") or 205)
        if show_confidence:
            alpha = max(25, min(220, int(alpha * max(0.16, confidence))))
        item["display_color"] = [
            int(item.get("color_r") or 0),
            int(item.get("color_g") or 0),
            int(item.get("color_b") or 0),
            alpha,
        ]
        # Kolor oznacza wartość parametru. Wysokość walca jest niezależna i
        # wynika wyłącznie z rzeczywistej wysokości terenu n.p.m.
        item["elevation"] = _terrain_elevation_m(
            float(item["latitude"]),
            float(item["longitude"]),
            terrain_dem,
        )
        item["elevation_display"] = f"{item['elevation']:.0f} m n.p.m."
        item["display_value"] = _format_value(parameter, value)
        item["tooltip_title"] = f"Komórka — {meta['label']}"
        cells.append(item)

    resolution_m = float((surface.get("metadata") or {}).get("grid_resolution_km", 8.0)) * 1000
    layers: list[pdk.Layer] = [
        pdk.Layer(
            "GeoJsonLayer",
            data=boundary,
            stroked=True,
            filled=True,
            get_fill_color=[7, 18, 30, 12],
            get_line_color=[190, 221, 237, 120],
            line_width_min_pixels=1.1,
            pickable=False,
        ),
        pdk.Layer(
            "ColumnLayer",
            data=cells,
            get_position="[longitude, latitude]",
            radius=resolution_m * 0.40,
            disk_resolution=24 if view_3d else 32,
            extruded=view_3d,
            get_fill_color="display_color",
            get_elevation="elevation",
            elevation_scale=height_scale,
            pickable=True,
            auto_highlight=True,
            opacity=0.78 if view_3d else 0.52,
        ),
    ]

    stations = []
    if show_stations:
        selected_station_id = int((selected_forecast or {}).get("nearest_station_id") or -1)
        for row in surface.get("stations") or []:
            item = dict(row)
            is_selected = int(item.get("station_id") or -2) == selected_station_id
            item["radius"] = 8500 if is_selected else 3500
            item["station_color"] = (
                [255, 246, 178, 245] if is_selected else [240, 248, 255, 155]
            )
            station_name = str(
                item.get("station_name") or item.get("city_name") or "stacja"
            )
            item["tooltip_title"] = f"Stacja — {station_name}"
            item["display_value"] = _format_value(
                parameter,
                (
                    float(item["predicted_value"])
                    if item.get("predicted_value") is not None
                    else None
                ),
            )
            station_elevation = _terrain_elevation_m(
                float(item["latitude"]),
                float(item["longitude"]),
                terrain_dem,
            )
            # Ta sama transformacja Z co dla walca pod stacją. Marker leży
            # na rzeczywistej powierzchni, zamiast na wspólnym pułapie nad
            # najwyższym punktem Polski.
            item["position_z"] = (
                station_elevation * height_scale if view_3d else 0.0
            )
            item["elevation_display"] = f"{station_elevation:.0f} m n.p.m."
            item["confidence"] = "wartość stacyjna"
            item["nearest_station_distance_km"] = "0.00"
            item["stations_used"] = 1
            item["quality_flag"] = "Opublikowana prognoza dla stacji"
            stations.append(item)
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=stations,
                get_position="[longitude, latitude, position_z]",
                get_radius="radius",
                get_fill_color="station_color",
                get_line_color=[5, 15, 25, 235],
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
                parameters={"depthTest": False},
            )
        )
        selected_station_labels = []
        for row in stations:
            if int(row.get("station_id") or -2) != selected_station_id:
                continue
            label = str(row.get("station_name") or row.get("city_name") or "stacja")
            layout = _choose_label_offset(
                text=label,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                font_size=19,
                candidates=[(0, 19), (0, -24), (82, 7), (-82, 7), (74, 24), (-74, 24)],
                reserved_boxes=reserved_label_boxes,
                center_latitude=view_latitude,
                center_longitude=view_longitude,
                zoom=view_zoom,
                collision_padding=7,
            )
            pixel_offset = [0, 19]
            if layout is not None:
                pixel_offset, bounds = layout
                reserved_label_boxes.append(bounds)
            selected_station_labels.append(
                {**row, "label": label, "pixel_offset": pixel_offset}
            )
        if selected_station_labels:
            layers.append(
                pdk.Layer(
                    "TextLayer",
                    data=selected_station_labels,
                    # Tekst jest nakładką ekranową zakotwiczoną wyłącznie
                    # geograficznie. Nie dziedziczy skrajnej skali Z walców.
                    get_position="[longitude, latitude]",
                    get_text="label",
                    get_size=19,
                    get_color=[255, 255, 255, 255],
                    get_text_anchor="'middle'",
                    get_alignment_baseline="'center'",
                    get_pixel_offset="pixel_offset",
                    billboard=True,
                    font_family="Arial, sans-serif",
                    character_set=POLISH_MAP_CHARACTER_SET,
                    font_settings={"sdf": True, "fontSize": 128, "buffer": 8, "radius": 12, "cutoff": 0.25},
                    outline_width=0.28,
                    outline_color=[0, 0, 0, 255],
                    pickable=False,
                    parameters={"depthTest": False},
                )
            )

    city_rows: list[dict[str, Any]] = []
    if show_city_names:
        city_rows = _declutter_city_labels(
            places,
            selected_place=selected_place,
            center_latitude=view_latitude,
            center_longitude=view_longitude,
            zoom=view_zoom,
            density=city_label_density,
            reserved_boxes=reserved_label_boxes,
        )
        for row in city_rows:
            city_elevation = _terrain_elevation_m(
                float(row["latitude"]),
                float(row["longitude"]),
                terrain_dem,
            )
            row["terrain_elevation_m"] = city_elevation
        layers.append(
            pdk.Layer(
                "TextLayer",
                data=city_rows,
                # Warstwa napisów działa jak etykiety kartograficzne, a nie
                # obiekt 3D. Dzięki temu skala 0–100 nie może wypchnąć tekstu
                # poza bryłę widoku kamery.
                get_position="[longitude, latitude]",
                get_text="name",
                get_size="font_size",
                get_color=[255, 255, 255, 255],
                get_text_anchor="'middle'",
                get_alignment_baseline="'center'",
                get_pixel_offset="pixel_offset",
                billboard=True,
                font_family="Arial, sans-serif",
                character_set=POLISH_MAP_CHARACTER_SET,
                font_settings={"sdf": True, "fontSize": 128, "buffer": 8, "radius": 12, "cutoff": 0.25},
                pickable=False,
                outline_width=0.30,
                outline_color=[0, 0, 0, 255],
                # Etykiety są warstwą ekranową ponad danymi. Samo wysokie Z
                # nie wystarcza przy dużym nachyleniu kamery, ponieważ walce
                # mogą wygrać test głębokości fragmentów WebGL.
                parameters={"depthTest": False},
            )
        )

    if selected_forecast and selected_forecast.get("cell_latitude") is not None:
        grid_latitude = float(selected_forecast["cell_latitude"])
        grid_longitude = float(selected_forecast["cell_longitude"])
        grid_elevation = _terrain_elevation_m(
            grid_latitude,
            grid_longitude,
            terrain_dem,
        )
        cell_marker = [
            {
                "longitude": grid_longitude,
                "latitude": grid_latitude,
                "label_z": grid_elevation * height_scale if view_3d else 0.0,
                "label": "punkt siatki",
                "pixel_offset": cell_label_offset,
                "tooltip_title": "Punkt siatki interpolacyjnej",
                "display_value": _format_value(
                    parameter,
                    selected_forecast.get("predicted_value"),
                ),
                "elevation_display": f"{grid_elevation:.0f} m n.p.m.",
                "confidence": selected_forecast.get("confidence") or "—",
                "nearest_station_distance_km": (
                    selected_forecast.get("nearest_station_distance_km") or "—"
                ),
                "stations_used": selected_forecast.get("stations_used") or "—",
                "quality_flag": "Komórka referencyjna powierzchni",
            }
        ]
        layers.extend(
            [
                pdk.Layer(
                    "ScatterplotLayer",
                    data=cell_marker,
                    get_position="[longitude, latitude, label_z]",
                    get_radius=5600,
                    get_fill_color=[255, 201, 80, 245],
                    get_line_color=[30, 20, 5, 255],
                    line_width_min_pixels=2,
                    stroked=True,
                    pickable=True,
                    parameters={"depthTest": False},
                ),
                pdk.Layer(
                    "TextLayer",
                    data=cell_marker,
                    get_position="[longitude, latitude]",
                    get_text="label",
                    get_size=17,
                    get_color=[255, 255, 255, 255],
                    get_pixel_offset="pixel_offset",
                    billboard=True,
                    font_family="Arial, sans-serif",
                    character_set=POLISH_MAP_CHARACTER_SET,
                    font_settings={"sdf": True, "fontSize": 128, "buffer": 8, "radius": 12, "cutoff": 0.25},
                    outline_width=0.30,
                    outline_color=[0, 0, 0, 255],
                    parameters={"depthTest": False},
                ),
            ]
        )

    if selected_place:
        place_latitude = float(selected_place["latitude"])
        place_longitude = float(selected_place["longitude"])
        place_elevation = _terrain_elevation_m(
            place_latitude,
            place_longitude,
            terrain_dem,
        )
        marker = [
            {
                **selected_place,
                "label_z": place_elevation * height_scale if view_3d else 0.0,
                "pixel_offset": selected_place_offset,
                "tooltip_title": (
                    "Dokładny punkt — "
                    + str(selected_place.get("label") or selected_place.get("name") or "punkt")
                ),
                "display_value": _format_value(
                    parameter,
                    (selected_forecast or {}).get("predicted_value"),
                ),
                "elevation_display": f"{place_elevation:.0f} m n.p.m.",
                "confidence": (selected_forecast or {}).get("confidence") or "—",
                "nearest_station_distance_km": (
                    (selected_forecast or {}).get("nearest_station_distance_km") or "—"
                ),
                "stations_used": (selected_forecast or {}).get("stations_used") or "—",
                "quality_flag": "Dokładny punkt zapytania",
            }
        ]
        layers.extend(
            [
                pdk.Layer(
                    "ScatterplotLayer",
                    data=marker,
                    get_position="[longitude, latitude, label_z]",
                    get_radius=22000,
                    get_fill_color=[57, 232, 196, 35],
                    get_line_color=[90, 255, 225, 210],
                    line_width_min_pixels=2,
                    stroked=True,
                    parameters={"depthTest": False},
                ),
                pdk.Layer(
                    "ScatterplotLayer",
                    data=marker,
                    get_position="[longitude, latitude, label_z]",
                    get_radius=7200,
                    get_fill_color=[255, 255, 255, 250],
                    get_line_color=[43, 225, 193, 255],
                    line_width_min_pixels=4,
                    stroked=True,
                    pickable=True,
                    parameters={"depthTest": False},
                ),
                pdk.Layer(
                    "TextLayer",
                    data=marker,
                    get_position="[longitude, latitude]",
                    get_text="label",
                    get_size=25,
                    get_color=[255, 255, 255, 255],
                    get_text_anchor="'middle'",
                    get_alignment_baseline="'center'",
                    get_pixel_offset="pixel_offset",
                    billboard=True,
                    font_family="Arial, sans-serif",
                    character_set=POLISH_MAP_CHARACTER_SET,
                    font_settings={"sdf": True, "fontSize": 128, "buffer": 8, "radius": 12, "cutoff": 0.25},
                    outline_width=0.34,
                    outline_color=[0, 0, 0, 255],
                    parameters={"depthTest": False},
                ),
            ]
        )

    view_state = pdk.ViewState(
        latitude=view_latitude,
        longitude=view_longitude,
        zoom=view_zoom,
        pitch=42 if view_3d else 0,
        bearing=-7 if view_3d else 0,
        min_zoom=4,
        max_zoom=12,
    )
    tooltip = {
        "html": (
            "<b>{tooltip_title}</b><br/>"
            "Wartość: {display_value}<br/>"
            "Wysokość terenu: {elevation_display}<br/>"
            "Pewność: {confidence}<br/>"
            "Odległość od najbliższej stacji: {nearest_station_distance_km} km<br/>"
            "Stacje użyte: {stations_used}<br/>"
            "<span style='color:#b6c6d4'>{quality_flag}</span>"
        ),
        "style": {
            "backgroundColor": "#0b1b2c",
            "color": "white",
            "borderRadius": "12px",
        },
    }
    return pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        # Bazowa mapa zachowuje własne etykiety CARTO. Nad walcami znajduje się
        # dodatkowa, kontrolowana warstwa najważniejszych polskich miast, aby
        # nazwy pozostały czytelne także na intensywnych kolorach danych.
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        tooltip=tooltip,
    )



def _timeline_local_times(rows: list[dict[str, Any]]) -> list[pd.Timestamp]:
    if not rows:
        return []
    raw = pd.Series(
        [row.get("target_time") for row in rows],
        dtype="object",
    )
    parsed = pd.to_datetime(raw, utc=True, errors="coerce").dropna()
    if parsed.empty:
        return []
    local = parsed.dt.tz_convert(DISPLAY_TIMEZONE)
    unique = {
        timestamp.isoformat(): timestamp
        for timestamp in local.tolist()
    }
    return sorted(unique.values())


def _timeline_focus_time(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).tz_convert(DISPLAY_TIMEZONE)


def _floor_available_time(
    values: list[pd.Timestamp],
    requested: pd.Timestamp,
) -> pd.Timestamp:
    candidates = [value for value in values if value <= requested]
    return candidates[-1] if candidates else values[0]


def _ceil_available_time(
    values: list[pd.Timestamp],
    requested: pd.Timestamp,
) -> pd.Timestamp:
    candidates = [value for value in values if value >= requested]
    return candidates[0] if candidates else values[-1]


def _timeline_range_control(
    rows: list[dict[str, Any]],
    *,
    focus_time: Any,
    place_name: str | None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Render one synchronized two-handle time selector for both detail charts."""

    available = _timeline_local_times(rows)
    if not available:
        return None, None
    if len(available) == 1:
        return available[0], available[0]

    focus = _timeline_focus_time(focus_time)
    if focus is None:
        focus = available[len(available) // 2]
    focus = min(max(focus, available[0]), available[-1])

    half_window = pd.Timedelta(int(DEFAULT_TIMELINE_WINDOW_HOURS * 30), unit="m")  # HF21_DASHBOARD_TIMEDELTA_EXPLICIT_UNIT_V2
    default_start = _floor_available_time(available, focus - half_window)
    default_end = _ceil_available_time(available, focus + half_window)
    if default_start >= default_end:
        default_start, default_end = available[0], available[-1]

    options = [timestamp.isoformat() for timestamp in available]
    labels = {
        timestamp.isoformat(): timestamp.strftime("%d.%m %H:%M")
        for timestamp in available
    }
    key_material = "|".join(
        [
            str(place_name or "point"),
            focus.isoformat(),
            options[0],
            options[-1],
        ]
    )
    key = "timeline_range_" + hashlib.sha1(
        key_material.encode("utf-8")
    ).hexdigest()[:14]

    selected = st.select_slider(
        "Zakres godzinowy obu wykresów",
        options=options,
        value=(default_start.isoformat(), default_end.isoformat()),
        format_func=lambda value: labels.get(str(value), str(value)),
        key=key,
        help=(
            "Przeciągnij lewy i prawy uchwyt. Zakres jest wspólny dla wykresu "
            "PM10/PM2.5 oraz temperatury i opadu. Zmiana nie pobiera danych "
            "ponownie — profil jest już zapisany w pamięci podręcznej."
        ),
    )
    start = pd.Timestamp(pd.to_datetime(selected[0], utc=True)).tz_convert(
        DISPLAY_TIMEZONE
    )
    end = pd.Timestamp(pd.to_datetime(selected[1], utc=True)).tz_convert(
        DISPLAY_TIMEZONE
    )
    if start > end:
        start, end = end, start

    points = sum(start <= value <= end for value in available)
    duration = max(0.0, (end - start).total_seconds() / 3600.0)
    st.caption(
        "Widoczny zakres: "
        f"{start.strftime('%d.%m %H:%M')} – {end.strftime('%d.%m %H:%M')} "
        f"({duration:.0f} h, {points} punktów godzinowych). "
        "Możesz też przeciągać miniaturowy suwak pod każdym wykresem, "
        "przybliżać kółkiem myszy i używać przycisków 6/12/24 h."
    )
    return start, end


def _timeline_xaxis(
    range_start: pd.Timestamp | None,
    range_end: pd.Timestamp | None,
) -> dict[str, Any]:
    axis: dict[str, Any] = {
        "type": "date",
        "gridcolor": "rgba(255,255,255,.08)",
        "rangeslider": {
            "visible": True,
            "thickness": 0.12,
            "bgcolor": "rgba(7,19,31,.70)",
            "bordercolor": "rgba(122,184,255,.30)",
            "borderwidth": 1,
        },
        "rangeselector": {
            "buttons": [
                {"count": 6, "label": "6 h", "step": "hour", "stepmode": "backward"},
                {"count": 12, "label": "12 h", "step": "hour", "stepmode": "backward"},
                {"count": 24, "label": "24 h", "step": "hour", "stepmode": "backward"},
                {"step": "all", "label": "Całość"},
            ],
            "bgcolor": "rgba(12,33,54,.92)",
            "activecolor": "rgba(67,224,192,.52)",
            "bordercolor": "rgba(154,199,225,.28)",
            "borderwidth": 1,
            "font": {"color": "#dcebf5", "size": 11},
            "x": 0.0,
            "y": 1.16,
        },
    }
    if range_start is not None and range_end is not None:
        axis["range"] = [range_start.isoformat(), range_end.isoformat()]
    return axis


def _add_focus_line(figure: go.Figure, focus_time: Any) -> None:
    focus = _timeline_focus_time(focus_time)
    if focus is None:
        return
    figure.add_vline(
        x=focus.isoformat(),
        line_width=2,
        line_dash="dot",
        line_color="#43e0c0",
    )


def _pm_timeline(
    rows: list[dict[str, Any]],
    place_name: str | None = None,
    *,
    range_start: pd.Timestamp | None = None,
    range_end: pd.Timestamp | None = None,
    focus_time: Any = None,
) -> go.Figure | None:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return None
    frame = frame[frame["parameter"].isin(["PM10", "PM2.5"])].copy()
    if frame.empty:
        return None
    frame["target_time"] = pd.to_datetime(
        frame["target_time"], utc=True, errors="coerce"
    ).dt.tz_convert(DISPLAY_TIMEZONE)
    figure = go.Figure()
    for parameter, group in frame.groupby("parameter"):
        group = group.sort_values("target_time")
        figure.add_trace(
            go.Scatter(
                x=group["target_time"],
                y=group["value"],
                mode="lines+markers",
                name=str(parameter),
                hovertemplate="%{x|%d.%m %H:%M}<br>%{y:.1f} µg/m³<extra></extra>",
            )
        )
    figure.update_layout(
        height=330,
        title=(
            f"Profil godzinowy pyłów — {place_name}"
            if place_name
            else "Profil godzinowy pyłów"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#dcebf5"},
        margin={"l": 15, "r": 15, "t": 78, "b": 15},
        yaxis={"title": "µg/m³", "gridcolor": "rgba(255,255,255,.08)"},
        xaxis=_timeline_xaxis(range_start, range_end),
        legend={"orientation": "h"},
        hovermode="x unified",
        dragmode="pan",
    )
    _add_focus_line(figure, focus_time)
    return figure


def _weather_timeline(
    rows: list[dict[str, Any]],
    place_name: str | None = None,
    *,
    range_start: pd.Timestamp | None = None,
    range_end: pd.Timestamp | None = None,
    focus_time: Any = None,
) -> go.Figure | None:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return None
    frame = frame[
        frame["parameter"].isin(
            ["temperature_c", "precipitation_mm", "precipitation_probability"]
        )
    ].copy()
    if frame.empty:
        return None
    frame["target_time"] = pd.to_datetime(
        frame["target_time"], utc=True, errors="coerce"
    ).dt.tz_convert(DISPLAY_TIMEZONE)
    figure = go.Figure()
    temperature = frame[frame["parameter"] == "temperature_c"].sort_values("target_time")
    rain = frame[frame["parameter"] == "precipitation_mm"].sort_values("target_time")
    probability = frame[frame["parameter"] == "precipitation_probability"].sort_values(
        "target_time"
    )
    if not temperature.empty:
        figure.add_trace(
            go.Scatter(
                x=temperature["target_time"],
                y=temperature["value"],
                mode="lines+markers",
                name="Temperatura",
                line={"width": 3},
                yaxis="y",
                hovertemplate="%{x|%d.%m %H:%M}<br>%{y:.1f} °C<extra></extra>",
            )
        )
    if not rain.empty:
        figure.add_trace(
            go.Bar(
                x=rain["target_time"],
                y=rain["value"],
                name="Opad",
                yaxis="y2",
                opacity=0.55,
                hovertemplate="%{x|%d.%m %H:%M}<br>%{y:.2f} mm/okres<extra></extra>",
            )
        )
    if not probability.empty:
        figure.add_trace(
            go.Scatter(
                x=probability["target_time"],
                y=probability["value"] * 100,
                mode="lines",
                name="Prawdopodobieństwo opadu",
                yaxis="y3",
                line={"dash": "dot"},
                hovertemplate="%{x|%d.%m %H:%M}<br>%{y:.0f}%<extra></extra>",
            )
        )
    figure.update_layout(
        height=350,
        title=(
            f"Temperatura i opad — {place_name}"
            if place_name
            else "Prognoza temperatury i opadu"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#dcebf5"},
        margin={"l": 15, "r": 65, "t": 78, "b": 15},
        yaxis={"title": "°C", "gridcolor": "rgba(255,255,255,.08)"},
        yaxis2={"title": "mm/okres", "overlaying": "y", "side": "right", "showgrid": False},
        yaxis3={
            "title": "%",
            "overlaying": "y",
            "side": "right",
            "position": 0.94,
            "range": [0, 100],
            "showgrid": False,
        },
        xaxis=_timeline_xaxis(range_start, range_end),
        legend={"orientation": "h"},
        barmode="overlay",
        hovermode="x unified",
        dragmode="pan",
    )
    _add_focus_line(figure, focus_time)
    return figure



# HF21_DASHBOARD_PARAMETER_KEYS_V1
def _ui_parameter_key(value: Any) -> str:
    raw = str(value or "").strip()
    aliases = {
        "PM10": "PM10",
        "PM2.5": "PM2.5",
        "TEMPERATURE_C": "temperature_c",
        "PRECIPITATION_MM": "precipitation_mm",
        "PRECIPITATION_PROBABILITY": "precipitation_probability",
        "PM25": "PM2.5",
    }
    return aliases.get(raw.upper(), raw)


def _normalise_query_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Canonicalise and de-duplicate forecast parameters at the UI boundary."""

    if not result:
        return result
    output = dict(result)
    canonical: dict[str, dict[str, Any]] = {}
    for raw_row in result.get("forecasts") or []:
        row = dict(raw_row)
        parameter = _ui_parameter_key(row.get("parameter"))
        row["parameter"] = parameter
        current = canonical.get(parameter)
        if current is None or (
            current.get("predicted_value") is None
            and row.get("predicted_value") is not None
        ):
            canonical[parameter] = row
    preferred_order = (
        "PM10",
        "PM2.5",
        "temperature_c",
        "precipitation_probability",
        "precipitation_mm",
    )
    output["forecasts"] = [
        canonical[parameter]
        for parameter in preferred_order
        if parameter in canonical
    ] + [
        row for parameter, row in canonical.items() if parameter not in preferred_order
    ]
    return output


def _render_forecast_cards(result: dict[str, Any] | None) -> None:
    if not result:
        st.info("Wpisz pytanie, aby zobaczyć wartości dla wybranego miasta.")
        return
    forecasts = {
        _ui_parameter_key(row.get("parameter")): row
        for row in result.get("forecasts", [])
    }
    parameters = [
        parameter for parameter in QUERY_PARAMETER_OPTIONS if parameter in forecasts
    ]
    if not parameters:
        st.warning("API nie zwróciło żadnego z wybranych parametrów.")
        return
    columns = st.columns(len(parameters))
    for column, parameter in zip(columns, parameters, strict=True):
        row = forecasts.get(parameter) or {}
        meta = _parameter_meta(parameter)
        value = row.get("predicted_value")
        if row.get("unit"):
            meta = {**meta, "unit": str(row.get("unit"))}
        exact = bool(row.get("exact_time_match"))
        sub = (
            f"{_local_time(row.get('target_time'))} · "
            f"h={row.get('horizon_hours', '—')} · "
            f"{'dokładnie' if exact else 'brak dokładnego pakietu'}"
        )
        column.markdown(
            f"""
<div class="info-card">
  <div class="label">{meta['label']}</div>
  <div class="value">{_format_value(parameter, value)}</div>
  <div class="sub">{sub}</div>
</div>
""",
            unsafe_allow_html=True,
        )


def _station_influence_graph(
    contributions: list[dict[str, Any]],
    *,
    point_name: str,
    point_latitude: float | None,
    point_longitude: float | None,
    parameter: str,
) -> go.Figure | None:
    if point_latitude is None or point_longitude is None:
        return None

    rows: list[dict[str, Any]] = []
    for item in contributions:
        try:
            weight = max(0.0, float(item.get("normalized_weight") or 0.0))
            station_latitude = float(item["latitude"])
            station_longitude = float(item["longitude"])
            distance_km = float(item["distance_km"])
        except (TypeError, ValueError):
            continue
        except KeyError:
            continue

        latitude_1 = math.radians(float(point_latitude))
        latitude_2 = math.radians(station_latitude)
        longitude_delta = math.radians(station_longitude - float(point_longitude))
        bearing = math.atan2(
            math.sin(longitude_delta) * math.cos(latitude_2),
            math.cos(latitude_1) * math.sin(latitude_2)
            - math.sin(latitude_1)
            * math.cos(latitude_2)
            * math.cos(longitude_delta),
        )
        bearing_degrees = (math.degrees(bearing) + 360.0) % 360.0
        rows.append(
            {
                **item,
                "_weight": weight,
                "_distance_km": distance_km,
                # Rzut lokalny zachowujący dokładną odległość z IDW oraz
                # rzeczywisty azymut wyliczony ze współrzędnych WGS84.
                "_east_km": distance_km * math.sin(bearing),
                "_north_km": distance_km * math.cos(bearing),
                "_bearing_degrees": bearing_degrees,
            }
        )
    if not rows:
        return None

    rows.sort(key=lambda item: item["_weight"], reverse=True)
    maximum_weight = max(float(item["_weight"]) for item in rows) or 1.0
    figure = go.Figure()
    station_x: list[float] = []
    station_y: list[float] = []
    station_size: list[float] = []
    station_text: list[str] = []
    station_hover: list[str] = []
    station_text_position: list[str] = []
    for index, item in enumerate(rows):
        x = float(item["_east_km"])
        y = float(item["_north_km"])
        weight = item["_weight"]
        relative_weight = max(0.0, min(1.0, weight / maximum_weight))
        station_x.append(x)
        station_y.append(y)
        station_size.append(11.0 + 45.0 * relative_weight**1.35)
        station_text.append(str(item.get("station_name") or f"Stacja {index + 1}"))
        station_text_position.append("top center" if y >= 0.0 else "bottom center")
        distance = item["_distance_km"]
        prediction = item.get("predicted_value")
        station_hover.append(
            f"<b>{station_text[-1]}</b><br>"
            f"Udział: {weight:.1%}<br>"
            f"Odległość: {float(distance):.2f} km<br>"
            f"Azymut: {float(item['_bearing_degrees']):.1f}°<br>"
            f"Współrzędne: {float(item['latitude']):.5f}, "
            f"{float(item['longitude']):.5f}<br>"
            f"Prognoza stacji: {float(prediction):.3f}"
            if prediction is not None
            else f"<b>{station_text[-1]}</b><br>Udział: {weight:.1%}"
        )
        figure.add_trace(
            go.Scatter(
                x=[0.0, x],
                y=[0.0, y],
                mode="lines",
                line={
                    "width": 0.8 + 22.0 * relative_weight**1.55,
                    "color": "rgba(72, 187, 255, 0.68)",
                },
                hoverinfo="skip",
                showlegend=False,
            )
        )

    figure.add_trace(
        go.Scatter(
            x=station_x,
            y=station_y,
            mode="markers+text",
            text=station_text,
            textposition=station_text_position,
            marker={
                "size": station_size,
                "color": [item["_weight"] for item in rows],
                "colorscale": "Blues",
                "cmin": 0,
                "cmax": max(item["_weight"] for item in rows) or 1.0,
                "line": {"width": 1.5, "color": "#dff7ff"},
                "showscale": False,
            },
            hovertext=station_hover,
            hoverinfo="text",
            showlegend=False,
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[0.0],
            y=[0.0],
            mode="markers+text",
            text=[point_name],
            textposition="bottom center",
            marker={"size": 30, "symbol": "diamond", "color": "#39e6c4", "line": {"width": 2, "color": "#ffffff"}},
            hovertext=[f"Dokładny punkt interpolacji<br>{point_name}"],
            hoverinfo="text",
            showlegend=False,
        )
    )
    maximum_extent = max(
        1.0,
        *(abs(value) for value in station_x),
        *(abs(value) for value in station_y),
    )
    plot_extent = maximum_extent * 1.28
    figure.update_layout(
        title=(
            f"Geograficzny graf stacji IDW — {_parameter_meta(parameter)['label']}"
            "<br><sup>Położenie i długość linii są w skali kilometrów; "
            "grubość linii oznacza wagę IDW</sup>"
        ),
        height=620,
        margin={"l": 70, "r": 40, "t": 90, "b": 65},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis={
            "title": "Odległość wschód–zachód [km]",
            "range": [-plot_extent, plot_extent],
            "showgrid": True,
            "gridcolor": "rgba(150,180,205,0.20)",
            "zeroline": True,
            "zerolinecolor": "rgba(220,235,245,0.55)",
        },
        yaxis={
            "title": "Odległość północ–południe [km]",
            "range": [-plot_extent, plot_extent],
            "showgrid": True,
            "gridcolor": "rgba(150,180,205,0.20)",
            "zeroline": True,
            "zerolinecolor": "rgba(220,235,245,0.55)",
            "scaleanchor": "x",
            "scaleratio": 1,
        },
        hoverlabel={"bgcolor": "#102538", "font_color": "white"},
        annotations=[
            {"x": 0, "y": plot_extent, "text": "N", "showarrow": False, "font": {"size": 18}},
            {"x": plot_extent, "y": 0, "text": "E", "showarrow": False, "font": {"size": 18}},
            {"x": 0, "y": -plot_extent, "text": "S", "showarrow": False, "font": {"size": 18}},
            {"x": -plot_extent, "y": 0, "text": "W", "showarrow": False, "font": {"size": 18}},
        ],
    )
    return figure


def _render_exact_point(result: dict[str, Any] | None, parameter: str, surface: dict[str, Any]) -> None:
    if not result:
        return
    row = next(
        (
            item
            for item in result.get("forecasts", [])
            if _ui_parameter_key(item.get("parameter")) == _ui_parameter_key(parameter)
        ),
        None,
    )
    # HF21_UI_INTEGRITY_HOTFIX_V4
    forecast_missing_for_parameter = row is None
    row = row or {}
    place = result.get("place") or {}
    station = result.get("station") or {}
    metadata = surface.get("metadata") or {}
    experimental = bool(row.get("experimental") or metadata.get("experimental"))
    if experimental:
        st.warning(
            "Wynik eksperymentalny — model nie przeszedł wszystkich miękkich "
            "progów jakości. Aktywny model pozostaje dostępny, ale wynik należy "
            "interpretować ostrożniej."
        )
    contributions = row.get("station_contributions") or []
    nearest_contribution = min(
        (
            item
            for item in contributions
            if item.get("distance_km") is not None
        ),
        key=lambda item: float(item["distance_km"]),
        default=None,
    )
    table = pd.DataFrame(
        [
            (
                "Status wybranego parametru",
                (
                    f"Brak opublikowanej prognozy dla {parameter}; "
                    "punkt lokalizacji nadal jest prawidłowo pokazany."
                    if forecast_missing_for_parameter
                    else "Prognoza dostępna"
                ),
            ),
            (
                "Status jakości modelu",
                "EKSPERYMENTALNY"
                if experimental
                else str(row.get("quality_status") or metadata.get("quality_status") or "accepted"),
            ),
            (
                "Powód statusu eksperymentalnego",
                json.dumps(
                    row.get("experimental_reason")
                    or metadata.get("experimental_reason")
                    or [],
                    ensure_ascii=False,
                ),
            ),
            ("Punkt zapytania", place.get("name")),
            (
                "Dokładny punkt prognozy",
                f"{float(place.get('latitude')):.6f}, {float(place.get('longitude')):.6f}"
                if place.get("latitude") is not None
                else "—",
            ),
            ("Precyzja lokalizacji", place.get("precision") or "—"),
            ("Źródło lokalizacji", place.get("source") or "—"),
            ("Algorytm przestrzenny", row.get("spatial_method") or "—"),
            ("Potęga odległości p", row.get("distance_power") or "—"),
            (
                "Układ metryczny",
                row.get("projected_crs") or metadata.get("projected_crs") or "—",
            ),
            ("Stacje użyte", row.get("stations_used") or "—"),
            (
                "Najbliższa stacja referencyjna",
                station.get("station_name") or "—",
            ),
            (
                "Odległość do stacji referencyjnej",
                f"{float(station.get('distance_km')):.2f} km"
                if station.get("distance_km") is not None
                else "—",
            ),
            (
                "Najbliższa stacja użyta przez IDW",
                nearest_contribution.get("station_name")
                if nearest_contribution
                else "—",
            ),
            (
                "Odległość najbliższej stacji IDW",
                f"{float(nearest_contribution['distance_km']):.2f} km"
                if nearest_contribution
                else (
                    f"{float(row.get('nearest_station_distance_km')):.2f} km"
                    if row.get("nearest_station_distance_km") is not None
                    else "—"
                ),
            ),
            ("Czas bazowy", _local_time(row.get("forecast_origin_time"))),
            ("Czas docelowy", _local_time(row.get("target_time"))),
            ("Metoda czasowa", row.get("temporal_method") or "—"),
            ("Wersja modelu", row.get("model_version") or "—"),
        ],
        columns=["Właściwość", "Wartość"],
    )
    # HF21_DASHBOARD_ARROW_VALUE_STRING: Arrow requires one stable type per column.
    table.iloc[:, 1] = table.iloc[:, 1].map(
        lambda value: "\u2014" if value is None else str(value)
    )
    st.dataframe(table, hide_index=True, width="stretch")
    if contributions:
        contribution_frame = pd.DataFrame(contributions).rename(
            columns={
                "station_name": "Stacja",
                "distance_km": "Odległość [km]",
                "quality_weight": "Jakość q",
                "normalized_weight": "Udział",
                "predicted_value": "Prognoza stacji",
            }
        )
        st.caption("Wkład stacji w interpolację dokładnego punktu")
        st.dataframe(
            contribution_frame[
                ["Stacja", "Odległość [km]", "Jakość q", "Udział", "Prognoza stacji"]
            ],
            hide_index=True,
            width="stretch",
        )
        graph = _station_influence_graph(
            contributions,
            point_name=str(place.get("name") or "Punkt prognozy"),
            point_latitude=(
                float(place["latitude"])
                if place.get("latitude") is not None
                else None
            ),
            point_longitude=(
                float(place["longitude"])
                if place.get("longitude") is not None
                else None
            ),
            parameter=parameter,
        )
        if graph is not None:
            st.caption(
                "Graf zachowuje rzeczywisty kierunek i odległość stacji od punktu. "
                "Grubsza linia i większy punkt oznaczają większy udział w wyniku IDW."
            )
            st.plotly_chart(
                graph,
                width="stretch",
                config=PLOTLY_CHART_CONFIG,
            )


st.markdown(
    """
<div class="hero">
  <div class="pill">GODZINOWA PROGNOZA · DOKŁADNY TARGET_TIME · OPEN MODEL PLATFORM</div>
  <h1>Jakość powietrza i pogoda<br/>dla konkretnej godziny</h1>
  <p>PM10, PM2.5, temperatura i opad są liczone lokalnie dla dokładnych godzin.
  DigitalOcean App Platform odczytuje opublikowane prognozy przez Bridge
  (lokalny katalog albo Spaces) i wykonuje lekkie IDW + PCHIP dla wskazanego punktu.
  Każdy wynik pokazuje czas bazowy, czas docelowy, model i dokładny punkt interpolacji.</p>
</div>
""",
    unsafe_allow_html=True,
)

try:
    health = load_health()
    manifest = load_manifest()
    boundary = load_boundary()
    places = load_places()
except Exception as exc:
    st.error(f"Nie można uruchomić platformy — API albo pakiet mapowy jest niedostępny: {exc}")
    st.code(f"API: {API_URL}")
    st.stop()

# Serving v2 is the source of truth. New targets such as NO2 or O3 become
# selectable without another dashboard patch.
QUERY_PARAMETER_OPTIONS = _manifest_parameter_options(manifest)

map_tab, model_tab, cost_tab, docs_tab = st.tabs(
    [
        "🗺️ Mapa i prognoza",
        "🧠 Modele, MLOps i oceny",
        "💰 Koszty i wykorzystanie",
        "📚 Jak to działa",
    ]
)

with map_tab:
    use_exact_point = st.checkbox(
        "Użyj dokładnych współrzędnych zamiast miejsca z treści zapytania",
        key="use_exact_point",
    )

    # HF21_DASHBOARD_LOCATION_MODEL_UX_V2
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
            width="stretch",
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

    with st.form("natural-language-query", clear_on_submit=False):
        question = st.text_input(
            "Zapytaj o miejsce i termin",
            value=st.session_state.get("question", DEFAULT_QUESTION),
            placeholder="Np. jutro o 17:00 w Krakowie — PM10, temperatura i deszcz",
            label_visibility="collapsed",
        )
        point_name = None
        point_latitude = None
        point_longitude = None

        if use_exact_point:
            st.caption("Prognoza zostanie obliczona dokładnie dla poniższego punktu.")
            name_col, latitude_col, longitude_col = st.columns([2, 1, 1])

            with name_col:
                point_name = st.text_input(
                    "Nazwa punktu",
                    value=st.session_state.get("point_name", "Wybrany punkt"),
                    placeholder="Np. Startowisko Mieroszów",
                )

            with latitude_col:
                point_latitude = st.number_input(
                    "Latitude",
                    min_value=-90.0,
                    max_value=90.0,
                    value=float(st.session_state.get("point_latitude", 50.0)),
                    format="%.6f",
                )

            with longitude_col:
                point_longitude = st.number_input(
                    "Longitude",
                    min_value=-180.0,
                    max_value=180.0,
                    value=float(st.session_state.get("point_longitude", 19.0)),
                    format="%.6f",
                )

        submitted = st.form_submit_button(
            "Sprawdź",
            type="primary",
            width="stretch",
        )

    if submitted:
        with st.spinner("Rozpoznaję miejsce, termin i parametry…"):
            try:
                query_payload = {"text": question}
                if use_exact_point:
                    query_payload.update(
                        {
                            "latitude": point_latitude,
                            "longitude": point_longitude,
                            "place_name": point_name,
                            "location_source": "exact_coordinates",
                        }
                    )
                result = _request_json(
                    "query/preview",
                    method="POST",
                    payload=query_payload,
                    timeout=QUERY_TIMEOUT_SECONDS,
                )
                result = _normalise_query_result(result) or {}
                proposed_parameters = [
                    parameter
                    for parameter in QUERY_PARAMETER_OPTIONS
                    if parameter
                    in {
                        _ui_parameter_key(value)
                        for value in result.get("proposed_parameters") or []
                    }
                ]
                st.session_state["pending_query_confirmation"] = {
                    "result": result,
                    "question": question,
                    "parameters": proposed_parameters or ["PM10", "PM2.5"],
                }
                st.session_state.pop("query_result", None)
                st.session_state["question"] = question
                if use_exact_point:
                    st.session_state["point_name"] = point_name
                    st.session_state["point_latitude"] = point_latitude
                    st.session_state["point_longitude"] = point_longitude
                st.session_state.pop("timeline_error", None)
            except Exception as exc:
                st.error(f"Nie udało się zinterpretować zapytania: {exc}")

    # HF21_OPENAI_COORDINATE_TIME_CONFIRMATION_V1
    pending_confirmation = st.session_state.get("pending_query_confirmation")
    if pending_confirmation:
        pending_result = pending_confirmation.get("result") or {}
        pending_intent = pending_result.get("intent") or {}
        pending_place = pending_result.get("place") or {}
        location_check = pending_result.get("location_validation") or {}
        time_check = pending_result.get("time_validation") or {}
        coordinate_candidate = location_check.get("candidate") or {}
        reference = location_check.get("reference") or {}
        proposed_parameters = [
            parameter
            for parameter in QUERY_PARAMETER_OPTIONS
            if parameter
            in {
                _ui_parameter_key(value)
                for value in (
                    pending_result.get("proposed_parameters")
                    or pending_confirmation.get("parameters")
                    or pending_intent.get("pollutants")
                    or []
                )
            }
        ] or ["PM10", "PM2.5"]

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
        threshold = float(location_check.get("automatic_acceptance_threshold_km") or 1.0)

        openai_time = str(
            time_check.get("candidate_target_time")
            or pending_intent.get("target_time")
            or ""
        )
        parser_time = str(time_check.get("reference_target_time") or "")
        time_difference = time_check.get("difference_minutes")
        location_confirmation_required = bool(
            location_check.get("confirmation_required")
        )
        time_confirmation_required = bool(time_check.get("confirmation_required"))

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
                ",".join(proposed_parameters),
            ]
        )
        confirmation_defaults = {
            "hf21_confirmation_name": (
                reference_name if recommend_reference else candidate_name
            ),
            "hf21_confirmation_latitude": (
                reference_latitude if recommend_reference else candidate_latitude
            ),
            "hf21_confirmation_longitude": (
                reference_longitude if recommend_reference else candidate_longitude
            ),
            "hf21_confirmation_time": (
                parser_time if recommend_parser_time else openai_time
            ),
            "hf21_confirmation_location_source": (
                "confirmed_independent_reference"
                if recommend_reference else "confirmed_openai_candidate"
            ),
            "hf21_confirmation_time_source": (
                "confirmed_deterministic_parser"
                if recommend_parser_time else "confirmed_openai_candidate"
            ),
            "hf21_confirmation_parameters": list(proposed_parameters),
        }
        for state_key, default_value in confirmation_defaults.items():
            st.session_state.setdefault(state_key, default_value)
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
            st.session_state["hf21_confirmation_parameters"] = list(
                proposed_parameters
            )

        if location_confirmation_required and time_confirmation_required:
            st.warning(
                "Lokalizacja i czas wymagają potwierdzenia. Porównaj źródła "
                "przed obliczeniem prognozy."
            )
        elif location_confirmation_required:
            st.warning(
                "Lokalizacja wymaga potwierdzenia. Czas został przyjęty automatycznie."
            )
        elif time_confirmation_required:
            st.warning(
                "Czas wymaga potwierdzenia. Lokalizacja została przyjęta automatycznie."
            )
        else:
            st.info(
                "OpenAI i źródła kontrolne są zgodne. Sprawdź lub zmień "
                "nazwę, współrzędne, termin i parametry przed obliczeniem prognozy."
            )

        st.markdown("### Sprawdź i doprecyzuj punkt na mapie")
        st.caption(
            "Znacznik pokazuje punkt proponowany do obliczeń. Kliknij inne miejsce "
            "na mapie, aby natychmiast przepisać jego współrzędne do formularza."
        )
        review_latitude = float(st.session_state["hf21_confirmation_latitude"])
        review_longitude = float(st.session_state["hf21_confirmation_longitude"])
        review_map = folium.Map(
            location=[review_latitude, review_longitude],
            zoom_start=13,
            tiles="OpenStreetMap",  # HF21_LOCATION_REVIEW_LIGHT_MAP
            control_scale=True,
        )
        folium.Marker(
            [review_latitude, review_longitude],
            tooltip=(
                f"{st.session_state['hf21_confirmation_name']} — "
                f"{review_latitude:.6f}, {review_longitude:.6f}"
            ),
            icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
        ).add_to(review_map)
        folium.Circle(
            [review_latitude, review_longitude],
            radius=1000,
            color="#38e8c4",
            fill=False,
            weight=2,
            tooltip="Promień kontrolny 1 km",
        ).add_to(review_map)
        review_event = st_folium(
            review_map,
            key=(
                "hf21-confirmation-map-"
                + hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()[:16]
            ),
            height=480,
            width="stretch",
            returned_objects=["last_clicked"],
        )
        review_click = (review_event or {}).get("last_clicked") or {}
        if review_click.get("lat") is not None and review_click.get("lng") is not None:
            clicked_latitude = float(review_click["lat"])
            clicked_longitude = float(review_click["lng"])
            if (
                abs(clicked_latitude - review_latitude) > 1e-7
                or abs(clicked_longitude - review_longitude) > 1e-7
            ):
                st.session_state["hf21_confirmation_latitude"] = clicked_latitude
                st.session_state["hf21_confirmation_longitude"] = clicked_longitude
                st.session_state["hf21_confirmation_location_source"] = (
                    "confirmed_map_click"
                )
                st.rerun()

        # HF21_NONE_DISTANCE_HOTFIX_V1
        if not location_confirmation_required:
            st.success(
                f"OpenAI i {reference_source} wskazują zgodny punkt"
                + (
                    f" (różnica {float(distance):.3f} km)."
                    if distance is not None
                    else "."
                )
                + " Lokalizacja nie wymaga zatwierdzenia."
            )
        elif recommend_reference:
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
        if location_confirmation_required:
            st.markdown("### Porównanie lokalizacji")
            st.dataframe(
                pd.DataFrame(location_rows),
                hide_index=True,
                width="stretch",
            )

            source_buttons = st.columns(2)
            with source_buttons[0]:
                if st.button("Użyj propozycji OpenAI", width="stretch"):
                    st.session_state["hf21_confirmation_name"] = candidate_name
                    st.session_state["hf21_confirmation_latitude"] = candidate_latitude
                    st.session_state["hf21_confirmation_longitude"] = candidate_longitude
                    st.session_state["hf21_confirmation_location_source"] = "confirmed_openai_candidate"
                    st.rerun()
            with source_buttons[1]:
                if st.button(
                    f"Użyj punktu {reference_source}",
                    disabled=not reference_available,
                    width="stretch",
                ):
                    st.session_state["hf21_confirmation_name"] = reference_name
                    st.session_state["hf21_confirmation_latitude"] = reference_latitude
                    st.session_state["hf21_confirmation_longitude"] = reference_longitude
                    st.session_state["hf21_confirmation_location_source"] = "confirmed_independent_reference"
                    st.rerun()

        if time_confirmation_required:
            st.markdown("### Porównanie czasu")
            time_rows = [
                {"Źródło": "OpenAI — propozycja", "Termin ISO 8601": openai_time},
            ]
            if parser_time:
                time_rows.append(
                    {"Źródło": "Deterministyczny parser czasu", "Termin ISO 8601": parser_time}
                )
            st.dataframe(pd.DataFrame(time_rows), hide_index=True, width="stretch")
            if time_difference is not None:
                st.caption(f"Różnica między parserami: {float(time_difference):.2f} min.")

            time_buttons = st.columns(2)
            with time_buttons[0]:
                if st.button("Użyj czasu OpenAI", width="stretch"):
                    st.session_state["hf21_confirmation_time"] = openai_time
                    st.session_state["hf21_confirmation_time_source"] = "confirmed_openai_candidate"
                    st.rerun()
            with time_buttons[1]:
                if st.button(
                    "Użyj czasu parsera kontrolnego",
                    disabled=not bool(parser_time),
                    width="stretch",
                ):
                    st.session_state["hf21_confirmation_time"] = parser_time
                    st.session_state["hf21_confirmation_time_source"] = "confirmed_deterministic_parser"
                    st.rerun()
        else:
            st.success("OpenAI i parser kontrolny wskazują zgodny czas. Czas nie wymaga zatwierdzenia.")

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
            st.multiselect(
                "Parametry do obliczenia i wyświetlenia",
                options=list(QUERY_PARAMETER_OPTIONS),
                format_func=lambda value: _parameter_option_label(value, manifest),
                key="hf21_confirmation_parameters",
                help=(
                    "Zaznaczenie pochodzi z treści zapytania. Możesz je zmienić; "
                    "API obliczy i zwróci tylko wybrane parametry."
                ),
            )
            confirmation_submit = st.form_submit_button(
                "Zatwierdź dane i parametry — oblicz prognozę",
                type="primary",
                width="stretch",
            )

        if confirmation_submit:
            confirmed_parameters = list(
                st.session_state.get("hf21_confirmation_parameters") or []
            )
            if not confirmed_parameters:
                st.error("Wybierz co najmniej jeden parametr prognozy.")
            else:
                with st.spinner("Obliczam tylko wybrane parametry prognozy…"):
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
                            "parameters": confirmed_parameters,
                            "parser_provider": (
                                pending_result.get("interpretation") or {}
                            ).get("provider"),
                            "parser_model": (
                                pending_result.get("interpretation") or {}
                            ).get("model"),
                            "parser_prompt_tokens": (
                                pending_result.get("interpretation") or {}
                            ).get("prompt_tokens"),
                            "parser_completion_tokens": (
                                pending_result.get("interpretation") or {}
                            ).get("completion_tokens"),
                            "requested_view": str(
                                pending_intent.get("requested_view") or "forecast"
                            ),
                        }
                        confirmed_result = _request_json(
                            "query",
                            method="POST",
                            payload=confirmed_payload,
                            timeout=QUERY_TIMEOUT_SECONDS,
                        )
                        # /query stores the interaction before returning.  Drop
                        # only the small FinOps cache so the next render sees it
                        # immediately instead of showing the previous 60 s view.
                        load_quality_overview.clear()
                        confirmed_result = _normalise_query_result(confirmed_result) or {}
                        returned_parameters = {
                            _ui_parameter_key(row.get("parameter"))
                            for row in confirmed_result.get("forecasts") or []
                        }
                        expected_parameters = {
                            _ui_parameter_key(value)
                            for value in confirmed_payload["parameters"]
                        }
                        missing_parameters = expected_parameters - returned_parameters
                        if missing_parameters:
                            raise RuntimeError(
                                "API nie zwróciło po zatwierdzeniu: "
                                + ", ".join(sorted(missing_parameters))
                                + ". Zwrócone parametry: "
                                + ", ".join(sorted(returned_parameters))
                            )
                        st.session_state["query_result"] = confirmed_result
                        st.session_state["selected_parameters"] = confirmed_parameters
                        selected_row = next(
                            (
                                row
                                for row in confirmed_result.get("forecasts") or []
                                if _ui_parameter_key(row.get("parameter"))
                                in expected_parameters
                            ),
                            None,
                        )
                        if selected_row:
                            st.session_state["map_parameter"] = selected_row.get("parameter")
                        if selected_row and selected_row.get("target_time"):
                            st.session_state["requested_target_time"] = selected_row["target_time"]
                        st.session_state.pop("pending_query_confirmation", None)
                        st.session_state.pop("hf21_confirmation_token", None)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nie udało się zatwierdzić danych: {exc}")

        if st.button("Odrzuć propozycje i wróć do zapytania", width="stretch"):
            st.session_state.pop("pending_query_confirmation", None)
            st.session_state.pop("query_result", None)
            st.rerun()

    query_result = _normalise_query_result(st.session_state.get("query_result"))
    if query_result:
        st.session_state["query_result"] = query_result
        accepted_location = query_result.get("location_validation") or {}
        if (
            accepted_location.get("status") == "accepted"
            and accepted_location.get("reason") == "candidate_matches_independent_reference"
        ):
            accepted_distance = accepted_location.get("distance_to_reference_km")
            accepted_reference = accepted_location.get("reference") or {}
            accepted_source = str(
                accepted_reference.get("source") or "OpenStreetMap"
            )
            st.success(
                "Lokalizacja przyjęta automatycznie: propozycja OpenAI i "
                f"{accepted_source} są zgodne"
                + (
                    f" (różnica {float(accepted_distance):.3f} km, próg 1 km)."
                    if accepted_distance is not None
                    else "."
                )
            )
    _render_forecast_cards(query_result)

    parameters = list(manifest.get("parameters") or [])
    if query_result:
        result_parameter_keys = {
            _ui_parameter_key(row.get("parameter"))
            for row in query_result.get("forecasts") or []
        }
        parameters = [
            value
            for value in parameters
            if _ui_parameter_key(value) in result_parameter_keys
        ]
    if not parameters:
        st.warning("Manifest nie zawiera powierzchni.")
        st.stop()
    default_parameter = st.session_state.get("map_parameter")
    if default_parameter not in parameters:
        default_parameter = "PM10" if "PM10" in parameters else parameters[0]

    control_cols = st.columns([1.25, 2.1, 1, 1])
    with control_cols[0]:
        parameter = st.selectbox(
            "Warstwa",
            parameters,
            index=parameters.index(default_parameter),
            format_func=lambda value: _parameter_option_label(value, manifest),
        )
    entries = _surface_entries(manifest, parameter)
    if not entries:
        st.warning(f"Brak powierzchni dla {parameter}.")
        st.stop()
    selected_parameter_quality = _manifest_parameter_quality(manifest, parameter)
    if selected_parameter_quality["experimental"]:
        st.warning(
            f"🧪 {_parameter_meta(parameter)['label']}: wynik eksperymentalny. "
            "Aktywny model nie przeszedł wszystkich miękkich progów jakości; "
            "wartość pozostaje dostępna, ale należy interpretować ją ostrożniej."
        )
    requested_target = st.session_state.get("requested_target_time")
    entry_index = 0
    if requested_target:
        requested_timestamp = pd.to_datetime(
            requested_target, utc=True, errors="coerce"
        )
        if not pd.isna(requested_timestamp):
            distances: list[tuple[float, int]] = []
            for index, entry in enumerate(entries):
                entry_timestamp = pd.to_datetime(
                    entry.get("target_time"), utc=True, errors="coerce"
                )
                if not pd.isna(entry_timestamp):
                    distances.append(
                        (
                            abs(
                                (
                                    pd.Timestamp(entry_timestamp)
                                    - pd.Timestamp(requested_timestamp)
                                ).total_seconds()
                            ),
                            index,
                        )
                    )
            if distances:
                entry_index = min(distances)[1]
    with control_cols[1]:
        selected_entry = st.selectbox(
            "Dokładny termin powierzchni",
            entries,
            index=entry_index,
            format_func=_entry_label,
        )
    with control_cols[2]:
        show_city_names = st.toggle("Nazwy miast", value=True)
    with control_cols[3]:
        show_stations = st.toggle("Stacje", value=True)
    height_scale = st.slider(
        "Skala przewyższenia terenu 3D",
        min_value=0.0,
        max_value=100.0,
        value=8.0,
        step=1.0,
        help=(
            "0 oznacza płaską mapę 2D. W trybie 3D każdy walec zaczyna się "
            "na poziomie 0, a jego wysokość wynika z wysokości terenu n.p.m. "
            "Suwak zwiększa jedynie pionowe przewyższenie."
        ),
    )
    show_confidence = st.toggle(
        "Pewność jako przezroczystość",
        value=True,
        help="Obszary słabiej pokryte stacjami są bardziej przezroczyste.",
    )
    city_label_density = DEFAULT_CITY_LABEL_DENSITY
    if show_city_names:
        city_label_density = st.select_slider(
            "Gęstość etykiet miast",
            options=list(CITY_LABEL_DENSITIES),
            value=DEFAULT_CITY_LABEL_DENSITY,
            help=(
                "Etykiety są rozmieszczane kolizyjnie. Tryb Minimalna pokazuje "
                "największe miasta, Automatyczna jest ustawieniem zalecanym, "
                "a Większa próbuje zmieścić więcej nazw bez nachodzenia."
            ),
        )

    try:
        surface = load_surface(
            parameter,
            selected_entry.get("target_time"),
            int(selected_entry.get("horizon_hours") or 0) or None,
        )
    except Exception as exc:
        st.error(f"Nie można pobrać wybranej powierzchni: {exc}")
        st.stop()

    selected_place = None
    selected_forecast = None
    if query_result:
        place = query_result.get("place") or {}
        selected_place = {
            "name": place.get("name"),
            "label": place.get("name"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
        }
        selected_forecast = next(
            (
                row
                for row in query_result.get("forecasts", [])
                if _ui_parameter_key(row.get("parameter")) == _ui_parameter_key(parameter)
                and row.get("target_time") == selected_entry.get("target_time")
            ),
            next(
                (
                    row
                    for row in query_result.get("forecasts", [])
                    if _ui_parameter_key(row.get("parameter")) == _ui_parameter_key(parameter)
                ),
                None,
            ),
        )

    if query_result:
        st.markdown(f"**Odpowiedź:** {query_result.get('summary', '')}")
        query_cost = _query_cost(query_result)
        st.caption(
            "Szczegółowe tokeny, historia i koszty całego stosu są dostępne "
            "w zakładce „Koszty i wykorzystanie”."
        )
        for warning in query_result.get("warnings") or []:
            st.warning(warning)

        with st.expander(
            "Oceń tę odpowiedź — ogólnie lub według składników", expanded=False
        ):
            feedback_key = str(query_result.get("request_id") or "current")
            score = st.slider(
                "Ocena ogólna",
                min_value=0,
                max_value=5,
                value=4,
                key=f"feedback-score-{feedback_key}",
            )
            st.caption(
                "Każdy element można ocenić niezależnie. Te oceny dotyczą "
                "gotowej odpowiedzi, a nie metryk MAE/RMSE modeli."
            )
            component_columns = st.columns(2)
            location_score = component_columns[0].slider(
                "Trafność lokalizacji", 0, 5, 4,
                key=f"feedback-location-{feedback_key}",
            )
            time_score = component_columns[1].slider(
                "Trafność daty i czasu", 0, 5, 4,
                key=f"feedback-time-{feedback_key}",
            )
            parameter_score = component_columns[0].slider(
                "Kompletność wymaganych parametrów", 0, 5, 4,
                key=f"feedback-parameters-{feedback_key}",
            )
            clarity_score = component_columns[1].slider(
                "Czytelność odpowiedzi", 0, 5, 4,
                key=f"feedback-clarity-{feedback_key}",
            )
            comment = st.text_area(
                "Komentarz (opcjonalny)",
                key=f"feedback-comment-{feedback_key}",
            )
            if st.button("Zapisz ocenę", key=f"feedback-submit-{feedback_key}"):
                try:
                    feedback_result = _request_json(
                        "feedback",
                        method="POST",
                        payload={
                            "trace_id": query_result.get("trace_id"),
                            "request_id": query_result.get("request_id"),
                            "score": float(score) / 5.0,
                            "label": f"{score}/5",
                            "comment": comment or None,
                            "question": query_result.get("question"),
                            "metadata": {
                                "dashboard_version": APP_VERSION,
                                "component_scores": {
                                    "location": float(location_score) / 5.0,
                                    "time": float(time_score) / 5.0,
                                    "parameters": float(parameter_score) / 5.0,
                                    "clarity": float(clarity_score) / 5.0,
                                },
                                "exact_time_match": (
                                    query_result.get("time_selection") or {}
                                ).get("all_selected_values_exact"),
                            },
                        },
                        timeout=30,
                    )
                    st.success(
                        "Ocena zapisana lokalnie"
                        + (
                            " i przekazana do Langfuse."
                            if (feedback_result.get("observability") or {}).get(
                                "submitted"
                            )
                            else "."
                        )
                    )
                except Exception as exc:
                    st.warning(f"Nie udało się zapisać oceny: {exc}")

    st.pydeck_chart(
        build_map(
            boundary=boundary,
            surface=surface,
            places=places,
            selected_place=selected_place,
            selected_forecast=selected_forecast,
            show_stations=show_stations,
            show_city_names=show_city_names,
            city_label_density=city_label_density,
            show_confidence=show_confidence,
            height_scale=height_scale,
        ),
        width="stretch",
        height=680,
    )

    st.caption(
        f"Warstwa: {_parameter_option_label(parameter, manifest)} · "
        f"czas bazowy {_local_time(surface.get('origin_time'))} · "
        f"czas docelowy {_local_time(surface.get('target_time'))} · "
        f"horyzont +{surface.get('horizon_hours')} h · "
        f"siatka {(surface.get('metadata') or {}).get('grid_resolution_km', '—')} km"
    )
    st.info(
        "Jak czytać mapę: kolorowe koła pokazują komórki obliczonej powierzchni "
        "interpolacyjnej (nie są stacjami). Małe znaczniki to stacje pomiarowe, "
        "a wyróżniony punkt to dokładna lokalizacja zapytania. Po włączeniu 3D "
        "koła zmieniają się w słupki, których wysokość jest tylko pomocniczą "
        "wizualizacją wartości."
    )

    if query_result:
        with st.expander("Dokładny punkt interpolacji i pochodzenie wyniku", expanded=True):
            _render_exact_point(query_result, parameter, surface)

        timeline_state_key = (
            "hf21_timeline::" + str(query_result.get("request_id") or "current")
        )
        timeline = list(
            query_result.get("timeline")
            or st.session_state.get(timeline_state_key)
            or []
        )
        place_payload = query_result.get("place") or {}
        intent_payload = query_result.get("intent") or {}
        timeline_parameters = tuple(
            value
            for value in (
                "PM10",
                "PM2.5",
                "temperature_c",
                "precipitation_probability",
                "precipitation_mm",
            )
            if value in parameters
        )
        if not timeline and place_payload.get("latitude") is not None:
            load_timeline_requested = st.button(
                "Załaduj profil godzinowy i wykresy",
                help=(
                    "To osobna, cięższa operacja. Odpowiedź dla wskazanej minuty "
                    "oraz mapa są dostępne bez pobierania całego profilu."
                ),
            )
            if load_timeline_requested:
                load_timeline.clear()
                with st.spinner(
                    "Pobieram interpolowany profil godzinowy dla wybranego miejsca…"
                ):
                    try:
                        timeline_payload = load_timeline(
                            float(place_payload["latitude"]),
                            float(place_payload["longitude"]),
                            str(intent_payload.get("target_time")),
                            timeline_parameters,
                            str(place_payload.get("name") or "wybrany punkt"),
                        )
                        timeline = list(timeline_payload.get("rows") or [])
                        st.session_state[timeline_state_key] = timeline
                        st.session_state["timeline_error"] = None
                        if timeline_payload.get("errors"):
                            st.caption(
                                "Profil załadowano częściowo: "
                                f"{len(timeline_payload['errors'])} powierzchni było niedostępnych."
                            )
                        st.caption(
                            "Profil godzinowy: "
                            f"{len(timeline)} wartości z "
                            f"{timeline_payload.get('entries_considered', 0)} pakietów; "
                            f"czas API {float(timeline_payload.get('elapsed_ms') or 0) / 1000:.1f} s."
                        )
                    except Exception as exc:
                        st.session_state["timeline_error"] = str(exc)
                        st.warning(
                            "Odpowiedź dla wskazanej godziny działa, ale nie udało się "
                            f"dobrać pełnego profilu godzinowego: {exc}"
                        )
            else:
                st.info(
                    "Profil godzinowy nie jest pobierany automatycznie. "
                    "Kliknij przycisk, jeśli chcesz zobaczyć wykresy całego dnia."
                )

        if timeline:
            place_name = str(place_payload.get("name") or "") or None
            focus_time = intent_payload.get("target_time")
            range_start, range_end = _timeline_range_control(
                timeline,
                focus_time=focus_time,
                place_name=place_name,
            )
            chart_cols = st.columns(2)
            pm_chart = _pm_timeline(
                timeline,
                place_name,
                range_start=range_start,
                range_end=range_end,
                focus_time=focus_time,
            )
            weather_chart = _weather_timeline(
                timeline,
                place_name,
                range_start=range_start,
                range_end=range_end,
                focus_time=focus_time,
            )
            if pm_chart is not None:
                chart_cols[0].plotly_chart(
                    pm_chart,
                    width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
            if weather_chart is not None:
                chart_cols[1].plotly_chart(
                    weather_chart,
                    width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
        elif not st.session_state.get("timeline_error"):
            st.info("Brak godzinowego profilu dla wybranego dnia i punktu.")

def _model_quality_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _active_model_quality(
    row: dict[str, Any], serving_manifest: dict[str, Any]
) -> dict[str, Any]:
    """Return an explicit quality decision; never expose a raw ``None`` in UI."""
    card = dict(row.get("card") or {})
    metrics = dict(card.get("metrics") or {})
    target = str(row.get("target") or card.get("target") or "")
    manifest_quality = _manifest_parameter_quality(serving_manifest, target)
    explicit_status = str(
        metrics.get("quality_status")
        or card.get("quality_status")
        or row.get("quality_status")
        or ""
    ).strip().lower()
    explicit_experimental = bool(
        metrics.get("experimental")
        or card.get("experimental")
        or row.get("experimental")
    )
    experimental = bool(
        explicit_experimental
        or explicit_status == "experimental"
        or manifest_quality.get("experimental")
    )
    reasons = list(
        metrics.get("experimental_reason")
        or card.get("experimental_reason")
        or row.get("experimental_reason")
        or manifest_quality.get("experimental_reason")
        or []
    )
    return {
        "status": "experimental" if experimental else "accepted",
        "label": "EKSPERYMENTALNY" if experimental else "ZATWIERDZONY",
        "experimental": experimental,
        "reasons": reasons,
    }


def _model_published_outputs(row: dict[str, Any]) -> list[str]:
    target = str(row.get("target") or (row.get("card") or {}).get("target") or "")
    outputs = [target] if target else []
    # Jeden model hurdle ma część regresyjną i klasyfikacyjną, dlatego dostarcza
    # dwa odrębne parametry kontraktu Serving v2.
    if target == "precipitation_mm":
        outputs.append("precipitation_probability")
    return outputs


def _candidate_frame(payload: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for run in list(payload.get("candidate_runs") or []):
        metrics = dict(run.get("metrics") or {})
        params = dict(run.get("params") or {})
        rows.append({
            "Target": run.get("target") or params.get("target"),
            "Model": run.get("provider") or params.get("provider") or "nieznany",
            "Profil": run.get("profile") or params.get("profile"),
            "Wybrany": bool(run.get("selected")),
            "Status": metrics.get("quality_status") or run.get("status"),
            "MAE": _model_quality_number(metrics.get("mae")),
            "RMSE": _model_quality_number(metrics.get("rmse")),
            "Bias": _model_quality_number(metrics.get("bias")),
            "AUC": _model_quality_number(metrics.get("roc_auc")),
            "Brier skill": _model_quality_number(
                metrics.get("brier_skill_vs_climatology")
            ),
            "Poprawa vs persistence": _model_quality_number(
                metrics.get("improvement_vs_persistence")
            ),
            "Dataset": params.get("dataset_id") or metrics.get("dataset_id"),
            "Run ID": run.get("run_id"),
            "Start": run.get("start_time"),
        })
    frame = pd.DataFrame(rows)
    if not frame.empty and "MAE" in frame:
        frame["Względny wynik"] = frame.groupby("Target")["MAE"].transform(
            lambda series: series.min() / series.replace(0, pd.NA)
        )
    return frame


def render_ml_quality_overview() -> None:
    try:
        active_payload = load_models()
        comparison = load_model_comparison()
    except Exception as exc:
        st.warning(f"Analityka modeli jest chwilowo niedostępna: {exc}")
        return
    active = list(active_payload.get("models") or [])
    candidates = _candidate_frame(comparison)
    targets = sorted({str(value) for value in candidates.get("Target", []) if value})
    # Serving v2, a nie karta modelu, jest źródłem prawdy o publicznych
    # parametrach.  Karty używają pola ``target``; poprzedni odczyt nieistniejącego
    # ``parameter`` powodował błędny licznik równy zero.
    predicted_outputs = set(_manifest_parameter_options(manifest))
    selected_count = int(candidates.get("Wybrany", pd.Series(dtype=bool)).sum())
    kpi = st.columns(4)
    kpi[0].metric("Aktywne artefakty modeli", len(active))
    kpi[1].metric("Kandydaci MLflow", len(candidates))
    kpi[2].metric("Publikowane parametry", len(predicted_outputs))
    kpi[3].metric("Zwycięzcy w historii", selected_count)

    quality_rows: list[dict[str, Any]] = []
    active_by_target = {str(row.get("target")): row for row in active}
    for parameter in _manifest_parameter_options(manifest):
        model_target = (
            "precipitation_mm"
            if parameter == "precipitation_probability"
            else parameter
        )
        model = active_by_target.get(model_target) or {}
        parameter_quality = _manifest_parameter_quality(manifest, parameter)
        status = "experimental" if parameter_quality["experimental"] else "accepted"
        quality_rows.append({
            "Cel publikacji": parameter,
            "Decyzja": "EKSPERYMENTALNY" if status == "experimental" else "ZATWIERDZONY",
            "Model źródłowy": model_target,
            "Metoda": model.get("provider") or "—",
            "Sposób": (
                "wyjście klasyfikacyjne modelu hurdle"
                if parameter == "precipitation_probability"
                else "bezpośrednie wyjście modelu"
            ),
            "Powód / uwagi": "; ".join(parameter_quality["experimental_reason"]) or "—",
        })
    approved_targets = [
        row["Cel publikacji"] for row in quality_rows if row["Decyzja"] == "ZATWIERDZONY"
    ]
    experimental_targets = [
        row["Cel publikacji"] for row in quality_rows if row["Decyzja"] == "EKSPERYMENTALNY"
    ]
    st.success("Zatwierdzone cele Serving v2: " + (", ".join(approved_targets) or "brak"))
    if experimental_targets:
        st.warning("Eksperymentalne cele Serving v2: " + ", ".join(experimental_targets))
    else:
        st.info("Eksperymentalne cele Serving v2: brak.")
    with st.expander("Decyzje publikacyjne według celu", expanded=True):
        st.dataframe(pd.DataFrame(quality_rows), hide_index=True, width="stretch")
        st.caption(
            f"{len(active)} artefakty modeli dostarczają {len(predicted_outputs)} "
            "parametrów. precipitation_probability jest osobnym wyjściem "
            "klasyfikacyjnym modelu precipitation_mm, a nie piątym artefaktem."
        )

    ranking_tab, active_tab, answer_tab, history_tab = st.tabs([
        "🏆 Ranking i zwycięzcy",
        "✅ Aktywne modele",
        "💬 Jakość odpowiedzi",
        "🧾 Historia i techniczne",
    ])
    with ranking_tab:
        if candidates.empty:
            st.info("Brak runów kandydatów w opublikowanym artefakcie MLflow.")
        else:
            chosen_target = st.selectbox(
                "Porównywany parametr", targets, key="model_ranking_target"
            )
            subset = candidates[candidates["Target"].astype(str) == chosen_target].copy()
            available_metrics = [
                metric for metric in (
                    "MAE", "RMSE", "Bias", "Poprawa vs persistence", "AUC", "Brier skill"
                ) if metric in subset and subset[metric].notna().any()
            ]
            metric = st.selectbox(
                "Metryka rankingu", available_metrics or ["MAE"],
                key="model_ranking_metric",
            )
            lower_is_better = metric in {"MAE", "RMSE"}
            chart = subset.dropna(subset=[metric]).sort_values(
                metric, ascending=lower_is_better
            )
            chart["Etykieta"] = chart["Model"].astype(str) + chart["Wybrany"].map(
                lambda selected: " ★" if selected else ""
            )
            colors = [
                "#43e0c0" if selected else "#4f7cff"
                for selected in chart["Wybrany"]
            ]
            figure = go.Figure(go.Bar(
                x=chart[metric], y=chart["Etykieta"], orientation="h",
                marker_color=colors,
                customdata=chart[["Profil", "Status", "Run ID"]].fillna("—"),
                hovertemplate=(
                    "%{y}<br>" + metric + ": %{x:.4f}<br>Profil: %{customdata[0]}"
                    "<br>Status: %{customdata[1]}<br>Run: %{customdata[2]}<extra></extra>"
                ),
            ))
            figure.update_layout(
                title=f"{chosen_target}: {metric} — {'mniej' if lower_is_better else 'więcej'} = lepiej",
                xaxis_title=metric, yaxis_title="", height=max(360, 48 * len(chart)),
                margin=dict(l=20, r=20, t=70, b=30),
            )
            st.plotly_chart(figure, width="stretch", config=PLOTLY_CHART_CONFIG)

            cross = candidates.dropna(subset=["Względny wynik"]).copy()
            if not cross.empty:
                cross_figure = go.Figure()
                for target, rows in cross.groupby("Target"):
                    cross_figure.add_box(
                        name=str(target), y=rows["Względny wynik"], boxpoints="all",
                        text=rows["Model"], jitter=0.25, pointpos=0,
                    )
                cross_figure.update_layout(
                    title="Względna jakość kandydatów w obrębie każdego parametru",
                    yaxis_title="najlepsze MAE / MAE modelu (1,0 = najlepszy)",
                    xaxis_title="Parametr",
                )
                st.plotly_chart(
                    cross_figure, width="stretch", config=PLOTLY_CHART_CONFIG
                )

            visible = [
                "Target", "Model", "Profil", "Wybrany", "Status", "MAE", "RMSE",
                "Bias", "AUC", "Brier skill", "Poprawa vs persistence", "Dataset", "Run ID"
            ]
            st.dataframe(
                subset[[column for column in visible if column in subset]],
                hide_index=True, width="stretch",
            )
            diagnostic_columns = st.columns(2)
            if subset["MAE"].notna().any() and subset["RMSE"].notna().any():
                scatter_rows = subset.dropna(subset=["MAE", "RMSE"]).copy()
                scatter_figure = go.Figure()
                for selected, rows in scatter_rows.groupby("Wybrany"):
                    scatter_figure.add_scatter(
                        x=rows["MAE"], y=rows["RMSE"], mode="markers+text",
                        name="Wybrany" if selected else "Kandydat",
                        text=rows["Model"], textposition="top center",
                        marker=dict(
                            size=16 if selected else 10,
                            color="#43e0c0" if selected else "#4f7cff",
                            line=dict(width=1, color="#ffffff"),
                        ),
                        customdata=rows[["Profil", "Bias", "Run ID"]].fillna("—"),
                        hovertemplate=(
                            "%{text}<br>MAE: %{x:.4f}<br>RMSE: %{y:.4f}"
                            "<br>Profil: %{customdata[0]}<br>Bias: %{customdata[1]}"
                            "<br>Run: %{customdata[2]}<extra></extra>"
                        ),
                    )
                scatter_figure.update_layout(
                    title="Kompromis MAE–RMSE", xaxis_title="MAE (mniej = lepiej)",
                    yaxis_title="RMSE (mniej = lepiej)", height=430,
                )
                diagnostic_columns[0].plotly_chart(
                    scatter_figure, width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
            heat_metrics = [
                name for name in ("MAE", "RMSE", "Bias", "AUC", "Brier skill")
                if name in subset and subset[name].notna().any()
            ]
            if heat_metrics:
                heat_rows = subset.copy()
                heat_rows["Etykieta"] = (
                    heat_rows["Model"].astype(str)
                    + heat_rows["Wybrany"].map(lambda value: " ★" if value else "")
                )
                heat_values = []
                for metric_name in heat_metrics:
                    series = pd.to_numeric(heat_rows[metric_name], errors="coerce")
                    minimum, maximum = series.min(), series.max()
                    if pd.isna(minimum) or pd.isna(maximum) or maximum == minimum:
                        normalised = series.map(lambda value: 1.0 if pd.notna(value) else None)
                    elif metric_name in {"MAE", "RMSE"}:
                        normalised = 1.0 - (series - minimum) / (maximum - minimum)
                    else:
                        normalised = (series - minimum) / (maximum - minimum)
                    heat_values.append(normalised.tolist())
                heat_figure = go.Figure(go.Heatmap(
                    z=heat_values,
                    x=heat_rows["Etykieta"], y=heat_metrics,
                    colorscale=[[0, "#ff6b6b"], [0.5, "#ffbf69"], [1, "#43e0c0"]],
                    zmin=0, zmax=1,
                    colorbar=dict(title="wynik względny"),
                    hovertemplate="%{y}<br>%{x}<br>Wynik względny: %{z:.2f}<extra></extra>",
                ))
                heat_figure.update_layout(
                    title="Wielowymiarowy profil kandydatów", height=430,
                    xaxis_tickangle=-25,
                )
                diagnostic_columns[1].plotly_chart(
                    heat_figure, width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )

    with active_tab:
        cards: list[dict[str, Any]] = []
        for row in active:
            card = dict(row.get("card") or {})
            metrics = dict(card.get("metrics") or {})
            quality = _active_model_quality(row, manifest)
            cards.append({
                "Parametr": row.get("target"), "Metoda": row.get("provider"),
                "Wersja": row.get("model_version"), "MAE": metrics.get("mae"),
                "RMSE": metrics.get("rmse"), "Decyzja": quality["label"],
                "Publikowane wyjścia": ", ".join(_model_published_outputs(row)),
                "Dane od": _local_time(card.get("training_data_start")),
                "Dane do": _local_time(card.get("training_data_end")),
                "Aktywowany": _local_time(row.get("activated_at")),
            })
        active_frame = pd.DataFrame(cards)
        if active_frame.empty:
            st.info("Brak aktywnych kart modeli.")
        else:
            st.dataframe(active_frame, hide_index=True, width="stretch")
            for row in active:
                quality = _active_model_quality(row, manifest)
                badge = "🧪" if quality["experimental"] else "✅"
                with st.expander(
                    f"{badge} {quality['label']} · {row.get('target')} · "
                    f"{row.get('provider')} · {row.get('model_version')}"
                ):
                    st.markdown(
                        "**Publikowane wyjścia:** "
                        + ", ".join(_model_published_outputs(row))
                    )
                    if quality["reasons"]:
                        st.warning("; ".join(quality["reasons"]))
                    st.json(row.get("card") or row, expanded=False)

    with answer_tab:
        try:
            quality = load_quality_overview()
            primary = dict(quality.get("primary") or {})
            remote = dict(quality.get("remote") or {})
            local = dict(quality.get("local") or {})
            source = primary if primary.get("status") == "ok" else local
            score = _model_quality_number(source.get("average_score"))
            positive = _model_quality_number(source.get("positive_fraction"))
            cols = st.columns(4)
            cols[0].metric(
                "Źródło",
                "nasz Object Store" if primary.get("status") == "ok" else "lokalny fallback",
            )
            cols[1].metric("Liczba ocen", int(source.get("count") or 0))
            cols[2].metric("Średnia answer_quality", "—" if score is None else f"{score:.1%}")
            cols[3].metric("Oceny pozytywne", "—" if positive is None else f"{positive:.1%}")
            interactions = dict(primary.get("interactions") or {})
            interaction_count = int(interactions.get("count") or 0)
            if interaction_count:
                st.markdown("#### Techniczna jakość gotowych odpowiedzi")
                technical_rows = []
                for label, key in (
                    ("Komplet żądanych parametrów", "complete_parameter_answers"),
                    ("Dokładny wskazany czas", "exact_time_answers"),
                    ("Rozwiązane współrzędne", "resolved_point_answers"),
                    ("Bez ostrzeżeń", "answers_without_warnings"),
                    ("Zwrócono prognozę", "answers_with_forecasts"),
                ):
                    passed = int(interactions.get(key) or 0)
                    technical_rows.append({
                        "Kontrola": label,
                        "Udział": passed / interaction_count,
                        "Poprawne": passed,
                        "Wszystkie": interaction_count,
                    })
                technical_frame = pd.DataFrame(technical_rows)
                technical_figure = go.Figure(go.Bar(
                    x=technical_frame["Udział"],
                    y=technical_frame["Kontrola"],
                    orientation="h",
                    marker_color=[
                        "#43e0c0" if value >= 0.9 else
                        "#ffbf69" if value >= 0.7 else "#ff6b6b"
                        for value in technical_frame["Udział"]
                    ],
                    text=[f"{value:.0%}" for value in technical_frame["Udział"]],
                    textposition="outside",
                    customdata=technical_frame[["Poprawne", "Wszystkie"]],
                    hovertemplate=(
                        "%{y}<br>%{x:.1%}<br>Poprawne: %{customdata[0]} / "
                        "%{customdata[1]}<extra></extra>"
                    ),
                ))
                technical_figure.update_layout(
                    xaxis=dict(title="Udział odpowiedzi", range=[0, 1.08], tickformat=".0%"),
                    yaxis_title="", height=390,
                    margin=dict(l=20, r=55, t=25, b=40),
                )
                st.plotly_chart(
                    technical_figure, width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
                details = st.columns(3)
                details[0].metric("Przeanalizowane odpowiedzi", interaction_count)
                details[1].metric(
                    "Łączne ostrzeżenia", int(interactions.get("warning_total") or 0)
                )
                details[2].metric(
                    "Zwrócone wartości prognoz",
                    int(interactions.get("forecast_value_total") or 0),
                )
                interaction_daily = dict(interactions.get("daily") or {})
                if interaction_daily:
                    technical_daily = pd.DataFrame([
                        {
                            "Data": day,
                            "Kompletność": (
                                int(row.get("complete") or 0)
                                / max(1, int(row.get("count") or 0))
                            ),
                            "Dokładny czas": (
                                int(row.get("exact_time") or 0)
                                / max(1, int(row.get("count") or 0))
                            ),
                        }
                        for day, row in sorted(interaction_daily.items())
                    ])
                    technical_daily_figure = go.Figure()
                    for metric_name, color in (
                        ("Kompletność", "#43e0c0"),
                        ("Dokładny czas", "#4f7cff"),
                    ):
                        technical_daily_figure.add_scatter(
                            x=technical_daily["Data"],
                            y=technical_daily[metric_name],
                            mode="lines+markers", name=metric_name,
                            line=dict(width=3, color=color),
                        )
                    technical_daily_figure.update_layout(
                        title="Techniczna jakość odpowiedzi w czasie",
                        yaxis=dict(range=[0, 1.05], tickformat=".0%"),
                        legend=dict(orientation="h"),
                    )
                    st.plotly_chart(
                        technical_daily_figure, width="stretch",
                        config=PLOTLY_CHART_CONFIG,
                    )

            score_buckets = dict(primary.get("score_buckets") or {})
            if score_buckets:
                st.markdown("#### Oceny użytkowników")
                score_figure = go.Figure(go.Pie(
                    labels=list(score_buckets.keys()),
                    values=list(score_buckets.values()),
                    hole=0.58,
                    marker=dict(colors=["#ff6b6b", "#ffbf69", "#4f7cff", "#43e0c0"]),
                    textinfo="label+value",
                ))
                score_figure.update_layout(
                    title="Rozkład answer_quality", height=360,
                    margin=dict(l=20, r=20, t=60, b=20),
                )
                st.plotly_chart(
                    score_figure, width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
            feedback_labels = dict(primary.get("feedback_labels") or {})
            if feedback_labels:
                st.caption(
                    "Pochodzenie ocen: "
                    + ", ".join(
                        f"{label}: {count}"
                        for label, count in sorted(feedback_labels.items())
                    )
                )
            component_scores = dict(primary.get("component_scores") or {})
            if component_scores:
                component_names = {
                    "location": "Trafność lokalizacji",
                    "time": "Trafność daty i czasu",
                    "parameters": "Kompletność parametrów",
                    "clarity": "Czytelność odpowiedzi",
                }
                component_frame = pd.DataFrame([
                    {
                        "Składnik": component_names.get(name, name),
                        "Średnia": _model_quality_number(row.get("average_score")),
                        "Liczba ocen": int(row.get("count") or 0),
                    }
                    for name, row in component_scores.items()
                    if isinstance(row, dict)
                ]).dropna(subset=["Średnia"])
                if not component_frame.empty:
                    component_figure = go.Figure(go.Bar(
                        x=component_frame["Średnia"],
                        y=component_frame["Składnik"],
                        orientation="h",
                        marker_color="#43e0c0",
                        text=[f"{value:.0%}" for value in component_frame["Średnia"]],
                        textposition="outside",
                        customdata=component_frame[["Liczba ocen"]],
                        hovertemplate=(
                            "%{y}<br>Średnia: %{x:.1%}<br>Liczba ocen: "
                            "%{customdata[0]}<extra></extra>"
                        ),
                    ))
                    component_figure.update_layout(
                        title="Ocena poszczególnych składników odpowiedzi",
                        xaxis=dict(range=[0, 1.08], tickformat=".0%"),
                        yaxis_title="", height=350,
                    )
                    st.plotly_chart(
                        component_figure, width="stretch",
                        config=PLOTLY_CHART_CONFIG,
                    )
                    radar_frame = pd.concat([
                        component_frame,
                        component_frame.iloc[[0]],
                    ], ignore_index=True)
                    radar_figure = go.Figure(go.Scatterpolar(
                        r=radar_frame["Średnia"],
                        theta=radar_frame["Składnik"],
                        fill="toself", name="Oceny użytkowników",
                        line=dict(color="#43e0c0", width=3),
                    ))
                    radar_figure.update_layout(
                        title="Profil jakości odpowiedzi",
                        polar=dict(radialaxis=dict(range=[0, 1], tickformat=".0%")),
                        showlegend=False, height=430,
                    )
                    st.plotly_chart(
                        radar_figure, width="stretch",
                        config=PLOTLY_CHART_CONFIG,
                    )
            daily = pd.DataFrame(primary.get("daily") or [])
            if not daily.empty:
                quality_figure = go.Figure()
                quality_figure.add_scatter(
                    x=daily["date"], y=daily["average_score"], mode="lines+markers",
                    name="Średnia jakość", line=dict(color="#43e0c0", width=3),
                )
                quality_figure.add_bar(
                    x=daily["date"], y=daily["count"], name="Liczba ocen",
                    yaxis="y2", opacity=0.28, marker_color="#4f7cff",
                )
                quality_figure.update_layout(
                    title="Jakość odpowiedzi w czasie",
                    yaxis=dict(title="answer_quality", range=[0, 1]),
                    yaxis2=dict(title="liczba ocen", overlaying="y", side="right"),
                    legend=dict(orientation="h"),
                )
                st.plotly_chart(
                    quality_figure, width="stretch", config=PLOTLY_CHART_CONFIG
                )
            requested_parameters = dict(interactions.get("requested_parameters") or {})
            if requested_parameters:
                parameter_frame = pd.DataFrame([
                    {"Parametr": key, "Liczba zapytań": value}
                    for key, value in requested_parameters.items()
                ]).sort_values("Liczba zapytań", ascending=False)
                parameter_figure = go.Figure(go.Bar(
                    x=parameter_frame["Parametr"],
                    y=parameter_frame["Liczba zapytań"],
                    marker_color="#4f7cff",
                ))
                parameter_figure.update_layout(
                    title="Parametry wymagane przez użytkowników",
                    xaxis_title="Parametr", yaxis_title="Liczba zapytań",
                )
                st.plotly_chart(
                    parameter_figure, width="stretch",
                    config=PLOTLY_CHART_CONFIG,
                )
            if remote.get("status") == "ok":
                st.caption(
                    "Langfuse działa wyłącznie jako dodatkowa diagnostyka; "
                    "źródłem danych panelu jest nasz Object Store."
                )
        except Exception as exc:
            st.warning(f"Nie udało się odczytać analityki jakości odpowiedzi: {exc}")

    with history_tab:
        history = candidates.copy()
        if not history.empty:
            history["Start"] = pd.to_datetime(history["Start"], unit="ms", errors="coerce")
            history = history.sort_values("Start", ascending=False)
            st.dataframe(
                history[[column for column in (
                    "Start", "Target", "Model", "Profil", "Wybrany", "Status", "Dataset", "Run ID"
                ) if column in history]],
                hide_index=True, width="stretch",
            )
        tracking_uri = comparison.get("tracking_uri")
        st.caption(
            "Na DigitalOcean używany jest wersjonowany artefakt porównania; "
            "lokalny serwer MLflow nie jest wymagany do działania aplikacji."
        )
        if tracking_uri:
            st.code(str(tracking_uri))


with model_tab:
    st.subheader("Centrum modeli, MLOps i niezależnych ocen odpowiedzi")
    st.caption(
        "Ranking, Aktywne modele i Historia dotyczą wyłącznie modeli ML. "
        "Jakość odpowiedzi dotyczy wyłącznie gotowych odpowiedzi ocenianych "
        "pod mapą — metryki MAE/RMSE nie są oceną tekstu."
    )
    render_ml_quality_overview()


with cost_tab:
    refresh_costs = st.button(
        "Odśwież koszty i historię",
        help="Czyści tylko 60-sekundowy cache agregatu FinOps.",
        key="refresh-finops-overview",
    )
    if refresh_costs:
        load_quality_overview.clear()
    try:
        cost_quality = load_quality_overview()
    except Exception as exc:
        st.warning(f"Nie można pobrać agregatu kosztów i wykorzystania: {exc}")
        cost_quality = {}
    _render_cost_center(
        cost_quality,
        _normalise_query_result(st.session_state.get("query_result")) or {},
    )


with docs_tab:
    st.subheader("Dokumentacja techniczna i matematyczna")
    (
        processing_tab,
        mathematics_tab,
        plugin_tab,
        hf20_tab,
        technical_latex_tab,
        math_latex_tab,
    ) = st.tabs(
        [
            "Przetwarzanie",
            "Model matematyczny",
            "Własne metody",
            "HF20: czas i MLOps",
            "LaTeX techniczny",
            "LaTeX matematyczny",
        ]
    )
    with processing_tab:
        try:
            st.markdown(load_document("docs/processing"))
        except Exception as exc:
            st.error(f"Dokumentacja techniczna jest niedostępna: {exc}")
    with mathematics_tab:
        try:
            st.markdown(load_document("docs/mathematics"))
        except Exception as exc:
            st.error(f"Dokumentacja matematyczna jest niedostępna: {exc}")
    with plugin_tab:
        try:
            st.markdown(load_document("docs/model-plugins"))
        except Exception as exc:
            st.error(f"Dokumentacja rozszerzeń modeli jest niedostępna: {exc}")
    with hf20_tab:
        try:
            st.markdown(load_document("docs/hf20"))
            hf20_latex = load_document("docs/hf20/source")
            st.download_button(
                "Pobierz dokumentację HF20 w LaTeX",
                data=hf20_latex.encode("utf-8"),
                file_name="DODATEK_TECHNICZNY_HF20_TIME_CONTRACT_MLOPS_PL.tex",
                mime="application/x-tex",
            )
        except Exception as exc:
            st.error(f"Dokumentacja HF20 jest niedostępna: {exc}")
    with technical_latex_tab:
        try:
            technical_latex = load_document("docs/processing/source")
            st.download_button(
                "Pobierz techniczną dokumentację LaTeX",
                data=technical_latex.encode("utf-8"),
                file_name="DOKUMENTACJA_TECHNICZNA_PRZETWARZANIA_PL.tex",
                mime="application/x-tex",
            )
            st.code(technical_latex[:12000], language="latex")
            if len(technical_latex) > 12000:
                st.caption("Podgląd został skrócony; pobierany plik zawiera pełne źródło.")
        except Exception as exc:
            st.error(f"Techniczne źródło LaTeX jest niedostępne: {exc}")
    with math_latex_tab:
        try:
            latex_source = load_document("docs/mathematics/source")
            st.download_button(
                "Pobierz matematyczną dokumentację LaTeX",
                data=latex_source.encode("utf-8"),
                file_name="DOKUMENTACJA_MODELU_GODZINOWEGO_PL.tex",
                mime="application/x-tex",
            )
            st.code(latex_source[:12000], language="latex")
            if len(latex_source) > 12000:
                st.caption("Podgląd został skrócony; pobierany plik zawiera pełne źródło.")
        except Exception as exc:
            st.error(f"Matematyczne źródło LaTeX jest niedostępne: {exc}")

st.caption(
    f"{CUSTOMER_NAME} · wersja {APP_VERSION} · API {API_URL} · "
    "predykcja i interpolacja wykonywane lokalnie"
)
