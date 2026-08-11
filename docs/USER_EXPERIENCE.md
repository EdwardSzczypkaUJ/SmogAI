# Doświadczenie użytkownika publicznej aplikacji 1.7.0

## Zapytanie z dokładną godziną

```text
Jutro o 17:00 jadę do Katowic. Jakie będą PM10, PM2.5, temperatura i opad?
```

Aplikacja:

1. wyodrębnia lokalizację i dokładny `target_time`;
2. rozwiązuje współrzędne miasta offline;
3. odczytuje opublikowane prognozy stacyjne przez Bridge;
4. nie wybiera najbliższego horyzontu 6/12/24;
5. liczy quality-weighted IDW dokładnie dla współrzędnych punktu;
6. pokazuje PM10, PM2.5, temperaturę, prawdopodobieństwo opadu i sumę opadu;
7. pokazuje `origin_time`, `target_time`, horyzont, model i pewność;
8. pokazuje punkt, najbliższą stację oraz udziały wszystkich użytych stacji.

Dla godziny z minutami aplikacja najpierw liczy IDW w tym samym punkcie dla
godzin źródłowych, a następnie PCHIP. Współrzędne wpisane przez użytkownika lub
wskazane na mapie mają pierwszeństwo przed centrum miejscowości.

## Zapytanie bez godziny

```text
Jutro jadę do Krakowa.
```

System nie udaje jednej arbitralnej wartości. Zwraca profil godzinowy dnia i
wyświetla na mapie godzinę startową z jawnym założeniem.

## Mapa

Domyślny jest czytelny widok 2D. Nazwy najważniejszych miast są osobną warstwą
nad powierzchnią. Tryb 3D jest opcjonalny; wysokość jest ograniczona i
logarytmicznie skalowana, aby nie zasłaniać etykiet i stacji.

Warstwy:

```text
PM10
PM2.5
temperatura
prawdopodobieństwo opadu
oczekiwana suma opadu
```

## Uczciwość prezentacji

UI wyraźnie rozróżnia:

```text
punkt prognozy i precyzję jego rozwiązania
najbliższą stację
stacje użyte do interpolacji wraz z udziałami
```

Wartość dla miasta jest prognozą przestrzennie interpolowaną, a nie pomiarem w
tym punkcie. LLM interpretuje tekst, ale nie generuje wartości liczbowych.

## Zakładki

- **Mapa i prognoza** — textbox, mapa, profile godzinowe i szczegóły punktu;
- **Model i jakość** — provider, wersja, zakres treningu, metryki i model card;
- **Jak to działa** — dokumentacja techniczna, matematyczna, pluginy i źródła LaTeX.

## Tryb awaryjny

Brak dokładnego pakietu godzinowego daje jawny komunikat o niedostępności. W
trybie legacy można włączyć fallback 6/12/24, lecz jest on oznaczony jako
`nearest_legacy_*` i nie jest domyślną logiką 1.7.0.
