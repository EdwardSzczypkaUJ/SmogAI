# SmogAI — FinOps i porównanie modeli przed demonstracją

Stan implementacji: przygotowane na odizolowanej kopii źródeł. Przeniesienie do
repozytorium roboczego jest dozwolone dopiero po zakończeniu aktywnego Serving.
Publikacja, push i deployment wymagają osobnych zgód operatora.

## Zakodowane

- publiczny kontrakt porównania modeli `1.1-public` bez ścieżek, identyfikatorów
  MLflow, danych treningowych i plików modeli;
- metryki per parametr, provider i horyzont 1–48 h oraz zwycięzca każdego
  parametru/horyzontu;
- ranking, heatmapa parametr × horyzont, donut zwycięzców i radar modeli;
- poprawne przypisanie metryk do rozwijanych kart aktywnych modeli;
- rozpoznawanie providerów `openai` i `openai_compatible`;
- cennik `gpt-5.4-mini` ze źródłem i datą, bez wyceny inną stawką;
- historia tokenów, providerów, modeli, struktura kosztów i scenariusze wolumenu;
- odczyt starszych raportów publikacji zapisanych jako UTF-16;
- transfer Spaces dzienny i miesięczny;
- dla nowych publikacji: kategorie `surfaces`, `stats`, `static`, `manifest`,
  `pointer`, czas, przepustowość, requesty i reuse/cache ratio;
- polityka fresh do 14 h, warning powyżej 14 do 22 h, stale powyżej 22 h;
- niezależny wiek pomiaru i ostatniego pobrania na wykresie i w statusie;
- warning nie blokuje pointera; stale i missing blokują, zachowując poprzedni
  publiczny release;
- stabilny klucz widgetu pytania Streamlit, aby poprzedni tekst nie wracał;
- domyślnie wyłączony cichy fallback OpenAI do parsera regułowego.

## Brama po zakończeniu Serving

1. Sprawdzić, że proces Serving nie działa, a `run.json` ma stan końcowy.
2. Porównać SHA-256 plików źródłowych z kopią diagnostyczną.
3. Przenieść wyłącznie przygotowane zmiany, bez nadpisania innych zmian operatora.
4. Uruchomić testy celowane porównania modeli, świeżości, publikacji i dashboardu.
5. Uruchomić pełny `pytest`, `ruff check` oraz `git diff --check`.
6. Zbudować lokalny publiczny artefakt porównania i sprawdzić sanitizację.
7. Uruchomić lokalny preflight bez zapisów zewnętrznych.
8. Nie commitować, nie pushować, nie publikować i nie wdrażać bez osobnej zgody.

## Po demonstracji

- trwały outbox i retry po braku sieci/restarcie;
- deduplikacja żądań publikacji i pełna odporność na duplikaty;
- sterowanie ponownym pobieraniem GIOŚ/IMGW na podstawie osobnego wieku pobrania;
- pełny licznik cache input OpenAI, jeśli API zwróci tę kategorię usage;
- uzgodnienie transferu i faktur z API DigitalOcean;
- monitoring kolejki, błędów i ETA;
- wspólny system progress dla `snapshot-train-hourly`.
