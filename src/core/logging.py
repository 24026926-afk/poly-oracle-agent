"""
src/core/logging.py

Structlog configuration for poly-oracle-agent.

Call ``configure_logging()`` once at application startup (from
orchestrator.py) before any log messages are emitted.
"""

import logging

import structlog
from structlog.contextvars import bind_contextvars


def configure_logging(log_level: str = "INFO") -> None:
    """Wire up structlog processors and stdlib logging bridge.

    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR.  When DEBUG,
            the console renderer is used for human-readable output;
            otherwise JSON is emitted for machine parsing.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Choose final renderer based on log level
    if log_level.upper() == "DEBUG":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging so third-party libs (aiohttp, sqlalchemy) also
    # flow through structlog processors.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Bind global context available to every log line
    bind_contextvars(app="poly-oracle-agent")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for *name*."""
    return structlog.get_logger(name)
