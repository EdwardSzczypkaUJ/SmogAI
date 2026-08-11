#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smog_ai.observability.evaluation import (
    PromptExpectation,
    evaluate_query_response,
)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("cases") or []
    if not isinstance(payload, list):
        raise TypeError("Prompt evaluation dataset must be a list or {'cases': [...]}")
    return [dict(item) for item in payload]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-average-score", type=float, default=0.85)
    parser.add_argument("--submit-feedback", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    args = parser.parse_args(argv)

    base = args.api_url.rstrip("/")
    cases = _load_cases(args.dataset)
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout_seconds) as client:
        for index, case in enumerate(cases, start=1):
            question = str(case["text"])
            expected = dict(case.get("expected") or {})
            expectation = PromptExpectation(
                parameters=tuple(str(v) for v in expected.get("parameters") or []),
                place_contains=expected.get("place_contains"),
                require_exact_time=bool(expected.get("require_exact_time", False)),
                minimum_forecasts=int(expected.get("minimum_forecasts", 1)),
            )
            try:
                response = client.post(
                    f"{base}/query",
                    json={
                        "text": question,
                        "session_id": "prompt-evaluation",
                        "user_id": "automated-evaluator",
                    },
                )
                response.raise_for_status()
                payload = dict(response.json())
                evaluation = evaluate_query_response(payload, expectation)
                row = {
                    "case_id": case.get("id") or f"case-{index}",
                    "question": question,
                    "status": "ok",
                    "score": evaluation.score,
                    "checks": evaluation.checks,
                    "details": evaluation.details,
                    "trace_id": payload.get("trace_id"),
                    "request_id": payload.get("request_id"),
                    "summary": payload.get("summary"),
                }
                if args.submit_feedback and payload.get("trace_id"):
                    feedback = client.post(
                        f"{base}/feedback",
                        json={
                            "trace_id": payload["trace_id"],
                            "request_id": payload.get("request_id"),
                            "score": evaluation.score,
                            "label": "automated-structural-evaluation",
                            "comment": json.dumps(
                                evaluation.checks,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            "question": question,
                            "session_id": "prompt-evaluation",
                            "user_id": "automated-evaluator",
                            "metadata": {
                                "case_id": row["case_id"],
                                "evaluator": "deterministic-structure-v1",
                            },
                        },
                    )
                    row["feedback_status"] = feedback.status_code
                    if feedback.is_success:
                        row["feedback"] = feedback.json()
                rows.append(row)
            except Exception as exc:
                rows.append(
                    {
                        "case_id": case.get("id") or f"case-{index}",
                        "question": question,
                        "status": "error",
                        "score": 0.0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    scores = [float(row["score"]) for row in rows]
    average = sum(scores) / len(scores) if scores else 0.0
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "api_url": base,
        "dataset": str(args.dataset.resolve()),
        "cases": rows,
        "case_count": len(rows),
        "average_score": average,
        "minimum_average_score": args.minimum_average_score,
        "passed": bool(rows) and average >= args.minimum_average_score and all(
            row["status"] == "ok" for row in rows
        ),
        "submitted_to_feedback_endpoint": bool(args.submit_feedback),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
