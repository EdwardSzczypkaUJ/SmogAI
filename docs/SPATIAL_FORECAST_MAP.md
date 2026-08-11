# Przestrzenna mapa prognoz Polski 1.7.0

## Cel

Dla każdej pełnej godziny `h=1..48` lokalny pipeline tworzy powierzchnie:

```text
PM10
PM2.5
temperature_c
precipitation_probability
precipitation_mm
```

App Platform odczytuje opublikowane prognozy stacyjne i gotowe siatki przez
`ObjectStore` Bridge. Dla zapytania użytkownika liczy lekkie IDW dokładnie we
współrzędnych punktu; nie uruchamia modelu ML.

## Kolejność

```text
godzinowe prognozy dla stacji
→ kontrola jakości
→ WGS84 → EPSG:2180
→ regularna siatka i maska Polski
→ IDW albo RBF
→ confidence + leave-one-station-out
→ Pandera
→ gzip JSON + immutable manifest
→ atomowy maps/latest.json
```

## Dlaczego EPSG:2180

Odległości są liczone w metrach w układzie właściwym dla Polski, nie w stopniach
geograficznych. Wynik jest ponownie zapisany w WGS84 dla mapy.

## Bridge interpolatora

```python
class SpatialInterpolator(Protocol):
    def interpolate(self, *, grid, stations, parameter, horizon_hours,
                    origin_time, target_time): ...
```

Wbudowane implementacje:

- `IDWInterpolator` — domyślny i deterministyczny;
- `RBFSpatialInterpolator` — alternatywa SciPy.

Nowy interpolator można dodać bez zmian w FastAPI i Streamlit.

## Wartość i pewność

IDW:

```text
w_i = q_i / (d_i^p + d_0^p)
z(x) = Σ(w_i z_i) / Σ(w_i)
```

Domyślnie `p=2`. `q_i` składa się z dostępnych wag jakości, aktualności,
modelu i kompletności. Punkt leżący w małym progu od stacji zwraca jej wartość
bez uśredniania. Parametry `p`, liczba sąsiadów i promień mają być później
dobierane osobno dla parametrów przez leave-one-station-out.

`confidence ∈ [0,1]` łączy odległość, liczbę stacji, lokalny rozrzut i możliwość
wyznaczenia wartości w promieniu. Niska pewność zmniejsza alpha.

## Opad

Dla opadu interpolowane są osobno prawdopodobieństwo i oczekiwana suma.
`precipitation_mm` zachowuje okres akumulacji, domyślnie 6 h kończących się w
`target_time`; nie jest sztucznie dzielone na `mm/h`.

## Dokładny punkt

API i UI pokazują:

- dokładne współrzędne zapytania i precyzję ich rozwiązania;
- odległość do najbliższej stacji;
- liczbę stacji użytych przez interpolator;
- algorytm, `p`, EPSG:2180 i confidence;
- listę stacji wraz z odległościami, jakością i znormalizowanymi udziałami.

Dla zapytania minutowego kolejność jest niezmienna: IDW w tym samym punkcie
dla godzin źródłowych, następnie PCHIP po uzyskanych wartościach. Nie łączymy
czasowo wartości odnoszących się do różnych lokalizacji.

## Wizualizacja PyDeck

- `GeoJsonLayer` — granica Polski;
- `ColumnLayer` — powierzchnia 2D lub ograniczone 3D;
- `ScatterplotLayer` — stacje, punkt miasta i środek komórki;
- `TextLayer` — nazwy miast nad powierzchnią;
- tooltip — wartość, jednostka, czas, confidence i geometria.

Domyślnie 3D jest wyłączone. Wysokość jest skalowana logarytmicznie i limitowana,
a etykiety są rysowane po powierzchni, dzięki czemu nie znikają za słupkami.

## Walidacja

Leave-one-station-out usuwa każdą stację, interpoluje w jej położeniu i liczy
MAE/RMSE. Walidacja dotyczy osobno każdego parametru i czasu docelowego.

## Polecenia

```powershell
& $Python -m smog_ai build-spatial-surfaces --config $Config --env-file $EnvFile
& $Python -m smog_ai validate-spatial-surfaces --config $Config --env-file $EnvFile
```
