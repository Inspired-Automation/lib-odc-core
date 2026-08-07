"""Update ODC_job_details status via spODC_job_details_UpdateStatus."""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({
    "IN PROGRESS",
    "FOUND",
    "NOT FOUND",
    "DOWNLOADED",
    "PARTIALLY DOWNLOADED",
    "NOT REQUIRED",
    "FAILED",
    "ERROR",
    "REQUIRES RETRY",
    "MISSING PARENT",
})

_EXEC_SP = "EXEC {db_name}.dbo.spODC_job_details_UpdateStatus ?, ?, ?"


def update(
    job_detail_id: str,
    status: str,
    update_parent_account: bool,
    tables: dict,
    dsn: str,
) -> None:
    """
    Call spODC_job_details_UpdateStatus to update a single job_details row.

    update_parent_account: True to also update the parent account record.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"UPDATEJOBDETAILS - invalid status '{status}'")

    sql = _EXEC_SP.format(db_name=tables["db_name"])
    parent_flag = "true" if update_parent_account else "false"

    logger.debug(
        "UPDATEJOBDETAILS - job_detail_id=%s status=%s parent=%s",
        job_detail_id, status, parent_flag,
    )

    def work(conn) -> None:
        cursor = conn.cursor()
        cursor.execute(sql, job_detail_id, status, parent_flag)
        conn.commit()
        logger.debug("UPDATEJOBDETAILS - updated successfully")

    db.run(dsn, work, description=f"updatejobdetails.update({job_detail_id})")
