# HF21 — dokładny punkt i dwukierunkowy Storage Bridge

## Zakres

Ta zmiana nie pobiera ponownie danych GIOŚ/IMGW, nie uruchamia pełnego treningu
i nie tworzy kolejnych kopii bazy. Korzysta z istniejącego projektu, danych oraz
backupów i rozszerza warstwę serwującą.

## Storage Bridge

`ArtifactRepository` zależy wyłącznie od protokołu `ObjectStore`. Ten sam
kontrakt obsługuje zapis i odczyt:

- lokalny katalog (`backend: local`);
- pamięć testową (`memory`);
- S3, MinIO lub DigitalOcean Spaces (`s3`/`spaces`).

Pipeline, API i dashboard nie zawierają osobnej logiki biznesowej dla Spaces.
Test `test_storage_bridge_backend_parity.py` wykonuje identyczny round-trip JSON,
gzip JSON, listowanie, konflikt immutable i usunięcie dla local oraz adaptera S3.

## Dokładny punkt

- jawne współrzędne użytkownika mają pierwszeństwo przed geokoderem;
- LLM nie wymyśla współrzędnych;
- wartości są liczone przez quality-weighted IDW, domyślnie `p=2`;
- odległości są metryczne w EPSG:2180;
- mały próg przy stacji zwraca dokładną wartość tej stacji;
- odpowiedź API zawiera wkłady stacji, odległości i znormalizowane wagi;
- dla minut kolejność to przestrzeń, a następnie czas: IDW → PCHIP.

App Platform nadal nie ładuje modelu ML i nie wykonuje `model.predict`.
Odczytuje opublikowane prognozy stacyjne przez Bridge i wykonuje jedynie lekkie,
deterministyczne obliczenie punktu.

## Weryfikacja

- pełny zestaw testów jednostkowych i integracyjnych offline;
- osobne testy dokładnego punktu i czterech godzin źródłowych PCHIP;
- walidacja specyfikacji DigitalOcean;
- seeded FastAPI smoke test;
- kompilacja pakietu.

## Następny etap

Bez ponownego pobierania danych:

1. podłączyć istniejącą bazę/runtime i wykonać audyt zakresów;
2. uruchomić kontrolowany profil `quick` na istniejącym snapshocie;
3. dodać wybór punktu kliknięciem mapy i trwałe własne miejsca;
4. zwalidować `p`, liczbę sąsiadów i promień osobno dla parametrów;
5. dodać korektę wysokości temperatury i dopiero potem eksperymenty anizotropowe.
