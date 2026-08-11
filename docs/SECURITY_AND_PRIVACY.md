# Bezpieczeństwo i prywatność

## Model dostępu

```text
publiczna przeglądarka → Streamlit → prywatny URL FastAPI → prywatny Space
```

Space nie musi być publiczny. CDN, publiczny listing i CORS są wyłączone.

## Sekrety

Sekrety nie trafiają do repozytorium:

- lokalnie: `%ProgramData%\SmogAI\smog-ai.env` i `server-local.env`;
- GitHub: Actions Secrets;
- App Platform: env `type: SECRET`.

Nie przekazuj tokenów jako argumentów wiersza poleceń.

## Rozdzielenie kluczy

Zalecane:

```text
lokalny pipeline: Limited access, Read/Write/Delete
FastAPI:          Limited access, Read
Streamlit:        brak klucza
```

W demo dopuszczalne jest współdzielenie klucza, ale rotacja wtedy wpływa na oba komponenty.

## API

- upload HTTP jest wyłączony w produkcyjnym App Platform;
- endpointy odczytowe mają rate limiting;
- limity rozmiaru dotyczą opcjonalnego uploadu;
- nagłówki `nosniff`, `no-referrer`, `DENY` są ustawiane przez middleware;
- Swagger produkcyjny jest wyłączony;
- publiczne API nie udostępnia SQLite.

## LLM i Langfuse

Do LLM trafia pytanie użytkownika i lista kandydatów miejsc, nie pełna baza pomiarów. Langfuse jest opcjonalny. Nie umieszczaj w textboxie danych osobowych; przy produkcji z użytkownikami należy dodać politykę retencji i pseudonimizację `session_id`.

## Rotacja

1. utwórz nowy klucz;
2. zaktualizuj lokalny env lub GitHub Secret;
3. przetestuj `storage-health`/deploy;
4. dopiero potem usuń stary klucz.
