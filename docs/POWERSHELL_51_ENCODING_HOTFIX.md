# Historia poprawki kodowania PowerShell

Wydanie 1.4.0 miało skrypty UTF-8 bez BOM, co w Windows PowerShell 5.1 mogło uszkadzać polskie znaki i powodować pozorne błędy klamer. Od 1.4.1, również w 1.7.0, wszystkie `.ps1` są wydawane jako UTF-8 BOM + CRLF. Test regresyjny i release gate sprawdzają każdy plik.

Nie konwertuj skryptów na ANSI. `.editorconfig` i `.gitattributes` utrzymują kontrakt.
