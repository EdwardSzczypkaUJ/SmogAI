from __future__ import annotations

import json

from server.api.settings import ServerSettings
from server.database.store import DatabaseSnapshotStore


def main() -> None:
    settings = ServerSettings.from_env()
    if not settings.database_url:
        raise SystemExit(
            "SMOG_AI_SERVER_DATABASE_URL or DATABASE_URL is required for server migration"
        )
    store = DatabaseSnapshotStore(
        settings.database_url,
        keep_versions=settings.keep_versions,
        initialize=True,
    )
    store.ping()
    print(
        json.dumps(
            {
                "status": "ok",
                "operation": "server_database_migration",
                "storage_backend": store.backend_name,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
