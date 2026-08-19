from __future__ import annotations

from sqlalchemy import inspect

from smog_ai.config import AppConfig
from smog_ai.database.engine import create_db_engine, init_database


def test_operational_schema_init_skips_full_sqlite_quick_check(
    app_config: AppConfig,
    monkeypatch,
) -> None:
    engine = create_db_engine(app_config)

    def reject_quick_check(_: str):  # type: ignore[no-untyped-def]
        raise AssertionError("operational schema init invoked PRAGMA quick_check")

    monkeypatch.setattr("smog_ai.database.engine.text", reject_quick_check)
    init_database(engine, verify_integrity=False)

    assert "air_measurements" in inspect(engine).get_table_names()
