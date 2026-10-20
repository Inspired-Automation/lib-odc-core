"""Job read/claim, duplicate check and file allocation for "grab all" ODC jobs.

A grab-all job (``ODC_jobs.grab_all = 1``) does not loop ``ODC_job_details``.
The supplier bot walks every account the portal itself lists and downloads
every bill dated on or after ``ODC_jobs.grab_all_start_date`` that is not
already recorded. Account reference, meter number, utility and customer name
all come from the portal, not from the database.

That breaks three assumptions the account-search helpers make:

- ``jobstodo.get_job_details()`` inner-joins job_details to scrape_accounts,
  so a grab-all job returns no rows. ``get_job()`` reads the job row alone.
- ``duplicate_check.is_duplicate()`` joins through ``ODC_job_details``.
  ``is_duplicate()`` here checks the grab-all table (``tables["grab_all_data"]``)
  instead, joined to ``ODC_jobs`` for supplier/client.
- Portal-derived values can be blank. ``allocate()`` fills a blank utility
  in before delegating to ``file_allocation.allocate()``, so the folder
  convention is identical to account-search downloads. The filename leaves
  the meter out (0.13.4): a grab-all bill is named by the account and invoice
  number the portal shows, ``{supplier}_{account}_{utility}_{invoice}_{yyyymmdd}``.
- ``file_save_as.save()`` inserts ODC_scrape_data's column set, which
  includes ``job_details_id``. ``ODC_grab_all_data`` has no such column (a
  grab-all file has no job_details row), so ``save()`` here inserts the
  grab-all table's own column set.
- ``jobstodo.get_job_details()`` also surfaces ``human_in_loop`` (the job's
  own ``ODC_jobs.human_in_loop``, falling back to
  ``ODC_suppliers.human_in_loop``), which a grab-all job otherwise has no
  row to read from. ``get_job()`` reads it the same way so every grab-all
  consumer can decide whether to request human-assisted login.

``notify_requester()`` (0.13.8) emails the person who started the run from
inse-toolkit once it has completed. The toolkit records each run it fires
in ``TOOLKIT_PORTAL_SCRAPE_ASSIST_RUN`` with the Control Room job id, which
is this run's ``process_id``. A scheduled run has no such row, so nobody is
emailed.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import db, file_allocation, graph_client
from .jobstodo import _UPDATE_PROCESS_ID
from .login_failure import DEFAULT_SENDER

logger = logging.getLogger(__name__)

#: Substituted for a blank utility so a filename never ends up with an empty
#: segment (``..._ACC1__INV1_...``).
UNKNOWN = "UNKNOWN"

#: inse-toolkit's own tables. They live on Titan_INSE for DEV and LIVE runs
#: alike (the toolkit stamps ``odc_env`` on the run row instead).
DEFAULT_TOOLKIT_RUNS_TABLE = "Titan_INSE.dbo.TOOLKIT_PORTAL_SCRAPE_ASSIST_RUN"
DEFAULT_TOOLKIT_USERS_TABLE = "Titan_INSE.dbo.TOOLKIT_USER"

_SELECT_REQUESTER = """
SELECT TOP 1 u.[email], u.[display_name]
FROM {runs} AS r
JOIN {users} AS u ON u.[toolkit_user_id] = r.[toolkit_user_id]
WHERE r.[control_room_job_id] = ?
ORDER BY r.[created_utc] DESC
"""

_COUNT_SAVED = """
SELECT COUNT(*), COUNT(DISTINCT [account_reference])
FROM {grab_all_data}
WHERE [job_id] = ?
AND   [process_id] = ?
AND   ISNULL([status], '') <> 'VOID'
"""

_SELECT_JOB = """
SELECT
    j.[id],
    j.[grab_all],
    j.[grab_all_start_date],
    j.[client_name],
    j.[supplier],
    j.[client_id],
    j.[supplier_id],
    j.[url],
    j.[username],
    j.[password],
    j.[download_locations],
    j.[client_location],
    j.[pdf_auto],
    j.[pdf_auto_endpoint],
    j.[pdf_auto_key],
    j.[pdf_auto_instance],
    COALESCE(j.[human_in_loop], s.[human_in_loop]) AS [human_in_loop]
FROM {jobs} AS j
LEFT JOIN {suppliers} AS s ON j.[supplier_id] = s.[id]
WHERE j.[id] = ?
"""

_CHECK_DUPLICATE = """
SELECT COUNT(g.[unique_file_ref])
FROM {grab_all_data} AS g
JOIN {jobs} AS j ON j.[id] = g.[job_id]
WHERE g.[account_reference] = ?
AND   j.[supplier]          = ?
AND   j.[client_name]       = ?
AND   g.[unique_file_ref]   = ?
"""

# Column set of ODC_grab_all_data (Titan_INSE_DEV, 2026-09-29): no
# job_details_id / account_type / zip_address, unlike ODC_scrape_data.
# OUTPUT returns the new id so the VOID update can target exactly this row.
_INSERT_SQL = """
INSERT INTO {grab_all_data} (
    [job_id],
    [client_customer_name],
    [account_reference],
    [bill_date],
    [unique_file_ref],
    [file_name],
    [file_type],
    [download_location],
    [bill_date_corrected],
    [process_id],
    [original_file_name],
    [doc_type],
    [client_file_path]
)
OUTPUT INSERTED.[id]
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_VOID_SQL = """
UPDATE {grab_all_data}
SET [status] = 'VOID'
WHERE [id] = ?
"""

_MIN_FILE_SIZE = 1024  # bytes; files below this are marked VOID, as in file_save_as


def get_job(job_id: str, tables: dict, dsn: str) -> dict | None:
    """Return the ODC_jobs row for job_id as a dict, or None if it does not exist.

    Keys match the job-level keys on ``jobstodo.get_job_details()`` rows
    (plus ``id``, ``grab_all`` and ``grab_all_start_date``), so a caller can
    read the job header the same way in either mode - including
    ``human_in_loop``: ``ODC_jobs.human_in_loop`` (copied from
    ``ODC_services.human_in_loop`` at job creation), falling back to
    ``{suppliers}.human_in_loop`` ``LEFT JOIN``ed by ``supplier_id`` when the
    job's is NULL, the same way ``jobstodo._SELECT_JOB_DETAILS`` does, so a grab-all job (which has no ``ODC_job_details`` row to read
    that flag from) can still decide whether to request human-assisted
    login. Does not claim the job: call ``claim_job()`` for that.
    """
    sql = _SELECT_JOB.format(jobs=tables["jobs"], suppliers=tables["suppliers"])

    def work(conn) -> dict | None:
        cursor = conn.cursor()
        cursor.execute(sql, job_id)
        row = cursor.fetchone()
        if row is None:
            logger.debug("GRAB_ALL - no job row for job %s", job_id)
            return None
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))

    return db.run(dsn, work, description=f"grab_all.get_job({job_id})")


def is_grab_all_job(job: dict | None) -> bool:
    """Return True when the job row (from ``get_job()``) has grab_all set."""
    return bool(job and job.get("grab_all"))


def claim_job(job_id: str, process_id: str, tables: dict, dsn: str) -> None:
    """Stamp ODC_jobs.process_id for job_id.

    The same statement ``jobstodo.get_job_details()`` runs before reading
    job_details. A grab-all job never calls that, so it claims here instead.
    """
    sql = _UPDATE_PROCESS_ID.format(jobs=tables["jobs"])

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, process_id, job_id)
        conn.commit()
        logger.debug("GRAB_ALL - stamped process_id %s on job %s", process_id, job_id)

    # Replaying is safe: the stamp is the same value every time.
    db.run(dsn, work, description=f"grab_all.claim_job({job_id})")


_SELECT_CLAIM = "SELECT [process_id] FROM {jobs} WHERE [id] = ?"

# Only clears the caller's own claim, so a run that stops never releases a
# job another run has since taken over.
_RELEASE_CLAIM = "UPDATE {jobs} SET [process_id] = NULL WHERE [id] = ? AND [process_id] = ?"


def claimed_by(job_id: str, tables: dict, dsn: str) -> str | None:
    """Return the process_id holding job_id, or None if it is unclaimed (or has no row)."""
    sql = _SELECT_CLAIM.format(jobs=tables["jobs"])

    def work(conn) -> str | None:
        row = conn.cursor().execute(sql, job_id).fetchone()
        if row is None or row[0] is None or str(row[0]).strip() == "":
            return None
        return str(row[0]).strip()

    return db.run(dsn, work, description=f"grab_all.claimed_by({job_id})")


def release_claim(job_id: str, process_id: str, tables: dict, dsn: str) -> bool:
    """Clear ODC_jobs.process_id if `process_id` still holds the job. True if it was cleared.

    For a grab-all run that stopped before its cycle finished: the job is
    then unclaimed again (inse-toolkit lists only unclaimed jobs), so a
    person can re-run it and the re-run resumes the cycle.
    """
    sql = _RELEASE_CLAIM.format(jobs=tables["jobs"])

    def work(conn) -> bool:
        cursor = conn.cursor()
        cursor.execute(sql, job_id, str(process_id))
        released = cursor.rowcount > 0
        conn.commit()
        return released

    # Replaying is safe: the second attempt matches no row and changes nothing.
    released = db.run(dsn, work, description=f"grab_all.release_claim({job_id})")
    if released:
        logger.info("GRAB_ALL - released claim %s on job %s", process_id, job_id)
    else:
        logger.info("GRAB_ALL - job %s is not held by %s; claim left as it is", job_id, process_id)
    return released


def is_duplicate(
    account_reference: str,
    supplier: str,
    client_name: str,
    unique_file_ref: str,
    tables: dict,
    dsn: str,
) -> bool:
    """Return True if this bill is already recorded in the grab-all table.

    Same argument order as ``duplicate_check.is_duplicate()``. Keyed on
    supplier and client (via ODC_jobs), not on job_id, so recreating a
    grab-all job does not re-download everything. Checks the grab-all table
    only, never ODC_scrape_data.
    """
    sql = _CHECK_DUPLICATE.format(
        grab_all_data=tables["grab_all_data"],
        jobs=tables["jobs"],
    )

    def work(conn) -> bool:
        row = conn.cursor().execute(
            sql, account_reference, supplier, client_name, unique_file_ref,
        ).fetchone()
        return bool(row and row[0])

    return db.run(dsn, work, description="grab_all.is_duplicate")


def allocate(
    *,
    client_name: str,
    customer_name: str,
    account_reference: str,
    supplier: str,
    client_location: str,
    utility: str,
    doc_type: str,
    bill_date_corrected: str,
    file_extension: str,
    invoice_number: str,
    config: dict,
    meter_number: str = "",
    sug_internal_id: str | None = None,
) -> dict:
    """Return ``file_allocation.allocate()``'s result for a portal-derived bill.

    The filename is ``{supplier}_{account}_{utility}_{invoice}_{yyyymmdd}``:
    no meter, so every grab-all bot names its files by the portal's account
    and invoice number alone. ``meter_number`` is accepted for compatibility
    with existing callers and not used. A blank utility (the portal did not
    show one) becomes ``UNKNOWN`` with a warning; the folder convention is
    exactly the account-search one.
    """
    if not (utility or "").strip():
        logger.warning(
            "GRAB_ALL - blank utility for account %s, invoice %s - using %s",
            account_reference, invoice_number, UNKNOWN,
        )
        utility = UNKNOWN

    return file_allocation.allocate(
        client_name=client_name,
        customer_name=customer_name,
        account_reference=account_reference,
        supplier=supplier,
        client_location=client_location,
        utility=utility,
        meter_number="",
        doc_type=doc_type,
        bill_date_corrected=bill_date_corrected,
        file_extension=file_extension,
        invoice_number=invoice_number,
        config=config,
        sug_internal_id=sug_internal_id,
        include_meter=False,
    )


def save(
    *,
    staging_path: str,
    target_path: str,
    job_id: str,
    account_reference: str,
    unique_file_ref: str,
    bill_date: str,
    bill_date_corrected: str,
    original_filename: str,
    complete_filename: str,
    file_extension: str,
    client_filepath: str,
    customer_name: str,
    doc_type: str,
    download_location: str,
    process_id: str,
    tables: dict,
    dsn: str,
) -> bool:
    """Move a downloaded grab-all file into place and record it in ODC_grab_all_data.

    Same behaviour as ``file_save_as.save()`` (move, insert, VOID if under
    1 KB), with the grab-all table's column set and no job_details_id.
    Keyword-only, so the long argument list cannot be passed out of order.
    Returns True on success.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.debug("GRAB_ALL - moving %s -> %s", staging_path, target_path)
    shutil.move(staging_path, target_path)

    file_size = target.stat().st_size

    insert_sql = _INSERT_SQL.format(grab_all_data=tables["grab_all_data"])
    void_sql = _VOID_SQL.format(grab_all_data=tables["grab_all_data"])

    # Values are bound as query parameters, so pyodbc handles quoting. Do not
    # pre-escape apostrophes (see file_save_as.save()).
    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(
            insert_sql,
            job_id,
            customer_name,
            account_reference,
            bill_date,
            unique_file_ref,
            complete_filename,
            file_extension,
            download_location,
            bill_date_corrected,
            process_id,
            original_filename,
            doc_type,
            client_filepath,
        )
        new_id = cursor.fetchone()[0]
        conn.commit()
        logger.debug("GRAB_ALL - record %s inserted for %s", new_id, complete_filename)

        if file_size < _MIN_FILE_SIZE:
            cursor.execute(void_sql, new_id)
            conn.commit()
            logger.warning("GRAB_ALL - file marked VOID (< 1 KB): %s", target_path)

    # The move stays outside the retry: only the insert is replayed, and a
    # transient SQLSTATE means it was never committed, so no duplicate row.
    db.run(dsn, work, description=f"grab_all.save({complete_filename})")

    return True


def _notification_settings(config: dict) -> dict:
    return config.get("grab_all_notification") or {}


def find_requester(process_id: str, config: dict, dsn: str) -> tuple[str, str] | None:
    """Return ``(email, display_name)`` of the toolkit user who started this
    Control Room run, or None for a run the toolkit did not start."""
    settings = _notification_settings(config)
    sql = _SELECT_REQUESTER.format(
        runs=settings.get("toolkit_runs_table") or DEFAULT_TOOLKIT_RUNS_TABLE,
        users=settings.get("toolkit_users_table") or DEFAULT_TOOLKIT_USERS_TABLE,
    )

    def work(conn) -> tuple[str, str] | None:
        row = conn.cursor().execute(sql, str(process_id)).fetchone()
        if row is None or not (row[0] or "").strip():
            return None
        return row[0].strip(), (row[1] or "").strip()

    return db.run(dsn, work, description=f"grab_all.find_requester({process_id})")


def count_saved(job_id: str, process_id: str, tables: dict, dsn: str) -> tuple[int, int]:
    """Return ``(bills, accounts)`` this run saved to the grab-all table, VOID excluded."""
    sql = _COUNT_SAVED.format(grab_all_data=tables["grab_all_data"])

    def work(conn) -> tuple[int, int]:
        row = conn.cursor().execute(sql, job_id, str(process_id)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    return db.run(dsn, work, description=f"grab_all.count_saved({job_id})")


def build_completion_email(
    *, job: dict, job_id: str, bills: int, accounts: int, recipient_name: str = "",
) -> tuple[str, str]:
    """Return ``(subject, body)`` for the run-complete email. Plain text."""
    supplier = (job.get("supplier") or "Supplier").strip()
    first_name = recipient_name.split()[0] if recipient_name.strip() else ""
    subject = f"{supplier} Grab-all process completed"
    body = (
        f"Hello{' ' + first_name if first_name else ''},\n\n"
        "This is an automated message from the ODC bots. The grab-all run for "
        f"{supplier} (ODC job {job_id}) has completed.\n\n"
        f"New bills saved: {bills} across {accounts} account(s)\n\n"
        "Many Thanks\n"
    )
    return subject, body


def notify_requester(
    config: dict, *, job: dict, job_id: str, process_id: str, tables: dict, dsn: str,
) -> bool:
    """Email the toolkit user who started this run that it has completed.

    Call it only once the run has finished end to end with no errors. Sends
    nothing when ``grab_all_notification.enabled`` is false, or when the
    toolkit did not start this run (a scheduled run). Never raises: a failed
    lookup or send is logged at ERROR and returns False, because the run
    itself has already succeeded. Returns True if an email was sent.
    """
    settings = _notification_settings(config)
    if str(settings.get("enabled", True)).strip().lower() in ("false", "0", "no", "off"):
        logger.info("GRAB_ALL - completion email disabled (grab_all_notification.enabled)")
        return False
    try:
        requester = find_requester(process_id, config, dsn)
        if requester is None:
            logger.info(
                "GRAB_ALL - run %s was not started from the toolkit; no completion email", process_id,
            )
            return False
        email, name = requester
        bills, accounts = count_saved(job_id, process_id, tables, dsn)
        subject, body = build_completion_email(
            job=job, job_id=job_id, bills=bills, accounts=accounts, recipient_name=name,
        )
        graph_client.send_mail(
            config, email, subject, body,
            sender_address=settings.get("sender_address") or DEFAULT_SENDER,
        )
    except Exception:
        logger.exception("GRAB_ALL - could not send the completion email for job %s", job_id)
        return False
    logger.info(
        "GRAB_ALL - completion email sent to %s for job %s (%d bill(s), %d account(s))",
        email, job_id, bills, accounts,
    )
    return True
