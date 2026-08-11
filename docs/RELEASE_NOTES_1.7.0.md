# Release notes 1.7.0 — Hourly Multi-Target & Pluggable Models

## Najważniejsze zmiany

- prognozy warunkowane dokładnym horyzontem `h=1..48`;
- brak cichego wyboru najbliższej powierzchni 6/12/24 h;
- temperatura i opad jako pełnoprawne cele;
- dwuetapowy hurdle model opadu;
- prognozowana pogoda jako cecha modeli PM10/PM2.5;
- chronologiczny cross-fitting ograniczający wyciek przyszłości;
- rejestr `ModelProvider` z pluginami, entry points i import strings;
- wbudowane ridge, polynomial ridge, gradient boosting, quantile, MLP i baseline;
- archiwalny importer miesięcznych danych terminowych/SYNOP IMGW;
- dokładne łączenie celu po `target_time`, a nie przesunięciu pozycyjnym;
- powierzchnie PM10, PM2.5, temperatury i opadu dla każdej godziny;
- dashboard z nazwami miast, poprawionym 3D, pogodą i punktem interpolacji;
- zakładka „Model i jakość” oraz dokumentacja dostępna na platformie;
- dokumentacja matematyczna i techniczna w LaTeX;
- zachowana pełna kompatybilność Bridge dla storage i frontendów.

## Zgodność

Migracja `0002_weather_precipitation_period.py` dodaje jawny okres akumulacji
opadu. Stare rekordy mogą być odczytywane zgodnie z skonfigurowanym okresem
legacy. Model 1.5.x pozostaje dostępny jako fallback po wyłączeniu
`hourly_forecasting.enabled`.

## Istotna semantyka opadu

`precipitation_mm` oznacza sumę opadu w jawnym okresie akumulacji kończącym
się w `target_time`. Domyślnie jest to 6 h. System nie tworzy sztucznego
rozkładu godzinowego przez dzielenie wartości przez sześć.
