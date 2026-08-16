from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# When this file is launched by absolute path on Windows, Python places only
# the scripts directory on sys.path. Add the project root explicitly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smog_ai.training_compaction import (
    apply_compaction,
    plan_compaction,
    rollback_compaction,
    verify_compaction,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe SmogAI base + delta compaction")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--action", choices=("plan", "apply", "verify", "rollback"), default="plan")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    if args.action == "plan":
        payload = plan_compaction(runtime_root=args.runtime_root, profile=args.profile)
    elif args.action == "apply":
        payload = apply_compaction(
            runtime_root=args.runtime_root,
            profile=args.profile,
            confirmation=args.confirmation,
        )
    elif args.action == "verify":
        payload = verify_compaction(runtime_root=args.runtime_root, profile=args.profile)
    else:
        payload = rollback_compaction(
            runtime_root=args.runtime_root,
            profile=args.profile,
            confirmation=args.confirmation,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("status") in {"ready", "ok"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
