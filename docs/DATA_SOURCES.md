# Źródła danych używane przez wydanie 1.7.0

## GIOŚ — bieżąca jakość powietrza

Konfiguracja używa API v1 JSON-LD:

```text
https://api.gios.gov.pl/pjp-api/v1/rest/station/findAll
https://api.gios.gov.pl/pjp-api/v1/rest/station/sensors/{stationId}
https://api.gios.gov.pl/pjp-api/v1/rest/data/getData/{sensorId}
```

Klient wysyła `Accept: application/ld+json, application/json;q=0.9, */*;q=0.1`,
obsługuje stronicowanie, PM10/PM2.5 oraz błędy pojedynczych sensorów. Odpowiedzi
400/404 dla stanowisk bez bieżącej serii są klasyfikowane jako niedostępne, nie
jako awaria całej kolekcji.

Oficjalna strona informacyjna:

```text
https://powietrze.gios.gov.pl/pjp/content/api
```

## IMGW — bieżący SYNOP

```text
https://danepubliczne.imgw.pl/api/data/synop
```

Adapter pobiera temperaturę, wilgotność, ciśnienie, prędkość i kierunek wiatru
oraz sumę opadu. Pole opadu ma jawny okres akumulacji. Domyślna konfiguracja
traktuje je jako sumę z 6 godzin kończącą się w czasie pomiaru, nie jako `mm/h`.

## IMGW — archiwalne dane terminowe/SYNOP

```text
https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/
  dane_meteorologiczne/terminowe/synop/
```

`smog_ai.collectors.imgw_archive` pobiera miesięczne ZIP-y, kontroluje SHA-256,
parsuje CSV według dołączonego nagłówka, zapisuje kody jakości i działa
idempotentnie. Domyślny lookback to 24 miesiące i można go zmienić w
`config.yaml`.

## Czas i pochodzenie

Wszystkie daty są zapisywane w UTC. Źródłowa strefa czasu, endpoint, checksum,
plik miesięczny, kod jakości i surowe pola diagnostyczne są zachowywane w
metadanych. Interfejs prezentuje `Europe/Warsaw`.

## Ważne ograniczenie naukowe

Aktualna obserwacja IMGW nie jest prognozą meteorologiczną. Wydanie 1.7.0
prognozuje temperaturę i opad własnymi modelami godzinowymi na podstawie
zgromadzonej historii. Wiarygodność tych modeli zależy od długości i jakości
historii oraz musi być potwierdzona chronologicznym backtestem.
