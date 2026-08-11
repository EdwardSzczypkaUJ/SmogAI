# HF20.4 — idempotentna publikacja zatwierdzonych modeli

## Przyczyna błędu

Wersjonowany klucz `model-card.json` jest immutable, ale HF20 umieszczał w jego
treści zmienny `published_at`. Po pierwszej lub częściowej publikacji do
`metrics_json` dochodziło również `remote_artifact`. Ponowienie tej samej wersji
modelu generowało więc inne bajty i prawidłowo uruchamiało
`ObjectConflictError`.

HF20.4:

- usuwa czas publikacji z immutable model card;
- usuwa transportowe `metrics.remote_artifact` z immutable card;
- pozostawia `published_at` w mutowalnym `active.json`;
- rozpoznaje legacy card różniącą się wyłącznie tymi polami;
- nadal odrzuca rzeczywistą zmianę providera, checksumu, datasetu, metryk lub
  kontraktu czasu;
- obsługuje ponowienie po częściowej publikacji bez kasowania ObjectStore.

Nie należy ręcznie usuwać lokalnego model-card ani całego `local-object-store`.
