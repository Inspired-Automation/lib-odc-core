"""Check whether an invoice has already been recorded in ODC_scrape_data.

Fuller than a plain scrape_data lookup: "inspired plc" client accounts are
also cross-checked against ODC_scrape_accounts/web_scrape_data (a client_name
that spans both a newer scrape_data pipeline and an older web_scrape_data
table), since those two tables can each independently already hold the record.
"""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)

_INSPIRED_PLC = "inspired plc"

_CHECK_STANDARD = """
SELECT COUNT(s.[unique_file_ref])
FROM {jobs} AS j
JOIN {job_details} AS d ON j.[id] = d.[job_id]
JOIN {scrape_data} AS s ON d.[id] = s.[job_details_id]
WHERE d.[account_reference] = ?
AND   j.[supplier]          = ?
AND   j.[client_name]       = ?
AND   s.[unique_file_ref]   = ?
"""

_CHECK_INSPIRED = """
SELECT COUNT(u.unique_file_ref)
FROM (
    SELECT s.unique_file_ref,
           d.account_reference AS customer_reference,
           a.supplier
    FROM   {scrape_data}     AS s
    JOIN   {job_details}     AS d ON s.job_details_id = d.id
                                  AND d.account_reference = s.account_reference
    JOIN   {scrape_accounts} AS a ON d.scrape_accounts_id = a.id
    WHERE  s.zip_address IS NULL
    AND    a.client_name = 'Inspired PLC'

    UNION ALL

    SELECT unique_file_ref, customer_reference, supplier
    FROM   {web_scrape_data}
) u
WHERE u.customer_reference = ?
AND   u.supplier           = ?
AND   u.unique_file_ref    = ?
"""


def is_duplicate(
    account_reference: str,
    supplier: str,
    client_name: str,
    unique_file_ref: str,
    tables: dict,
    dsn: str,
) -> bool:
    """Return True if this account_reference/supplier/unique_file_ref already exists."""
    if (client_name or "").lower() == _INSPIRED_PLC:
        sql = _CHECK_INSPIRED.format(
            scrape_data=tables["scrape_data"],
            job_details=tables["job_details"],
            scrape_accounts=tables["scrape_accounts"],
            web_scrape_data=tables["web_scrape_data"],
        )
        params = (account_reference, supplier, unique_file_ref)
    else:
        sql = _CHECK_STANDARD.format(
            jobs=tables["jobs"],
            job_details=tables["job_details"],
            scrape_data=tables["scrape_data"],
        )
        params = (account_reference, supplier, client_name, unique_file_ref)

    def work(conn) -> bool:
        row = conn.cursor().execute(sql, *params).fetchone()
        return bool(row and row[0])

    return db.run(dsn, work, description="duplicate_check.is_duplicate")
