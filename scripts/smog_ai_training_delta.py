from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Running a file from ProjectRoot\scripts makes that directory sys.path[0].
# Add the actual project root explicitly before importing the local package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smog_ai.training_delta import (
    CONFIRMATION,
    build_delta,
    plan_delta,
    verify_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--profile", default="quick", choices=("quick", "full"))
    parser.add_argument("--action", default="plan", choices=("plan", "build", "verify"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--no-verify-hashes", action="store_true")
    args = parser.parse_args()

    runtime = Path(args.runtime_root)
    if args.action == "plan":
        result = plan_delta(runtime_root=runtime, profile=args.profile)
    elif args.action == "build":
        if not args.apply:
            result = plan_delta(runtime_root=runtime, profile=args.profile)
            result["requested_action"] = "build"
            result["apply_required"] = True
            result["required_confirmation"] = CONFIRMATION
        else:
            result = build_delta(
                runtime_root=runtime,
                profile=args.profile,
                confirmation=args.confirmation,
            )
    else:
        result = verify_candidate(
            runtime_root=runtime,
            profile=args.profile,
            verify_hashes=not args.no_verify_hashes,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"ok", "ready"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
