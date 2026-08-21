"""Look up a SugarCRM account name by its internal id.

Used by `file_allocation` to resolve the real company name for Inspired PLC
customers instead of trusting the free-text `customer_name` on ODC_job_details.
Sugar is a separate MySQL-backed CRM (`DSN=Sugar Corp`, not Titan/Jupiter), so
this takes its own `dsn` rather than the `tables` dict the Titan-facing modules
use. `accounts` is SugarCRM's own fixed schema table, not an ODC-owned table,
so its name is not parameterised.
"""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)

_SELECT_ACCOUNT_NAME = """
SELECT a.NAME
FROM accounts AS a
WHERE a.deleted = 0
AND a.id = ?
"""


def get_company_name(sug_internal_id: str, dsn: str) -> str | None:
    """Return the SugarCRM accounts.NAME for sug_internal_id, or None if not found."""

    def work(conn) -> str | None:
        cursor = conn.cursor()
        cursor.execute(_SELECT_ACCOUNT_NAME, sug_internal_id)
        row = cursor.fetchone()
        if row is None:
            logger.debug(
                "SUGAR_CLIENT - no active account found for sug_internal_id %s",
                sug_internal_id,
            )
            return None
        return row[0]

    return db.run(
        dsn, work, description=f"sugar_client.get_company_name({sug_internal_id})",
    )
