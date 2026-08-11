# HF21 Dashboard UX v2

Poprawka zachowuje istniejące dane, snapshoty, modele i kopie zapasowe. Nie
przesyła danych treningowych ani operacyjnych do DigitalOcean Spaces.

## Zmiany

- jawne porównanie propozycji OpenAI z niezależnym resolverem
  OpenStreetMap/Nominatim;
- odległość między punktami i bezpieczna rekomendacja;
- osobne porównanie czasu OpenAI z parserem deterministycznym;
- możliwość wyboru jednego ze źródeł albo ręcznej korekty;
- mapa OpenStreetMap do wskazania dowolnego dokładnego punktu;
- kontrastowe etykiety miejscowości;
- wykresy MAE/RMSE/Bias także dla opublikowanych aktywnych i historycznych
  modeli, nie tylko dla bieżących kandydatów MLflow;
- domyślny parser OpenAI: `gpt-5.4-mini`;
- lokalny cache geokodera; żadne snapshoty ani powierzchnie nie trafiają do
  OpenStreetMap.

## Instalacja w katalogu projektu

Najpierw rozpakuj paczkę do katalogu projektu, zachowując katalog `scripts`.
Następnie w PowerShellu uruchom:

```powershell
$ProjectRoot = 'C:\..Work\..GotoIT\Works\..Projects\Weather\..Work\GIOS_IMGW_Forecast_Suite_1.7.0_HF21_ExactPoint_StorageBridge_PCHIP'
Set-Location -LiteralPath $ProjectRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $PythonExe .\scripts\apply_hf21_dashboard_ux_patch.py
& $PythonExe -m pip install "folium>=0.19,<1" "streamlit-folium>=0.24,<1"
```

Instalator tworzy kopie `.bak` przed zmianą plików.

## `.env`

Ustaw lub zmień poniższe wartości:

```dotenv
SMOG_AI_LLM_MODEL=gpt-5.4-mini
SMOG_AI_GEOCODER_PROVIDER=nominatim
SMOG_AI_GEOCODER_ENDPOINT=https://nominatim.openstreetmap.org
SMOG_AI_GEOCODER_USER_AGENT=SmogAI-HF21/1.7
SMOG_AI_GEOCODER_CACHE_PATH=C:\ProgramData\SmogAI\cache\geocoder-cache.json
```

Pozostaw dotychczasowy `LLM_API_KEY`. Po zmianach uruchom ponownie zarówno API,
jak i dashboard. API musi być uruchomione z tego samego pliku `.env`.

## Kontrola

```powershell
& $PythonExe -m py_compile .\server\dashboard\app.py
& $PythonExe -m pytest .\tests\test_openai_structured_intent.py -q
```

Po uruchomieniu `/api/v1/health` powinno zawierać:

```text
nlp_provider = openai_compatible
nlp_model    = gpt-5.4-mini
```

W zapytaniu o Mieroszów ekran kontroli ma pokazać osobno punkt OpenAI i punkt
OpenStreetMap/Nominatim. Jeżeli różnią się o ponad 3 km, domyślnie wybierany jest
punkt niezależnego resolvera, ale prognoza nie jest liczona przed zatwierdzeniem.
