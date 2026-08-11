# HF20.3 — semantyka wyniku audytu

## Awarie techniczne

Do `hard_failures` należą wyłącznie problemy takie jak:

- brak prognoz,
- niepełne serving leads,
- horyzont modelu poza kontraktem,
- czas docelowy w przeszłości,
- NaN/Inf,
- wartość poza zakresem,
- płaskie krzywe podstawowych parametrów,
- różna siatka czasów pomiędzy parametrami.

## Awarie jakościowe

Do `quality_failures` należą modele, które technicznie zwracają pełne
prognozy, lecz nie spełniają progu jakości. W HF20.3 dotyczy to w
szczególności opadu.

## Kody

- `0`: wszystkie skonfigurowane cele są gotowe;
- `4`: wynik częściowy albo problem techniczny; raport rozstrzyga rodzaj;
- `1`: błąd wykonania.

Do automatyzacji służą pola `decision`, `approved_targets` i
`experimental_targets`.
