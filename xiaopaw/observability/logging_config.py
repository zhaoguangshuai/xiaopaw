"""Structured logging configuration."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from xiaopaw.observability.pii_mask import mask_pii
from xiaopaw.observability.trace import trace_id_var


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter with trace_id and PII masking."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": trace_id_var.get("-"),
            "msg": mask_pii(record.getMessage()),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console format with trace_id."""

    def format(self, record: logging.LogRecord) -> str:
        tid = trace_id_var.get("-")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return f"{ts} [{tid[:8]}] {record.levelname:<5} {record.name}: {mask_pii(record.getMessage())}"


def setup_logging(
    log_dir: Path | None = None,
    level: int = logging.INFO,
    json_output: bool = True,
) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    if log_dir and json_output:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "xiaopaw.log", encoding="utf-8")
        fh.setFormatter(StructuredFormatter())
        root.addHandler(fh)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("lark_oapi").setLevel(logging.WARNING)
