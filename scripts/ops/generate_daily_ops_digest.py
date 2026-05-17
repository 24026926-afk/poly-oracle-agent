#!/usr/bin/env python3
"""
scripts/ops/generate_daily_ops_digest.py

WI-60 — Daily Operations Digest CLI.

Read-only operator command that produces a deterministic daily digest
file at ``03_Daily/YYYY-MM-DD-bot.md`` from the operational event ledger
and (when present) the position repository. The CLI never overwrites
manual coding daily notes at ``03_Daily/YYYY-MM-DD.md``.

Usage:

    python -m scripts.ops.generate_daily_ops_digest \\
        --date 2026-05-15 \\
        [--from-utc 2026-05-15T00:00:00Z] \\
        [--to-utc   2026-05-15T23:59:59Z] \\
        [--output 03_Daily/2026-05-15-bot.md] \\
        [--daily-notes-dir 03_Daily] \\
        [--enable-telegram]

The default window is the full UTC calendar day implied by ``--date``.
Operators may override the bounds with ``--from-utc`` / ``--to-utc``;
both must be ISO-8601 UTC timestamps and must straddle a single day so
the resulting bot digest filename remains unambiguous.

Exit codes:

* ``0`` — digest written successfully (including EMPTY_WINDOW).
* ``2`` — invalid CLI input (date, output path, manual-note collision).
* ``3`` — repository or database failure.
* ``4`` — forbidden content blocked digest generation.

Constraints (enforced by code review and tests):

* Never appends, mutates, or deletes operational events.
* Never imports LLM clients, execution routing, signing, broadcasting,
  or live wallet mutation paths.
* Never performs trading, sizing, EV, Kelly, PnL, exposure, or
  provider-cost calculations.
* Never overwrites the manual coding daily note path.
* Output is scanned for forbidden secret / high-cardinality content at
  the schema boundary AND on the final rendered text before writing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

# Allow ``python scripts/ops/generate_daily_ops_digest.py`` direct invocation.
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from src.observability.daily_ops_digest import generate_digest  # noqa: E402
from src.schemas.ops import (  # noqa: E402
    DailyOpsDigestFailureReason,
    DailyOpsDigestRequest,
    DailyOpsDigestStatus,
    _scan_event_payload,
)


# Exit code conventions ------------------------------------------------------
EXIT_OK: int = 0
EXIT_INVALID_INPUT: int = 2
EXIT_REPOSITORY: int = 3
EXIT_FORBIDDEN: int = 4

_MAX_ECHOED_VALUE_LEN: int = 32


def _safe_echo(value: str) -> str:
    """Return a value safe to echo back in error text, or a redaction tag."""
    if _scan_event_payload(value):
        return "<redacted>"
    if len(value) > _MAX_ECHOED_VALUE_LEN:
        return "<redacted-length>"
    return value


def _scrub_argparse_message(message: str) -> str:
    """Sanitize an argparse-formatted error message before printing."""
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
    """argparse subclass that scrubs raw user input from error output."""

    def error(self, message: str) -> None:  # type: ignore[override]
        scrubbed = _scrub_argparse_message(message)
        print(f"status: {DailyOpsDigestStatus.INVALID_REQUEST.value}")
        print(f"failure_reason: {DailyOpsDigestFailureReason.INVALID_DATE.value}")
        print(f"note: {scrubbed}")
        raise SystemExit(EXIT_INVALID_INPUT)


class _CLIInputError(Exception):
    """Internal typed error for CLI argument validation."""

    def __init__(
        self,
        failure_reason: DailyOpsDigestFailureReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.safe_message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgParser(
        prog="generate_daily_ops_digest",
        description="Generate a deterministic daily ops digest at 03_Daily/YYYY-MM-DD-bot.md.",
    )
    parser.add_argument(
        "--date",
        dest="date",
        required=False,
        default=None,
        help="Digest calendar date in YYYY-MM-DD (UTC). Defaults to today UTC.",
    )
    parser.add_argument(
        "--from-utc",
        dest="from_utc",
        default=None,
        help=(
            "Optional explicit window start (ISO-8601 UTC, e.g. "
            "2026-05-15T00:00:00Z). Must be paired with --to-utc and must "
            "fall on the same UTC calendar day as --date."
        ),
    )
    parser.add_argument(
        "--to-utc",
        dest="to_utc",
        default=None,
        help=(
            "Optional explicit window end (ISO-8601 UTC). Must be paired "
            "with --from-utc."
        ),
    )
    parser.add_argument(
        "--output",
        dest="output",
        default=None,
        help="Optional explicit output path; must end with YYYY-MM-DD-bot.md.",
    )
    parser.add_argument(
        "--daily-notes-dir",
        dest="daily_notes_dir",
        default="03_Daily",
        help="Daily notes directory (absolute or vault-relative). Default: 03_Daily.",
    )
    parser.add_argument(
        "--enable-telegram",
        dest="enable_telegram",
        action="store_true",
        help=(
            "Request optional Telegram digest delivery. Still requires "
            "Telegram alerts to be enabled at config level."
        ),
    )
    return parser


def _parse_date(value: Optional[str]) -> datetime:
    """Parse a YYYY-MM-DD string into a UTC-aware datetime at midnight."""
    if value is None or value.strip() == "":
        # Default: today's UTC date at 00:00.
        return datetime.combine(
            datetime.now(timezone.utc).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            "--date must be a valid YYYY-MM-DD UTC date",
        ) from None
    return parsed.replace(tzinfo=timezone.utc)


def _parse_iso_utc(label: str, value: str):
    """Parse an ISO-8601 UTC timestamp from CLI input.

    Returns a tz-aware UTC datetime. Raises ``_CLIInputError`` on any
    parse failure or if the value is not strictly UTC.
    """
    text = value.strip()
    if not text:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            f"{label} must be a non-empty ISO-8601 UTC timestamp",
        )
    candidate = text
    # Accept the trailing-Z form alongside +00:00.
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            f"{label} must be ISO-8601 UTC (e.g. 2026-05-15T00:00:00Z)",
        ) from None
    if parsed.tzinfo is None:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            f"{label} must include an explicit UTC timezone",
        )
    if parsed.utcoffset() != timedelta(0):
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            f"{label} must be in UTC (offset 0)",
        )
    return parsed.astimezone(timezone.utc)


def _build_request(args: argparse.Namespace) -> DailyOpsDigestRequest:
    """Build a typed digest request from parsed CLI arguments."""
    digest_date = _parse_date(args.date)

    window = None
    has_from = bool(getattr(args, "from_utc", None))
    has_to = bool(getattr(args, "to_utc", None))
    if has_from ^ has_to:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            "--from-utc and --to-utc must be supplied together",
        )
    if has_from and has_to:
        from_dt = _parse_iso_utc("--from-utc", args.from_utc)
        to_dt = _parse_iso_utc("--to-utc", args.to_utc)
        if from_dt >= to_dt:
            raise _CLIInputError(
                DailyOpsDigestFailureReason.INVALID_DATE,
                "--from-utc must be strictly earlier than --to-utc",
            )
        # Both bounds must fall on the same UTC calendar day so the
        # filename derived from --date stays unambiguous.
        if from_dt.date() != digest_date.date() or to_dt.date() != digest_date.date():
            raise _CLIInputError(
                DailyOpsDigestFailureReason.INVALID_DATE,
                "--from-utc and --to-utc must fall on the same UTC day as --date",
            )
        from src.schemas.ops import DailyOpsDigestWindow

        try:
            window = DailyOpsDigestWindow(from_utc=from_dt, to_utc=to_dt)
        except ValidationError:
            raise _CLIInputError(
                DailyOpsDigestFailureReason.INVALID_DATE,
                "explicit UTC window failed schema validation",
            ) from None

    try:
        return DailyOpsDigestRequest(
            digest_date_utc=digest_date,
            window=window,
            output_path=args.output if args.output else None,
            daily_notes_dir=args.daily_notes_dir,
            enable_telegram=bool(args.enable_telegram),
        )
    except ValidationError:
        raise _CLIInputError(
            DailyOpsDigestFailureReason.INVALID_DATE,
            "--date must be timezone-aware UTC and well-formed",
        ) from None


def _status_to_exit_code(status: DailyOpsDigestStatus) -> int:
    if status in (
        DailyOpsDigestStatus.SUCCESS,
        DailyOpsDigestStatus.EMPTY_WINDOW,
    ):
        return EXIT_OK
    if status in (
        DailyOpsDigestStatus.PATH_FAILURE,
        DailyOpsDigestStatus.INVALID_REQUEST,
    ):
        return EXIT_INVALID_INPUT
    if status in (
        DailyOpsDigestStatus.DATABASE_UNAVAILABLE,
        DailyOpsDigestStatus.MISSING_TABLE,
        DailyOpsDigestStatus.REPOSITORY_FAILURE,
        DailyOpsDigestStatus.READ_CAP_REACHED,
    ):
        return EXIT_REPOSITORY
    if status == DailyOpsDigestStatus.FORBIDDEN_CONTENT:
        return EXIT_FORBIDDEN
    return EXIT_REPOSITORY


def _print_report_summary(report) -> None:
    """Print a compact, secret-safe summary of the digest outcome."""
    print(f"status: {report.status.value}")
    if report.failure_reason is not None:
        print(f"failure_reason: {report.failure_reason.value}")
    if report.message:
        print(f"note: {report.message}")
    print(f"output_path: {report.write_result.path}")
    print(f"written: {str(report.write_result.written).lower()}")
    print(f"bytes_written: {report.write_result.bytes_written}")
    print(f"telegram_status: {report.telegram_result.status}")


async def _run_async(
    request: DailyOpsDigestRequest,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_notifier=None,
    daily_notes_root: Optional[Path] = None,
) -> int:
    report = await generate_digest(
        request,
        session_factory,
        telegram_notifier=telegram_notifier,
        daily_notes_root=daily_notes_root,
    )
    _print_report_summary(report)
    return _status_to_exit_code(report.status)


def _run_until_complete(coro) -> int:
    """Run an awaitable to completion regardless of caller event loop state."""
    try:
        asyncio.get_running_loop()
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
    """Resolve the runtime async session factory lazily."""
    from src.db.engine import AsyncSessionLocal  # noqa: WPS433 — deferred import

    return AsyncSessionLocal


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    date: Optional[str] = None,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    telegram_notifier=None,
    daily_notes_root: Optional[Path] = None,
) -> int:
    """Entrypoint suitable for direct invocation or unit testing.

    Returns a process exit code; never raises for typed CLI input
    errors. ``date``, ``session_factory``, ``telegram_notifier`` and
    ``daily_notes_root`` are testing overrides.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_INVALID_INPUT

    if date is not None:
        args.date = date

    try:
        request = _build_request(args)
    except _CLIInputError as exc:
        print(f"status: {DailyOpsDigestStatus.INVALID_REQUEST.value}")
        print(f"failure_reason: {exc.failure_reason.value}")
        print(f"note: {exc.safe_message}")
        return EXIT_INVALID_INPUT

    factory = (
        session_factory if session_factory is not None else _default_session_factory()
    )

    try:
        return _run_until_complete(
            _run_async(
                request,
                factory,
                telegram_notifier=telegram_notifier,
                daily_notes_root=daily_notes_root,
            )
        )
    except KeyboardInterrupt:
        return EXIT_REPOSITORY


if __name__ == "__main__":  # pragma: no cover — exercised via tests + ops
    raise SystemExit(main())
