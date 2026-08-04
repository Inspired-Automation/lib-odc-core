"""Job claiming and job_details retrieval for ODC_jobs / ODC_job_details."""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)

MULTI_CREDENTIAL_SENTINEL = "multi_credential"

_SELECT_PROCESS_ID = "SELECT process_id FROM {jobs} WHERE id = ?"

_UPDATE_PROCESS_ID = "UPDATE {jobs} SET process_id = ? WHERE id = ?"

_SELECT_JOB_CREDENTIALS = """
SELECT [username], [password]
FROM {jobs}
WHERE id = ?
"""

_SELECT_CLIENT_LOCATION = """
SELECT [client_location]
FROM {jobs}
WHERE id = ?
"""

_SELECT_JOB_DETAILS = """
SELECT
    j.[client_name],
    j.[supplier],
    j.[client_id],
    j.[supplier_id],
    j.[download_locations],
    j.[url],
    j.[username],
    j.[password],
    j.[client_location],
    j.[pdf_auto],
    j.[pdf_auto_endpoint],
    j.[pdf_auto_key],
    j.[pdf_auto_instance],
    d.[id],
    d.[job_id],
    d.[scrape_accounts_id],
    d.[scrape_start_date],
    d.[customer_name],
    d.[meter_number],
    d.[meter_id],
    d.[account_reference],
    d.[parent_account_reference],
    d.[utility],
    d.[alternative_ref_1],
    d.[status]
FROM {jobs} AS j
JOIN {job_details} AS d ON j.id = d.job_id
WHERE j.id = ?
  {status_filter}
"""

_STATUS_FILTER_PENDING = "AND d.[status] = 'pending'"

_STATUS_FILTER_MULTI = (
    "AND UPPER(LTRIM(RTRIM(d.[status]))) NOT IN ("
    "'DOWNLOADED', 'FOUND', 'FAILED', 'NOT REQUIRED', "
    "'PARTIALLY DOWNLOADED', 'NOT FOUND'"
    ")"
)

_SELECT_MULTI_CREDENTIALS = """
SELECT mc.username, mc.password, c.url
FROM {multi_credential} AS mc
JOIN {credential} AS c ON c.client_id = mc.client_id AND c.supplier_id = mc.supplier_id
WHERE mc.client_id = ?
  AND mc.supplier_id = ?
  AND mc.active = 1
"""

_REVERT_TO_PENDING = "UPDATE {job_details} SET [status] = 'pending' WHERE id = ?"

_CLEAR_PROCESS_ID = "UPDATE {jobs} SET process_id = NULL WHERE id = ?"


def is_job_claimed(job_id: str, tables: dict, dsn: str) -> bool:
    """Return True if ODC_jobs.process_id is already set for this job_id."""
    sql = _SELECT_PROCESS_ID.format(jobs=tables["jobs"])

    def work(conn) -> bool:
        cursor = conn.cursor()
        cursor.execute(sql, job_id)
        row = cursor.fetchone()
        if row is None:
            return False
        return bool(row[0])

    return db.run(dsn, work, description=f"jobstodo.is_job_claimed({job_id})")


def get_client_location(job_id: str, tables: dict, dsn: str) -> str | None:
    """Return client_location for job_id, or None if the job row is missing."""
    sql = _SELECT_CLIENT_LOCATION.format(jobs=tables["jobs"])

    def work(conn) -> str | None:
        cursor = conn.cursor()
        cursor.execute(sql, job_id)
        row = cursor.fetchone()
        if row is None:
            return None
        return row[0] or ""

    return db.run(dsn, work, description=f"jobstodo.get_client_location({job_id})")


def is_multi_credential_job(username: str | None, password: str | None) -> bool:
    """Return True when job-level credentials are the multi-credential sentinel."""
    return (
        (username or "").strip().lower() == MULTI_CREDENTIAL_SENTINEL
        and (password or "").strip().lower() == MULTI_CREDENTIAL_SENTINEL
    )


def get_job_details(
    process_id: str,
    job_id: str,
    tables: dict,
    dsn: str,
) -> list[dict]:
    """
    Stamp the job with process_id, then return job_details rows for job_id.

    Single-credential jobs: only ``pending`` rows.
    Multi-credential jobs (username/password = ``multi_credential``):
    all rows except terminal statuses (DOWNLOADED, FOUND, FAILED, NOT REQUIRED,
    PARTIALLY DOWNLOADED, NOT FOUND).
    """
    update_sql = _UPDATE_PROCESS_ID.format(jobs=tables["jobs"])
    cred_sql = _SELECT_JOB_CREDENTIALS.format(jobs=tables["jobs"])

    def work(conn) -> list[dict]:
        cursor = conn.cursor()

        logger.debug("JOBSTODO - stamping process_id %s on job %s", process_id, job_id)
        cursor.execute(update_sql, process_id, job_id)
        conn.commit()

        cursor.execute(cred_sql, job_id)
        cred_row = cursor.fetchone()
        if cred_row is None:
            logger.debug("JOBSTODO - no job row for job %s", job_id)
            return []

        job_username, job_password = cred_row[0], cred_row[1]
        multi = is_multi_credential_job(job_username, job_password)
        status_filter = _STATUS_FILTER_MULTI if multi else _STATUS_FILTER_PENDING

        select_sql = _SELECT_JOB_DETAILS.format(
            jobs=tables["jobs"],
            job_details=tables["job_details"],
            status_filter=status_filter,
        )

        mode_label = "searchable" if multi else "pending"
        logger.debug(
            "JOBSTODO - querying %s job details for job %s (multi=%s)",
            mode_label, job_id, multi,
        )
        cursor.execute(select_sql, job_id)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        logger.debug("JOBSTODO - found %d %s row(s) for job %s", len(rows), mode_label, job_id)
        return rows

    # Replaying this is safe: the process_id stamp is the same value every time,
    # and a transient SQLSTATE means it was never committed.
    return db.run(dsn, work, description=f"jobstodo.get_job_details({job_id})")


def get_multi_credentials(
    client_id: str,
    supplier_id: str,
    tables: dict,
    dsn: str,
) -> list[dict]:
    """Return active portal credentials for a client/supplier pair."""
    sql = _SELECT_MULTI_CREDENTIALS.format(
        multi_credential=tables["multi_credential"],
        credential=tables["credential"],
    )
    def work(conn) -> list[dict]:
        cursor = conn.cursor()
        cursor.execute(sql, client_id, supplier_id)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        logger.debug(
            "JOBSTODO - found %d multi-credential row(s) for client_id=%s supplier_id=%s",
            len(rows), client_id, supplier_id,
        )
        return rows

    return db.run(
        dsn, work,
        description=f"jobstodo.get_multi_credentials({client_id}/{supplier_id})",
    )


def revert_to_pending(job_detail_id: str, tables: dict, dsn: str) -> None:
    """Reset a job_details row to pending (used between credential attempts)."""
    sql = _REVERT_TO_PENDING.format(job_details=tables["job_details"])
    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, job_detail_id)
        conn.commit()
        logger.debug("JOBSTODO - reverted job_details %s to pending", job_detail_id)

    db.run(dsn, work, description=f"jobstodo.revert_to_pending({job_detail_id})")


def clear_job_claim(job_id: str, tables: dict, dsn: str) -> None:
    """Clear the process_id on ODC_jobs so the job can be retried."""
    sql = _CLEAR_PROCESS_ID.format(jobs=tables["jobs"])
    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, job_id)
        conn.commit()
        logger.debug("JOBSTODO - cleared process_id claim for job %s", job_id)

    db.run(dsn, work, description=f"jobstodo.clear_job_claim({job_id})")
