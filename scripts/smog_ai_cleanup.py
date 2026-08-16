from __future__ import annotations

import argparse
import json
from pathlib import Path

from smog_ai_automation import cleanup_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Bezpieczna retencja artefaktów SmogAI")
    parser.add_argument("--runtime-root", default=r"C:\ProgramData\SmogAI")
    parser.add_argument("--apply", action="store_true", help="Wykonaj usuwanie; bez tej flagi działa DryRun")
    parser.add_argument("--keep-training-quick", type=int, default=2)
    parser.add_argument("--keep-training-full", type=int, default=3)
    parser.add_argument("--keep-dashboard-snapshots", type=int, default=5)
    parser.add_argument("--keep-forecast-publications", type=int, default=10)
    parser.add_argument("--keep-map-surface-sets", type=int, default=5)
    parser.add_argument("--keep-automation-runs", type=int, default=30)
    parser.add_argument("--progress-retention-days", type=int, default=30)
    args = parser.parse_args()
    policy = {
        "training_quick": args.keep_training_quick,
        "training_full": args.keep_training_full,
        "dashboard_snapshots": args.keep_dashboard_snapshots,
        "forecast_publications": args.keep_forecast_publications,
        "map_surface_sets": args.keep_map_surface_sets,
        "automation_runs": args.keep_automation_runs,
        "progress_days": args.progress_retention_days,
    }
    report = cleanup_runtime(Path(args.runtime_root), apply=args.apply, policy=policy)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
