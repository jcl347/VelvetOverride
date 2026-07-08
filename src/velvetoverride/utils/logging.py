"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_dir: str | None = None,
) -> None:
    """Configure structlog with console or JSON rendering.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, render logs as JSON instead of colored console.
        log_dir: If provided, also write JSON logs to a file in this directory.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # Set up file logging if a log directory is specified
    if log_dir:
        _setup_file_handler(log_dir, level)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _setup_file_handler(log_dir: str, level: str) -> None:
    """Add a file handler to the stdlib root logger for persistent logs."""
    from datetime import datetime, timezone

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    filename = f"velvetoverride_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
    file_path = log_path / filename

    handler = logging.FileHandler(str(file_path))
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
