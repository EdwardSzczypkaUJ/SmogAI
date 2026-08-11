# Analiza dostarczonego `data-ranges.zip`

Dostarczony pakiet zawiera dwa audyty. Najnowszy obejmuje przedział od
1 stycznia 2025 do 9 sierpnia 2026 (czas lokalny) i używa progu co najmniej
pięciu stacji na godzinę.

Najważniejsze wnioski projektowe:

- PM10 i PM2.5 mają niemal ciągłą oś czasu; plan nie powinien ponownie pobierać
  całych lat dla pojedynczych luk jednogodzinnych.
- PM2.5 ma nieaktualny ogon od 6 sierpnia 2026 i powinno najpierw użyć kolektora
  bieżącego GIOŚ.
- dane pogodowe nie obejmują całego 2025 roku;
- okres lipiec/sierpień 2026 wymaga ponownej kontroli archiwów i danych bieżących;
- stary audyt opadu traktował `WO6G` jako wartość godzinną, przez co raportował
  regularne pięciogodzinne „luki”. Nowy audyt oczekuje slotów co sześć godzin i
  nie generuje fikcyjnego planu pobierania.

Program nie wykonuje bezwarunkowo planu z ZIP-a. Używa go do odtworzenia
żądanego zakresu i parametrów, po czym ponownie oblicza aktualne braki w SQLite.


## Zachowanie progu dostarczonego audytu

Najnowszy audyt używa progu pięciu stacji dla jakości powietrza i pogody.
Range-aware backfill odtwarza ten próg z ZIP-a, ale nie ufa zapisanym listom
luk: przed wykonaniem planu ponownie oblicza je w aktualnej SQLite.
