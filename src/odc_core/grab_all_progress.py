"""Resume a grab-all job where the last run stopped, cycle by cycle.

A grab-all login can see more accounts than one run can walk: Electric
Ireland's portal ends the session about 65 minutes after login, and one of
its logins has 45 companies, one of them with 198 accounts at about a minute
each. Without this, every run starts again at the first company, spends most
of its session re-checking work already done, and never reaches the end.

A **cycle** is one complete pass over everything a login can see, from the
first company to the last account of the last company. It is identified by
supplier + client + login (``ODC_jobs.username``), not by job id, because a
new ``ODC_jobs`` row is created every day for the same login.

- ``open_cycle()`` finds the open cycle for that key, or starts a new one,
  and loads what has already been recorded in it.
- ``Cycle.is_done()`` says whether a company or account can be skipped.
  ``DONE``, ``NO_ACCOUNTS`` and ``NOT_AUTHORISED`` are skipped; ``FAILED`` is
  always tried again.
- ``record()`` writes one result as soon as it is known, so a crash or a lost
  session loses at most the account that was in flight.
- ``close_cycle()`` is called once a run reaches the end of the list. The
  next run then opens a new cycle and starts again from the top.

Company-level rows use an empty ``account_reference``. Names are compared
with whitespace collapsed and case ignored, matching the Titan collation.

Tables (``tables["grab_all_cycle"]`` and ``tables["grab_all_progress"]``)
are created by the first consumer's migrations
(automation-odc-electric-ireland ``migrations/001``-``003``), the same way
``ODC_grab_all_data`` came from automation-odc-sse-airtricity: this library
owns no DDL.

Progress is bookkeeping. Callers should treat a failure here as "this run
cannot resume" and carry on downloading, not as a reason to stop: the worst
case is re-walking accounts, which the duplicate check already makes safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pyodbc

from . import db

logger = logging.getLogger(__name__)

STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"
STATUS_NO_ACCOUNTS = "NO_ACCOUNTS"
STATUS_NOT_AUTHORISED = "NOT_AUTHORISED"

VALID_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_NO_ACCOUNTS, STATUS_NOT_AUTHORISED})

#: Statuses a resumed run skips. FAILED is deliberately absent.
SKIP_STATUSES = frozenset({STATUS_DONE, STATUS_NO_ACCOUNTS, STATUS_NOT_AUTHORISED})

#: account_reference of a company-level row.
COMPANY_LEVEL = ""

# UPDLOCK + HOLDLOCK keeps two runs of the same login from both opening a
# cycle; the filtered unique index (migration 001) is the backstop.
_SELECT_OPEN_CYCLE = """
SELECT TOP 1 [id], [started_at_utc]
FROM {grab_all_cycle} WITH (UPDLOCK, HOLDLOCK)
WHERE [supplier] = ?
AND   [client_name] = ?
AND   [login_username] = ?
AND   [completed_at_utc] IS NULL
ORDER BY [id] DESC
"""

_INSERT_CYCLE = """
INSERT INTO {grab_all_cycle} ([supplier], [client_name], [login_username], [started_job_id])
OUTPUT INSERTED.[id], INSERTED.[started_at_utc]
VALUES (?, ?, ?, ?)
"""

_SELECT_PROGRESS = """
SELECT [company_name], [account_reference], [status]
FROM {grab_all_progress}
WHERE [cycle_id] = ?
"""

_UPDATE_PROGRESS = """
UPDATE {grab_all_progress}
SET [status] = ?, [bills_saved] = ?, [job_id] = ?, [process_id] = ?, [updated_at_utc] = SYSUTCDATETIME()
WHERE [cycle_id] = ? AND [company_name] = ? AND [account_reference] = ?
"""

_INSERT_PROGRESS = """
INSERT INTO {grab_all_progress}
    ([cycle_id], [company_name], [account_reference], [status], [bills_saved], [job_id], [process_id])
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

# Heartbeat (0.13.9, columns from migration 004/005): which run last worked
# the cycle, and when. Lets a new run tell a live claim from a dead one.
_TOUCH_CYCLE = """
UPDATE {grab_all_cycle}
SET [process_id] = ?, [heartbeat_at_utc] = SYSUTCDATETIME()
WHERE [id] = ?
"""

# Age is worked out by the database, so the runner's clock does not matter.
_SELECT_HEARTBEAT = """
SELECT TOP 1 [id], [process_id], DATEDIFF(SECOND, [heartbeat_at_utc], SYSUTCDATETIME())
FROM {grab_all_cycle}
WHERE [supplier] = ?
AND   [client_name] = ?
AND   [login_username] = ?
AND   [completed_at_utc] IS NULL
ORDER BY [id] DESC
"""

_CLOSE_CYCLE = """
UPDATE {grab_all_cycle}
SET [completed_at_utc] = SYSUTCDATETIME(), [completed_job_id] = ?
WHERE [id] = ? AND [completed_at_utc] IS NULL
"""


def _clean(value: str | None) -> str:
    """Strip and collapse whitespace: the form stored and compared."""
    return " ".join((value or "").split())


def _key(company_name: str | None, account_reference: str | None) -> tuple[str, str]:
    return _clean(company_name).lower(), _clean(account_reference).lower()


@dataclass
class Cycle:
    """One open cycle, with every result recorded in it so far."""

    id: int
    supplier: str
    client_name: str
    login_username: str
    started_at_utc: datetime | None
    resumed: bool
    statuses: dict[tuple[str, str], str] = field(default_factory=dict)
    #: False once a heartbeat write has failed (e.g. migration 004/005 not
    #: applied), so the warning is logged once and later beats are skipped.
    heartbeat_ok: bool = True

    def status_of(self, company_name: str, account_reference: str = COMPANY_LEVEL) -> str | None:
        """The recorded status, or None if nothing is recorded yet."""
        return self.statuses.get(_key(company_name, account_reference))

    def is_done(self, company_name: str, account_reference: str = COMPANY_LEVEL) -> bool:
        """True when this run can skip the company (or one of its accounts)."""
        return self.status_of(company_name, account_reference) in SKIP_STATUSES

    def counts(self) -> dict[str, int]:
        """How many rows of each status the cycle holds."""
        totals: dict[str, int] = {}
        for status in self.statuses.values():
            totals[status] = totals.get(status, 0) + 1
        return totals


@dataclass
class CycleHeartbeat:
    """Which run last worked the open cycle, and how long ago."""

    cycle_id: int
    process_id: str | None
    age_s: int | None  # None when no heartbeat has been written yet


def heartbeat(cycle: Cycle, process_id: str | int | None, tables: dict, dsn: str) -> bool:
    """Stamp the cycle with this run's process_id and the time. Never raises.

    ``record()`` and ``open_cycle()`` already do this; call it at other
    points where a run can go a long time without recording anything.
    """
    if process_id is None or not cycle.heartbeat_ok:
        return False
    sql = _TOUCH_CYCLE.format(grab_all_cycle=tables["grab_all_cycle"])

    def work(conn) -> None:
        conn.cursor().execute(sql, str(process_id), cycle.id)
        conn.commit()

    try:
        db.run(dsn, work, description=f"grab_all_progress.heartbeat({cycle.id})")
    except pyodbc.Error:
        cycle.heartbeat_ok = False
        logger.warning(
            "GRAB_ALL_PROGRESS - cycle %s heartbeat could not be written (migration 004/005 not applied?); "
            "a run that dies now cannot be taken over automatically", cycle.id, exc_info=True,
        )
        return False
    return True


def claim_heartbeat(
    supplier: str,
    client_name: str,
    login_username: str,
    tables: dict,
    dsn: str,
) -> CycleHeartbeat | None:
    """The open cycle's last heartbeat for supplier + client + login, or None if no cycle is open.

    A caller deciding whether a claim on a grab-all job is stale compares
    ``process_id`` with ``ODC_jobs.process_id`` and looks at ``age_s``. Read
    without a lock: it only informs that decision.
    """
    sql = _SELECT_HEARTBEAT.format(grab_all_cycle=tables["grab_all_cycle"])

    def work(conn) -> CycleHeartbeat | None:
        row = conn.cursor().execute(
            sql, _clean(supplier), _clean(client_name), _clean(login_username),
        ).fetchone()
        if row is None:
            return None
        process = None if row[1] is None or str(row[1]).strip() == "" else str(row[1]).strip()
        return CycleHeartbeat(cycle_id=int(row[0]), process_id=process, age_s=None if row[2] is None else int(row[2]))

    return db.run(dsn, work, description=f"grab_all_progress.claim_heartbeat({supplier}, {client_name})")


def open_cycle(
    supplier: str,
    client_name: str,
    login_username: str,
    job_id: str | int,
    tables: dict,
    dsn: str,
    *,
    process_id: str | int | None = None,
) -> Cycle:
    """Return the open cycle for supplier + client + login, starting one if there is none.

    With ``process_id`` (0.13.9) the cycle is also stamped with a heartbeat,
    so another run can see this one is alive from the moment it claims.
    """
    supplier, client_name, login_username = _clean(supplier), _clean(client_name), _clean(login_username)
    select_open = _SELECT_OPEN_CYCLE.format(grab_all_cycle=tables["grab_all_cycle"])
    insert_cycle = _INSERT_CYCLE.format(grab_all_cycle=tables["grab_all_cycle"])
    select_progress = _SELECT_PROGRESS.format(grab_all_progress=tables["grab_all_progress"])

    def work(conn) -> Cycle:
        cursor = conn.cursor()
        row = cursor.execute(select_open, supplier, client_name, login_username).fetchone()
        resumed = row is not None
        if row is None:
            row = cursor.execute(insert_cycle, supplier, client_name, login_username, int(job_id)).fetchone()
        conn.commit()
        cycle = Cycle(
            id=int(row[0]),
            supplier=supplier,
            client_name=client_name,
            login_username=login_username,
            started_at_utc=row[1],
            resumed=resumed,
        )
        for company, account, status in cursor.execute(select_progress, cycle.id).fetchall():
            cycle.statuses[_key(company, account)] = status
        return cycle

    # Replaying is safe: a transient SQLSTATE means the insert was not
    # committed, and the select finds the cycle if it was.
    cycle = db.run(dsn, work, description=f"grab_all_progress.open_cycle({supplier}, {client_name})")
    heartbeat(cycle, process_id, tables, dsn)
    logger.info(
        "GRAB_ALL_PROGRESS - %s cycle %s for %s / %s (started %s): %s",
        "resuming" if cycle.resumed else "starting",
        cycle.id, supplier, client_name, cycle.started_at_utc,
        ", ".join(f"{n} {s}" for s, n in sorted(cycle.counts().items())) or "nothing recorded yet",
    )
    return cycle


def record(
    cycle: Cycle,
    company_name: str,
    account_reference: str,
    status: str,
    *,
    bills_saved: int = 0,
    job_id: str | int,
    process_id: str | int | None,
    tables: dict,
    dsn: str,
) -> None:
    """Record one company (``account_reference=COMPANY_LEVEL``) or account result.

    Overwrites an earlier result for the same company/account in this cycle,
    so a FAILED account that later succeeds becomes DONE.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid grab-all progress status {status!r}")
    company_name, account_reference = _clean(company_name), _clean(account_reference)
    update_sql = _UPDATE_PROGRESS.format(grab_all_progress=tables["grab_all_progress"])
    insert_sql = _INSERT_PROGRESS.format(grab_all_progress=tables["grab_all_progress"])
    process = None if process_id is None else str(process_id)

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(
            update_sql, status, int(bills_saved), int(job_id), process,
            cycle.id, company_name, account_reference,
        )
        if cursor.rowcount == 0:
            cursor.execute(
                insert_sql, cycle.id, company_name, account_reference, status,
                int(bills_saved), int(job_id), process,
            )
        conn.commit()

    # Replaying is safe: update-then-insert lands on the same single row.
    db.run(dsn, work, description=f"grab_all_progress.record({company_name}, {account_reference or '(company)'})")
    cycle.statuses[_key(company_name, account_reference)] = status
    heartbeat(cycle, process_id, tables, dsn)
    logger.debug(
        "GRAB_ALL_PROGRESS - cycle %s: %s / %s -> %s",
        cycle.id, company_name, account_reference or "(company)", status,
    )


def close_cycle(cycle: Cycle, job_id: str | int, tables: dict, dsn: str) -> None:
    """Mark the cycle complete. The next ``open_cycle()`` starts a new one."""
    sql = _CLOSE_CYCLE.format(grab_all_cycle=tables["grab_all_cycle"])

    def work(conn) -> None:
        conn.cursor().execute(sql, int(job_id), cycle.id)
        conn.commit()

    # Replaying is safe: the WHERE only matches an open cycle.
    db.run(dsn, work, description=f"grab_all_progress.close_cycle({cycle.id})")
    logger.info(
        "GRAB_ALL_PROGRESS - cycle %s for %s / %s complete: %s",
        cycle.id, cycle.supplier, cycle.client_name,
        ", ".join(f"{n} {s}" for s, n in sorted(cycle.counts().items())) or "nothing recorded",
    )
