# HF21 — monitor czasu, ETA, raporty historii i czyszczenie

Hotfix dodaje:

- czas całego przebiegu aktualizowany podczas działania;
- czas bieżącego etapu i trenowanego modelu;
- ETA całego przebiegu i etapu z mediany porównywalnych przebiegów;
- zakres ETA, liczbę próbek oraz poziom wiarygodności;
- kolorowe statusy kandydatów, modeli i decyzji publikacyjnych;
- poprawne zakończenie statusu kandydatów po zamknięciu przebiegu;
- przyciski otwarcia i pobrania raportu w wierszu historii;
- użycie `elapsed_seconds` w historii zakończonych przebiegów;
- usuwanie atrybutu tylko do odczytu przed retencyjnym usunięciem `smog.db`;
- trzy krótkie próby usunięcia w przypadku chwilowej blokady Windows.

ETA nie jest prezentowane jako dokładna obietnica. Monitor pokazuje medianę,
przedział oraz wiarygodność zależną od liczby podobnych przebiegów tego samego
profilu. Przy braku historii wyświetlany jest komunikat `brak historii`.

Hotfix nie włącza harmonogramu, nie uruchamia treningu i nie usuwa ręcznie
żadnego aktualnego snapshotu.
