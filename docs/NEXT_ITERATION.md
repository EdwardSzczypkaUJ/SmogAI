# Kolejna iteracja po 1.7.0

Priorytety po pilocie godzinowego modelu wielozadaniowego:

1. walidacja naukowa na pełnych 2–3 latach oraz sezonach;
2. porównanie bezpośredniego modelu horyzontowego z multi-output i seq2seq;
3. ensemble providerów oraz kalibracja kwantyli conformal prediction;
4. GAM/splajny jako interpretowalna alternatywa dla gradient boostingu;
5. XGBoost/LightGBM/CatBoost jako zewnętrzne pluginy;
6. modele regionalne i hierarchiczne z efektem stacji;
7. porównanie IDW, RBF i krigingu w walidacji leave-one-station-out;
8. korekta temperatury o wysokość i modelowanie terenu;
9. lepszy model zerowo-inflacyjnego opadu i kalibracja prawdopodobieństwa;
10. kafle binarne/Parquet dla gęstszej siatki i animacji 48 godzin;
11. monitoring driftu danych, cech i jakości per horyzont;
12. testy wizualne, dostępność WCAG i optymalizacja mobilna;
13. rozszerzony gazetteer TERYT oraz lokalizacje spoza centrów miast;
14. OpenTelemetry obok Langfuse;
15. procedura blue/green dla aktywnych model cards i rollbacku artefaktów.
