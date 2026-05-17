#!/usr/bin/env python3
"""
scripts/ops/replay.py

WI-58 — Incident Replay CLI.

Read-only operator command that reconstructs a bounded UTC time window
from the operational event ledger and prints a chronological,
secret-safe replay.

Usage:

    python -m scripts.ops.replay \\
        --from 2026-05-15T00:00:00Z \\
        --to   2026-05-15T01:00:00Z \\
        [--severity WARNING --severity ERROR] \\
        [--source EVALUATION] \\
        [--event-type DECISION_SKIPPED] \\
        [--reason-code DECISION_SKIP_LOW_CONF] \\
        [--limit 500]

Filter values are case-insensitive; multiple flags may be passed to OR
within a category. Categories are intersected.

Constraints (enforced by code review and tests):

* Never appends, mutates, or deletes operational events.
* Never imports LLM clients, execution routing, signing, broadcasting,
  or live wallet mutation paths.
* Never performs trading, sizing, EV, Kelly, PnL, exposure, or
  provider-cost calculations.
* Output is scanned for forbidden secret / high-cardinality content at
  the schema boundary before printing.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

# Allow `python scripts/ops/replay.py` direct invocation.
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from src.observability.incident_replay import (  # noqa: E402
    format_report_lines,
    run_replay,
)
from src.schemas.ops import (  # noqa: E402
    IncidentReplayFailureReason,
    IncidentReplayFilter,
    IncidentReplayRequest,
    IncidentReplayStatus,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    _scan_event_payload,
)


_MAX_ECHOED_VALUE_LEN: int = 32


def _safe_echo(value: str) -> str:
    """Return a value safe to echo back to operators in error text.

    If the value matches any forbidden secret/high-cardinality pattern
    or exceeds the length cap, return a typed redaction placeholder
    instead. This prevents the CLI from echoing API keys, telegram
    tokens, wallet addresses, condition IDs, or other secret-shaped
    input that an operator may have pasted by mistake.
    """
    if _scan_event_payload(value):
        return "<redacted>"
    if len(value) > _MAX_ECHOED_VALUE_LEN:
        return "<redacted-length>"
    return value


def _scrub_argparse_message(message: str) -> str:
    """Sanitize an argparse-formatted error message before printing.

    argparse leaks raw operator input in two formats:

    * type-conversion / choice failures embed the value in single
      quotes, e.g. ``invalid int value: 'sk-...'``;
    * ``unrecognized arguments: ...`` and similar paths append the
      raw tokens unquoted, separated by whitespace.

    To stay safe against both, every whitespace-separated token in
    the message is run through ``_safe_echo`` — both with and without
    surrounding quotes — so any secret-shaped or overlong fragment is
    replaced with a typed redaction placeholder before it reaches the
    operator's terminal.
    """
    safe_tokens: list[str] = []
    for token in message.split():
        quote = ""
        inner = token
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            quote = token[0]
            inner = token[1:-1]
        echoed = _safe_echo(inner)
        if echoed != inner:
            safe_tokens.append(f"{quote}{echoed}{quote}")
        else:
            safe_tokens.append(token)
    return " ".join(safe_tokens)


class _SafeArgParser(argparse.ArgumentParser):
    """argparse subclass that scrubs error output before printing.

    argparse's default ``error()`` prints the raw user input to stderr
    and calls ``sys.exit(2)`` before any caller code can intervene.
    We override ``error()`` to (1) scrub any embedded raw input via
    ``_scrub_argparse_message``, (2) emit a typed status/failure
    block on stdout in the same shape as the rest of the CLI, and
    (3) exit with the typed ``EXIT_INVALID_INPUT`` code.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        scrubbed = _scrub_argparse_message(message)
        print(f"status: {IncidentReplayStatus.INVALID_WINDOW.value}")
        print(f"failure_reason: {IncidentReplayFailureReason.UNKNOWN_ENUM_VALUE.value}")
        print(f"note: {scrubbed}")
        raise SystemExit(EXIT_INVALID_INPUT)


# Exit code conventions ----------------------------------------------------
EXIT_OK: int = 0
EXIT_INVALID_INPUT: int = 2
EXIT_REPOSITORY: int = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgParser(
        prog="replay",
        description="Read-only incident replay over the operational event ledger.",
    )
    parser.add_argument(
        "--from",
        dest="from_iso",
        required=True,
        help="Window start (ISO-8601 UTC; trailing 'Z' allowed).",
    )
    parser.add_argument(
        "--to",
        dest="to_iso",
        required=True,
        help="Window end (ISO-8601 UTC; trailing 'Z' allowed).",
    )
    parser.add_argument(
        "--severity",
        action="append",
        default=[],
        help="Typed severity filter (repeatable).",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Typed source-component filter (repeatable).",
    )
    parser.add_argument(
        "--event-type",
        dest="event_types",
        action="append",
        default=[],
        help="Typed event-type filter (repeatable).",
    )
    parser.add_argument(
        "--reason-code",
        dest="reason_codes",
        action="append",
        default=[],
        help="Typed reason-code filter (repeatable).",
    )
    parser.add_argument(
        "--limit",
        default="1000",
        help="Maximum replay lines (1..1000). Default: 1000.",
    )
    return parser


def _parse_iso_utc(value: str, label: str) -> datetime:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime.

    Accepts trailing 'Z'. Rejects naive timestamps. Normalizes non-UTC
    offsets to UTC.
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # noqa: B904 — typed error wanted
        raise _CLIInputError(
            IncidentReplayFailureReason.MALFORMED_TIMESTAMP,
            f"{label} is not a valid ISO-8601 timestamp",
        ) from None
    if parsed.tzinfo is None:
        raise _CLIInputError(
            IncidentReplayFailureReason.NAIVE_TIMESTAMP,
            f"{label} must include timezone information",
        )
    return parsed.astimezone(timezone.utc)


def _parse_enum_list(values: Sequence[str], enum_cls, label: str) -> Optional[list]:
    """Parse a list of typed enum string values, splitting on comma.

    Returns ``None`` if the user passed no values for this category.
    Raises ``_CLIInputError`` on any unknown value.
    """
    if not values:
        return None
    out: list = []
    for raw in values:
        for piece in raw.split(","):
            stripped = piece.strip()
            if not stripped:
                continue
            try:
                out.append(enum_cls(stripped))
            except ValueError:
                # Try common case-folding for operator ergonomics.
                upper = stripped.upper()
                try:
                    out.append(enum_cls(upper))
                except ValueError:
                    allowed = ", ".join(sorted(m.value for m in enum_cls))
                    raise _CLIInputError(
                        IncidentReplayFailureReason.UNKNOWN_ENUM_VALUE,
                        f"{label} value {_safe_echo(stripped)!r} is not allowed; "
                        f"valid values: {allowed}",
                    ) from None
    return out or None


class _CLIInputError(Exception):
    """Internal typed error for CLI argument validation."""

    def __init__(
        self,
        failure_reason: IncidentReplayFailureReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.safe_message = message


def _build_request(args: argparse.Namespace) -> IncidentReplayRequest:
    """Build a typed replay request from parsed CLI arguments."""
    from_utc = _parse_iso_utc(args.from_iso, "--from")
    to_utc = _parse_iso_utc(args.to_iso, "--to")

    severities = _parse_enum_list(args.severity, OperationalEventSeverity, "--severity")
    sources = _parse_enum_list(args.source, OperationalEventSource, "--source")
    event_types = _parse_enum_list(
        args.event_types, OperationalEventType, "--event-type"
    )
    reason_codes = _parse_enum_list(
        args.reason_codes, OperationalEventReasonCode, "--reason-code"
    )

    filter_obj = IncidentReplayFilter(
        severities=severities,
        sources=sources,
        event_types=event_types,
        reason_codes=reason_codes,
    )

    try:
        limit_value = int(str(args.limit))
    except (TypeError, ValueError):
        raise _CLIInputError(
            IncidentReplayFailureReason.LIMIT_OUT_OF_RANGE,
            "--limit must be an integer between 1 and 1000",
        ) from None
    if limit_value < 1 or limit_value > 1000:
        raise _CLIInputError(
            IncidentReplayFailureReason.LIMIT_OUT_OF_RANGE,
            "--limit must be between 1 and 1000",
        )

    try:
        return IncidentReplayRequest(
            from_utc=from_utc,
            to_utc=to_utc,
            filter=filter_obj,
            limit=limit_value,
        )
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc", ())
            if loc and loc[0] == "limit":
                raise _CLIInputError(
                    IncidentReplayFailureReason.LIMIT_OUT_OF_RANGE,
                    "--limit must be between 1 and 1000",
                ) from None
        # Window validation errors land here (e.g., from > to).
        raise _CLIInputError(
            IncidentReplayFailureReason.FROM_AFTER_TO,
            "--from must be earlier than or equal to --to",
        ) from None


def _status_to_exit_code(status: IncidentReplayStatus) -> int:
    if status in (
        IncidentReplayStatus.SUCCESS,
        IncidentReplayStatus.EMPTY_WINDOW,
        IncidentReplayStatus.TRUNCATED,
    ):
        return EXIT_OK
    if status in (
        IncidentReplayStatus.INVALID_WINDOW,
        IncidentReplayStatus.INVALID_TIMESTAMP,
        IncidentReplayStatus.INVALID_FILTER,
    ):
        return EXIT_INVALID_INPUT
    return EXIT_REPOSITORY


def _input_failure_status(reason: IncidentReplayFailureReason) -> IncidentReplayStatus:
    if reason == IncidentReplayFailureReason.FROM_AFTER_TO:
        return IncidentReplayStatus.INVALID_WINDOW
    if reason in (
        IncidentReplayFailureReason.MALFORMED_TIMESTAMP,
        IncidentReplayFailureReason.NAIVE_TIMESTAMP,
    ):
        return IncidentReplayStatus.INVALID_TIMESTAMP
    if reason == IncidentReplayFailureReason.UNKNOWN_ENUM_VALUE:
        return IncidentReplayStatus.INVALID_FILTER
    if reason == IncidentReplayFailureReason.LIMIT_OUT_OF_RANGE:
        # --limit is an input argument, not a filter or window — route via
        # INVALID_WINDOW for exit-code grouping but keep the typed reason.
        return IncidentReplayStatus.INVALID_WINDOW
    return IncidentReplayStatus.REPOSITORY_FAILURE


async def _run_async(
    request: IncidentReplayRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    report = await run_replay(request, session_factory)
    for line in format_report_lines(report):
        print(line)
    return _status_to_exit_code(report.status)


def _run_until_complete(coro) -> int:
    """Run an awaitable to completion regardless of caller event loop state.

    Tests that exercise the CLI from an async context already hold a
    running loop; ``asyncio.run`` refuses to nest. In that case we spin
    a worker thread with its own loop.
    """
    try:
        import asyncio as _asyncio
        _asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    result: dict[str, int] = {}

    def _worker() -> None:
        result["code"] = asyncio.run(coro)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    return result.get("code", EXIT_REPOSITORY)


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    """Resolve the runtime async session factory lazily.

    Import the engine module only when the CLI actually needs a database
    connection. Tests pass an override and never trigger this path, so
    they never require database configuration.
    """
    from src.db.engine import AsyncSessionLocal  # noqa: WPS433 — deferred import

    return AsyncSessionLocal


def main(
    argv: Optional[Sequence[str]] = None,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> int:
    """Entrypoint suitable for direct invocation or unit testing.

    Returns a process exit code; never raises for typed CLI input errors.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on missing required args
        return int(exc.code) if exc.code is not None else EXIT_INVALID_INPUT

    try:
        request = _build_request(args)
    except _CLIInputError as exc:
        status = _input_failure_status(exc.failure_reason)
        print(f"status: {status.value}")
        print(f"failure_reason: {exc.failure_reason.value}")
        print(f"note: {exc.safe_message}")
        return _status_to_exit_code(status)

    factory = session_factory if session_factory is not None else _default_session_factory()
    try:
        return _run_until_complete(_run_async(request, factory))
    except KeyboardInterrupt:
        return EXIT_REPOSITORY


if __name__ == "__main__":  # pragma: no cover — exercised via tests + ops
    raise SystemExit(main())
