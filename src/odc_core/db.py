"""Shared MSSQL access for ODC bots, with retry on transient failures only.

Every ODC supplier project runs for hours against Jupiter over the corporate
network, so a momentary DBNETLIB drop or a login timeout is normal. Before this
module each such blip cost whatever account was in flight: on 2026-08-02 a
Pozitive Energy run lost accounts to `('08001', ... SQL Server does not exist or
access denied ... ConnectionOpen (Connect()))` raised inside
`duplicate_check.is_duplicate()`, and to `('HYT00', ... Login timeout expired)`
raised inside `updatejobdetails.update()`.

Only the SQLSTATEs in TRANSIENT_SQLSTATES are retried. A syntax error, a
constraint violation or a permission problem is a real fault and must surface on
the first attempt rather than being tried four times: retrying those wastes the
run and buries the cause.

Retrying is safe for the units of work in this package because every transient
SQLSTATE here means the connection or the transaction failed, so nothing was
committed. `40001` is the deadlock victim, which SQL Server has already rolled
back. Do not wrap a partially-committed multi-statement unit in `run()` without
checking that replaying it is idempotent.

Connections are trusted (DSN only, no credentials in the connection string) per
team-instructions.mdc Section 2.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

import pyodbc

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: SQLSTATEs that mean "the connection or transaction failed, try again".
#: 08001 unable to connect, 08S01 communication link failure, HYT00 timeout
#: expired, HYT01 connection timeout expired, 40001 serialisation failure
#: (deadlock victim).
TRANSIENT_SQLSTATES = frozenset({"08001", "08S01", "HYT00", "HYT01", "40001"})

#: Total attempts, including the first. 4 attempts with the backoff below spans
#: roughly 13 seconds, which covers a network blip without stalling a job that
#: is genuinely pointed at a dead server.
DEFAULT_ATTEMPTS = 4

#: First backoff in seconds; each subsequent wait is 3x the previous one.
DEFAULT_BASE_DELAY_S = 1.0

_BACKOFF_FACTOR = 3.0


def sqlstate(exc: BaseException) -> str:
    """Return the five-character SQLSTATE pyodbc puts in args[0], or ""."""
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str):
        return args[0].strip().upper()
    return ""


def is_transient(exc: BaseException) -> bool:
    """Return True if exc is a pyodbc error worth retrying."""
    return isinstance(exc, pyodbc.Error) and sqlstate(exc) in TRANSIENT_SQLSTATES


def _connect_once(dsn: str) -> pyodbc.Connection:
    # Trusted connection: the DSN carries the server and the integrated-security
    # setting, so no credentials appear here or in the logs.
    return pyodbc.connect(f"DSN={dsn}")


def _attempt(
    action: Callable[[], T],
    *,
    description: str,
    attempts: int,
    base_delay_s: float,
) -> T:
    """Call `action`, retrying it on a transient SQLSTATE with backoff."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    delay = base_delay_s
    last_exc: BaseException = RuntimeError(f"DB - {description} never ran")

    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if not is_transient(exc):
                raise
            last_exc = exc
            if attempt == attempts:
                break
            logger.warning(
                "DB - %s failed with transient SQLSTATE %s on attempt %d/%d; "
                "retrying in %.1fs",
                description, sqlstate(exc) or "(none)", attempt, attempts, delay,
            )
            time.sleep(delay)
            delay *= _BACKOFF_FACTOR

    logger.error(
        "DB - %s failed after %d attempt(s) with transient SQLSTATE %s",
        description, attempts, sqlstate(last_exc) or "(none)",
    )
    raise last_exc


def run(
    dsn: str,
    work: Callable[[pyodbc.Connection], T],
    *,
    description: str = "database call",
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
) -> T:
    """Open a connection, run `work(conn)`, and return its result.

    The connection is closed on the way out. A transient failure anywhere in
    `work` retries the whole unit on a fresh connection, so `work` must be safe
    to replay: see the module docstring.
    """
    def once() -> T:
        conn = _connect_once(dsn)
        try:
            return work(conn)
        finally:
            conn.close()

    return _attempt(
        once, description=description, attempts=attempts, base_delay_s=base_delay_s,
    )


@contextmanager
def connect(
    dsn: str,
    *,
    description: str = "connection",
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
) -> Iterator[pyodbc.Connection]:
    """Yield a connection, retrying only the connect itself.

    Use this when the caller cannot express its work as a replayable callable.
    Prefer `run()`: a transient drop part way through a query is not covered
    here, because by then the body of the `with` block is already running.
    """
    conn = _attempt(
        lambda: _connect_once(dsn),
        description=description,
        attempts=attempts,
        base_delay_s=base_delay_s,
    )
    try:
        yield conn
    finally:
        conn.close()
