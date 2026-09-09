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
    d.[status],
    a.[sug_internal_id],
    s.[human_in_loop]
FROM {jobs} AS j
JOIN {job_details} AS d ON j.id = d.job_id
JOIN {scrape_accounts} AS a ON d.[scrape_accounts_id] = a.[id]
LEFT JOIN {suppliers} AS s ON j.[supplier_id] = s.[id]
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

#: Job-level signal that a human-assisted login is in progress for this job.
#: Distinct from ODC_job_details.status (updatejobdetails.VALID_STATUSES),
#: which is per-account and only meaningful once search() starts - a
#: human-assisted login wait happens once per job, before any account is
#: individually processed, so it has no job_details_id to attach a per-account
#: status to. See lib-odc-core-spec.md 4.2.
#:
#: human_wait_status moves through three states:
#:   NULL -> HUMAN_WAIT_PENDING -> HUMAN_WAIT_COMPLETE -> NULL
#: set_human_wait() writes HUMAN_WAIT_PENDING (the cue for a poller's
#: toolkit to open the RDP session using rdp_host/rdp_username/
#: rdp_password). Detecting that the human has actually finished logging in
#: is the caller's own concern, not this library's - e.g. an in-page
#: confirm button or a post-login URL check polled from the caller's own
#: wait loop. Once the caller's wait loop confirms success, it calls
#: set_human_wait_complete(), which writes HUMAN_WAIT_COMPLETE and nulls
#: the RDP columns - the cue for the toolkit to disconnect. The caller must
#: still call clear_human_wait() on every exit path regardless - success,
#: failure, and timeout alike - to fully close out the signal; on a
#: failure/timeout exit, clear_human_wait() is called directly from
#: HUMAN_WAIT_PENDING and HUMAN_WAIT_COMPLETE is never written.
HUMAN_WAIT_PENDING = "PENDING_HUMAN"
HUMAN_WAIT_COMPLETE = "COMPLETE"

_SET_HUMAN_WAIT = (
    "UPDATE {jobs} SET human_wait_status = ?, "
    "human_wait_deadline = DATEADD(SECOND, ?, SYSUTCDATETIME()), "
    "rdp_host = ?, rdp_username = ?, rdp_password = ? "
    "WHERE id = ?"
)

_SET_HUMAN_WAIT_COMPLETE = (
    "UPDATE {jobs} SET human_wait_status = ?, "
    "rdp_host = NULL, rdp_username = NULL, rdp_password = NULL "
    "WHERE id = ?"
)

_CLEAR_HUMAN_WAIT = (
    "UPDATE {jobs} SET human_wait_status = NULL, "
    "human_wait_deadline = NULL, "
    "rdp_host = NULL, rdp_username = NULL, rdp_password = NULL "
    "WHERE id = ?"
)


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

    Each row is inner-joined to `scrape_accounts` on `scrape_accounts_id`, so it
    also carries `sug_internal_id` (the SugarCRM account id, used by
    `file_allocation` to resolve the Inspired PLC company folder name). A
    job_details row with no matching scrape_accounts row is excluded.

    Each row is also left-joined to `suppliers` on `ODC_jobs.supplier_id`, so
    it carries `human_in_loop` (nullable tinyint - truthy means this
    supplier's login requires `human_in_loop.wait_for_human_login()`, see
    that module). A left join, unlike the scrape_accounts one above: a job
    row with no matching supplier row still comes back, just with
    `human_in_loop` as None rather than being silently excluded.
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
            scrape_accounts=tables["scrape_accounts"],
            suppliers=tables["suppliers"],
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


def set_human_wait(
    job_id: str,
    timeout_s: int,
    rdp_host: str,
    rdp_username: str,
    rdp_password: str,
    tables: dict,
    dsn: str,
) -> None:
    """Mark ODC_jobs as waiting on a human-assisted login.

    Sets human_wait_status to HUMAN_WAIT_PENDING and human_wait_deadline to
    (now + timeout_s), computed on the database server so the deadline a
    poller reads cannot drift from clock differences between the run node
    and Titan. Callers should pass the same timeout_s their own wait loop
    uses (e.g. config["vnc"]["wait_for_human_timeout_s"]) so the two values
    can never diverge.

    rdp_host/rdp_username/rdp_password are the RDS machine and login the
    poller's toolkit needs to open the RDP session behind the login view -
    the human-assisted flow cannot connect without them. They are written in
    plaintext, the same pattern already used for portal credentials in
    ODC_credentials/ODC_job_details.

    Call set_human_wait_complete() once the caller's own wait loop confirms
    the human has finished logging in (that detection - e.g. an in-page
    confirm button or a post-login URL check - is the caller's concern, not
    this library's), then call clear_human_wait() on every exit path from
    the wait regardless - success, failure, and timeout alike. See each
    function's docstring.
    """
    sql = _SET_HUMAN_WAIT.format(jobs=tables["jobs"])

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(
            sql, HUMAN_WAIT_PENDING, timeout_s, rdp_host, rdp_username, rdp_password, job_id,
        )
        conn.commit()
        logger.debug(
            "JOBSTODO - set human_wait_status=PENDING for job %s (timeout %ss, rdp_host=%s)",
            job_id, timeout_s, rdp_host,
        )

    db.run(dsn, work, description=f"jobstodo.set_human_wait({job_id})")


def set_human_wait_complete(job_id: str, tables: dict, dsn: str) -> None:
    """Mark a human-assisted login as finished, ahead of the final clear.

    Sets human_wait_status to HUMAN_WAIT_COMPLETE and nulls
    rdp_host/rdp_username/rdp_password - the cue for a poller's toolkit to
    disconnect the RDP session, since those credentials are no longer valid
    for this job once it disconnects them. Distinct from clear_human_wait():
    this leaves human_wait_status itself non-NULL (COMPLETE, not cleared) so
    a poller can tell "just finished, go disconnect" apart from "nothing
    happening here."

    Call this only after the caller's own wait loop has confirmed the human
    actually finished logging in - never on a failure or timeout exit path,
    so a poller never reads COMPLETE for a login that didn't succeed; those
    paths should call clear_human_wait() directly instead. The caller must
    still call clear_human_wait() afterwards to fully close out the signal.
    """
    sql = _SET_HUMAN_WAIT_COMPLETE.format(jobs=tables["jobs"])

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, HUMAN_WAIT_COMPLETE, job_id)
        conn.commit()
        logger.debug("JOBSTODO - set human_wait_status=COMPLETE for job %s", job_id)

    db.run(dsn, work, description=f"jobstodo.set_human_wait_complete({job_id})")


def clear_human_wait(job_id: str, tables: dict, dsn: str) -> None:
    """Clear the human-wait signal and RDP connection details on ODC_jobs.

    Call this on every exit path out of a human-assisted wait - success,
    failure, and timeout alike - so a poller stops treating the job as
    waiting, any provisioned session is torn down promptly rather than a
    stale HUMAN_WAIT_PENDING/HUMAN_WAIT_COMPLETE lingering after the bot has
    already moved on, and the plaintext rdp_host/rdp_username/rdp_password
    fields do not sit in the database once the RDS machine is no longer in
    use for this job. Safe to call even if set_human_wait_complete() already
    nulled the RDP columns, or was never reached at all.
    """
    sql = _CLEAR_HUMAN_WAIT.format(jobs=tables["jobs"])

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, job_id)
        conn.commit()
        logger.debug("JOBSTODO - cleared human_wait_status for job %s", job_id)

    db.run(dsn, work, description=f"jobstodo.clear_human_wait({job_id})")
