"""Move a downloaded ODC invoice into place and record it in ODC_scrape_data."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import db

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO {scrape_data} (
    [job_id],
    [job_details_id],
    [account_reference],
    [unique_file_ref],
    [bill_date],
    [file_name],
    [file_type],
    [download_location],
    [bill_date_corrected],
    [original_file_name],
    [client_customer_name],
    [doc_type],
    [client_file_path],
    [process_id]
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_VOID_SQL = """
UPDATE {scrape_data}
SET [status] = 'VOID'
WHERE [job_details_id] = ? AND [file_name] = ?
"""

_MIN_FILE_SIZE = 1024  # bytes; files below this are marked VOID


def save(
    staging_path: str,
    target_path: str,
    job_id: str,
    job_details_id: str,
    account_reference: str,
    unique_file_ref: str,
    bill_date: str,
    bill_date_corrected: str,
    original_filename: str,
    complete_filename: str,
    file_extension: str,
    folder_location: str,
    client_filepath: str,
    customer_name: str,
    doc_type: str,
    download_location: str,
    process_id: str,
    tables: dict,
    dsn: str,
) -> bool:
    """
    Move the downloaded file from staging_path to target_path, insert a record
    into ODC_scrape_data, and mark the record VOID if the file is < 1 KB.
    Returns True on success.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.debug("FILE_SAVE_AS - moving %s -> %s", staging_path, target_path)
    shutil.move(staging_path, target_path)

    file_size = target.stat().st_size

    insert_sql = _INSERT_SQL.format(scrape_data=tables["scrape_data"])
    void_sql = _VOID_SQL.format(scrape_data=tables["scrape_data"])

    # Values are bound as query parameters, so pyodbc handles quoting. Do not
    # pre-escape apostrophes here: that would double-escape and store the
    # literal doubled quote (e.g. "Sainsbury''s") in the database.
    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(
            insert_sql,
            job_id,
            job_details_id,
            account_reference,
            unique_file_ref,
            bill_date,
            complete_filename,
            file_extension,
            download_location,
            bill_date_corrected,
            original_filename,
            customer_name,
            doc_type,
            client_filepath,
            process_id,
        )
        conn.commit()
        logger.debug("FILE_SAVE_AS - record inserted for %s", complete_filename)

        if file_size < _MIN_FILE_SIZE:
            cursor.execute(void_sql, job_details_id, complete_filename)
            conn.commit()
            logger.warning(
                "FILE_SAVE_AS - file marked VOID (< 1 KB): %s", target_path
            )

    # The shutil.move above stays outside the retry: only the insert is replayed,
    # and a transient SQLSTATE means it was never committed, so no duplicate row.
    db.run(dsn, work, description=f"file_save_as.save({complete_filename})")

    return True
