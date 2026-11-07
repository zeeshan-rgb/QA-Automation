"""Logging with mandatory secret redaction."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .redaction import scrub

_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """Scrubs the fully formatted message, including exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - bad format args
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configure_logging(log_dir: Path | None, level: int = logging.INFO) -> None:
    global _CONFIGURED
    root = logging.getLogger("pavs")
    if _CONFIGURED:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        handlers.append(logging.handlers.RotatingFileHandler(
            log_dir / "pavs.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"pavs.{name}")
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
