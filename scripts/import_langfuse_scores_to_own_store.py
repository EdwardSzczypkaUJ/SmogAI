from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path


def value(row: object, *names: str):
    if not isinstance(row, dict):
        return None
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.project_root.resolve()))

    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    from server.api.settings import ServerSettings
    from server.application.runtime import create_artifact_repository_from_settings
    from smog_ai.observability.own_store import OwnAnalyticsStore

    settings = ServerSettings.from_env()
    repository = create_artifact_repository_from_settings(settings)
    store = OwnAnalyticsStore(
        repository, private_prefix=settings.own_analytics_private_prefix
    )
    digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
    marker_key = f"{settings.own_analytics_private_prefix}/imports/langfuse/{digest}.json"
    if repository.store.exists(marker_key):
        print(json.dumps({
            "status": "already_imported", "input": str(args.input),
            "sha256": digest, "marker_key": marker_key,
        }, ensure_ascii=False, indent=2))
        return 0
    with gzip.open(args.input, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    imported = 0
    skipped = 0
    for index, row in enumerate(payload.get("scores") or []):
        score = value(row, "value")
        if score is None:
            skipped += 1
            continue
        event_id = str(value(row, "id", "score_id") or f"langfuse-import-{index}")
        store.save_feedback(
            feedback_id=event_id,
            trace_id=str(value(row, "trace_id", "traceId") or "imported"),
            request_id=None,
            score=float(score),
            label="imported_from_langfuse",
            comment=value(row, "comment"),
            question=None,
        )
        imported += 1
    repository.put_json(marker_key, {
        "schema_version": "1.0", "source": "langfuse_export",
        "input_name": args.input.name, "sha256": digest,
        "imported": imported, "skipped": skipped,
    }, immutable=True)
    print(json.dumps({
        "status": "ok", "input": str(args.input), "imported": imported,
        "skipped": skipped, "sha256": digest, "marker_key": marker_key,
        "summary": store.summary(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
