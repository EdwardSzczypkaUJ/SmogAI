# Zunifikowany katalog parametrów i zgodność UTF-8

## Powód zmiany

`AirParameterRegistry` opisuje zanieczyszczenia GIOŚ. Temperatura, opad,
wilgotność, ciśnienie oraz wiatr pochodzą z IMGW i nie są parametrami
zanieczyszczeń. Poprzedni skrypt pokazywał wyłącznie część GIOŚ, dlatego
`temperature_c` nie była widoczna mimo obecności w `hourly_forecasting.targets`.

Windows PowerShell 5.1 może uruchamiać proces Pythona z kodowaniem `cp1250`.
Jednostki `mg/m³`, `µg/m³`, `°C` i `°` nie zawsze są reprezentowalne w tej
stronie kodowej. CLI zachowuje teraz pełne dane przez awaryjne escapowanie JSON
(`ensure_ascii=True`), a skrypt katalogu przełącza konsolę i Pythona na UTF-8.

## Sekcje katalogu

- parametry powietrza GIOŚ: rejestr generyczny i role pobierania/modelowania;
- parametry pogodowe IMGW: temperatura, wilgotność, ciśnienie, opad, wiatr;
- cele godzinowe i warstwy przestrzenne;
- pochodne, np. prawdopodobieństwo opadu.

## Polecenia

```powershell
.\scripts\Show-ParameterCatalog.ps1 `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot
```

Stare polecenie `Show-AirParameterCatalog.ps1` pozostaje zgodnym aliasem i
również wyświetla obie sekcje.
