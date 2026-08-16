# Synchronizacja konfiguracji lokalnej SmogAI HF21

Skrypt `scripts\Sync-SmogAI-Environment.ps1` traktuje projektowy `.env` jako
źródło ustawień parsera OpenAI, geokodera Nominatim i Langfuse. Aktualizuje
`C:\ProgramData\SmogAI\smog-ai.env` oraz `server-local.env`, ale nie zmienia
ustawień Spaces, bazy danych, tokenu publikacji ani lokalnego storage.

Walidacja bez zapisu:

```powershell
.\scripts\Sync-SmogAI-Environment.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot 'C:\ProgramData\SmogAI' `
  -ValidateOnly
```

Synchronizacja:

```powershell
.\scripts\Sync-SmogAI-Environment.ps1 `
  -ProjectRoot (Get-Location).Path `
  -RuntimeRoot 'C:\ProgramData\SmogAI'
```

Przed każdą zmianą powstaje kopia w
`C:\ProgramData\SmogAI\config-backups`. Skrypt nie wypisuje wartości sekretów.
Wymusza `SMOG_AI_LLM_ALLOW_RULE_FALLBACK=false`, aby błąd OpenAI nie był
ukrywany przez parser regułowy.
