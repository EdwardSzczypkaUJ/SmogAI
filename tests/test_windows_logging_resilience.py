from __future__ import annotations

import logging
from pathlib import Path

from smog_ai.logging_config import configure_logging


def test_locked_shared_log_uses_process_local_fallback(tmp_path: Path, monkeypatch) -> None:
    real_handler = logging.handlers.TimedRotatingFileHandler
    calls = []

    def handler(filename, *args, **kwargs):
        calls.append(Path(filename))
        if len(calls) == 1:
            raise PermissionError("locked")
        return real_handler(filename, *args, **kwargs)

    monkeypatch.setattr(logging.handlers, "TimedRotatingFileHandler", handler)
    configure_logging(tmp_path, task_name="parameter-catalog")
    logging.getLogger(__name__).info("stage continues")

    assert calls[0].name == "parameter-catalog.jsonl"
    assert calls[1].name.startswith("parameter-catalog-")
    assert calls[1].exists()


def test_reconfiguration_closes_previous_handlers(tmp_path: Path) -> None:
    configure_logging(tmp_path, task_name="first")
    previous = list(logging.getLogger().handlers)
    configure_logging(tmp_path, task_name="second")
    assert all(handler not in logging.getLogger().handlers for handler in previous)
