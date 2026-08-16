from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the public answer-quality aggregate from private events."
    )
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.project_root.resolve()))

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)
    from server.api.settings import ServerSettings
    from server.application.runtime import create_artifact_repository_from_settings
    from smog_ai.observability.own_store import OwnAnalyticsStore

    settings = ServerSettings.from_env()
    repository = create_artifact_repository_from_settings(settings)
    analytics = OwnAnalyticsStore(
        repository, private_prefix=settings.own_analytics_private_prefix
    )
    print(json.dumps(analytics.rebuild_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
