from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_STANDARD_LOG_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
}


class JsonFormatter(logging.Formatter):
    """JSON formatter preserving structured progress fields.

    Historical imports report year, pollutant, station, page, counts and ETA
    through ``logging`` ``extra`` fields.  Dropping those fields made the
    progress monitor blind and left only an unhelpful message.  Include every
    non-standard, JSON-safe field so all Bridge implementations expose the same
    observable progress contract.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    log_dir: Path,
    level: str = "INFO",
    task_name: str = "application",
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Closing matters on Windows: clearing the list alone leaves an open file
    # handle until garbage collection and can block the next scheduled stage.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass

    # stdout avoids Windows PowerShell 5.1 wrapping ordinary INFO messages in
    # NativeCommandError records merely because logging.StreamHandler defaults
    # to stderr.
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter())
    root.addHandler(console)

    common_options = {
        "when": "midnight",
        "interval": 1,
        "backupCount": 30,
        "encoding": "utf-8",
        "utc": True,
    }
    primary_path = log_dir / f"{task_name}.jsonl"
    try:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            primary_path, **common_options
        )
    except (PermissionError, OSError) as exc:
        # Streamlit, Defender or another process may temporarily hold the
        # shared task log. Logging is auxiliary and must not abort E1/training.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        fallback_path = log_dir / f"{task_name}-{os.getpid()}-{stamp}.jsonl"
        try:
            file_handler = logging.handlers.TimedRotatingFileHandler(
                fallback_path, **common_options
            )
            console.handle(
                logging.LogRecord(
                    name=__name__, level=logging.WARNING, pathname=__file__,
                    lineno=0,
                    msg=(
                        f"Shared log is locked ({primary_path}); using "
                        f"process-local log {fallback_path}: {exc}"
                    ),
                    args=(), exc_info=None,
                )
            )
        except OSError as fallback_exc:
            # Console and the automation stage log still preserve output.
            console.handle(
                logging.LogRecord(
                    name=__name__, level=logging.WARNING, pathname=__file__,
                    lineno=0,
                    msg=(
                        "File logging unavailable; primary work continues: "
                        f"{fallback_exc}"
                    ),
                    args=(), exc_info=None,
                )
            )
            return
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
