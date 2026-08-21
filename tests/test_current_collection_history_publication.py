from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from smog_ai.database.engine import session_scope
from smog_ai.database.repository import set_application_state
from smog_ai.operations import build_public_operations_status


def test_current_source_state_is_included_in_published_history(
    engine,
    app_config,
) -> None:
    generated = datetime.now(UTC)
    with session_scope(engine) as session:
        set_application_state(
            session,
            "last_gios_success_at",
            (generated - timedelta(hours=1)).isoformat(),
        )
        set_application_state(
            session,
            "last_imgw_success_at",
            (generated - timedelta(hours=2)).isoformat(),
        )
        session.flush()
        payload = build_public_operations_status(
            session,
            app_config,
            source_origin_time=generated - timedelta(hours=3),
            generated_at=generated,
            surface_count=240,
        )

    rows = [
        row
        for row in payload["collection_history"]["freshness_checks"]
        if row.get("release_snapshot") is True
    ]
    assert {row["source"] for row in rows} == {"GIOS", "IMGW"}
    assert {row["generated_at"] for row in rows} == {
        generated.isoformat().replace("+00:00", "Z")
    }
    assert all(row["maximum_collection_age_hours"] is not None for row in rows)


def test_serving_collects_freshness_before_building_manifest() -> None:
    root = Path(__file__).resolve().parents[1]
    automation = (root / "scripts" / "smog_ai_automation.py").read_text(
        encoding="utf-8-sig"
    )
    serving_block = automation.split('if profile == "serving":', 1)[1].split(
        "return stages", 1
    )[0]
    assert serving_block.index('"data-freshness-report"') < serving_block.index(
        '"build-spatial-surfaces"'
    )


def test_manual_publisher_updates_canonical_freshness_history() -> None:
    root = Path(__file__).resolve().parents[1]
    publisher = (
        root / "scripts" / "Publish-SmogAI-ServingToDigitalOcean.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "$CanonicalFreshnessRoot" in publisher
    assert "--output-dir $CanonicalFreshnessRoot" in publisher
