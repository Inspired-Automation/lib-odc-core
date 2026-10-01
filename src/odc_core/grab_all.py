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
- Portal-derived values can be blank. ``allocate()`` fills those in before
  delegating to ``file_allocation.allocate()``, so the folder and filename
  convention is identical to account-search downloads.
- ``file_save_as.save()`` inserts ODC_scrape_data's column set, which
  includes ``job_details_id``. ``ODC_grab_all_data`` has no such column (a
  grab-all file has no job_details row), so ``save()`` here inserts the
  grab-all table's own column set.
- ``jobstodo.get_job_details()`` also surfaces ``ODC_suppliers.human_in_loop``
  via its own join, which a grab-all job otherwise has no row to read from.
  ``get_job()`` LEFT JOINs ``{suppliers}`` itself so every grab-all consumer
  can decide whether to request human-assisted login the same way.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import db, file_allocation
from .jobstodo import _UPDATE_PROCESS_ID

logger = logging.getLogger(__name__)

#: Substituted for a blank meter number or utility so a filename never ends
#: up with an empty segment (``..._ACC1__Elec_...``).
UNKNOWN = "UNKNOWN"

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
    s.[human_in_loop]
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
    ``human_in_loop``, ``LEFT JOIN``ed from ``{suppliers}`` by
    ``supplier_id`` the same way ``jobstodo._SELECT_JOB_DETAILS`` already
    does, so a grab-all job (which has no ``ODC_job_details`` row to read
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
    meter_number: str,
    doc_type: str,
    bill_date_corrected: str,
    file_extension: str,
    invoice_number: str,
    config: dict,
    sug_internal_id: str | None = None,
) -> dict:
    """Return ``file_allocation.allocate()``'s result for a portal-derived bill.

    A blank meter_number or utility (the portal did not show one) becomes
    ``UNKNOWN`` with a warning, then everything is passed straight through,
    so the folder and filename convention is exactly the account-search one.
    """
    if not (meter_number or "").strip():
        logger.warning(
            "GRAB_ALL - blank meter_number for account %s, invoice %s - using %s",
            account_reference, invoice_number, UNKNOWN,
        )
        meter_number = UNKNOWN
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
        meter_number=meter_number,
        doc_type=doc_type,
        bill_date_corrected=bill_date_corrected,
        file_extension=file_extension,
        invoice_number=invoice_number,
        config=config,
        sug_internal_id=sug_internal_id,
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
