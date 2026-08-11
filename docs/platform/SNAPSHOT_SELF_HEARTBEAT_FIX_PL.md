# HF19.2 — diagnoza zapętlonego snapshotu

## Rzeczywista przyczyna

Nie był potrzebny żaden zewnętrzny importer. Polecenia snapshotowe były
opakowane w `ProcessLease`, którego wątek heartbeat co 30 sekund aktualizował
wiersz `process_locks` w tej samej żywej SQLite, z której działało
`sqlite3.Connection.backup()`.

Sekwencja była następująca:

```text
snapshot start
→ ProcessLease zapisuje właściciela
→ backup kopiuje niemal całą bazę
→ heartbeat aktualizuje process_locks
→ SQLite wykrywa zmianę źródła i restartuje backup
→ monitor nie cofa procentu, więc pozostaje np. na 67,71%
→ kolejny heartbeat powtarza cykl
```

`ProgressReporter` celowo wymusza monotoniczny procent, dlatego restart nie był
widoczny jako spadek do początku.

## Naprawa

- snapshot używa `ProcessLease(..., heartbeat_enabled=False)`;
- normalny odnawialny heartbeat zaczyna się dopiero po powstaniu kopii;
- callback raportuje `backup_restarts` i rzeczywisty postęp bieżącej próby;
- po przekroczeniu limitu restartów albo czasu bez postępu proces kończy się
  czytelnym błędem zamiast wisieć bez końca.

## Zakres danych

Snapshot pozostaje lokalnym plikiem SQLite. HF19.2 nie zmienia polityki
local-only i nie włącza publikacji do DigitalOcean.
