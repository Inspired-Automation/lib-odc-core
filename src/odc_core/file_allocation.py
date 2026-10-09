"""Determine target folder/filename convention for downloaded ODC invoices.

Where a file goes is decided by lib-file-allocation (file_allocation_core),
shared with every process that files into POST RECEIVED: which clients take
the Inspired process (per-customer folders) versus the flat monthly folder,
and, for Inspired, which customer folder a Sugar account owns
(Titan_INSE.dbo.XDRIVE_CUSTOMER_MASTER, renaming the folder first when
Sugar's name has changed). This module keeps ODC's own parts: the Sugar name
lookup, the doc type and utility conventions, and the filenames.
"""

from __future__ import annotations

import logging

from file_allocation_core.routing import (
    TreeSpec,
    destination_folder,
    is_inspired_client,
    normalise_utility,
)

from . import sugar_client

logger = logging.getLogger(__name__)

#: audit changed_by for customer master writes made through this module.
SOURCE = "odc"


def _resolve_inspired_company_name(
    customer_name: str,
    sug_internal_id: str | None,
    config: dict,
) -> str:
    """Return the SugarCRM company name for an Inspired PLC customer.

    Falls back to the raw `customer_name` when there is no sug_internal_id to
    look up (older data) or no `config["sugar"]["dsn"]` (caller has not yet
    pinned a release with a config.yaml `sugar:` block).
    """
    if not sug_internal_id:
        logger.debug(
            "FILE_ALLOCATION - no sug_internal_id for customer %r, "
            "falling back to customer_name", customer_name,
        )
        return customer_name

    sugar_dsn = config.get("sugar", {}).get("dsn")
    if not sugar_dsn:
        logger.debug(
            "FILE_ALLOCATION - no config['sugar']['dsn'], "
            "falling back to customer_name for %r", customer_name,
        )
        return customer_name

    company_name = sugar_client.get_company_name(sug_internal_id, sugar_dsn)
    if not company_name:
        logger.warning(
            "FILE_ALLOCATION - sug_internal_id %s not found in Sugar, "
            "falling back to customer_name %r", sug_internal_id, customer_name,
        )
        return customer_name

    return company_name


def _customer_master_dsn(config: dict) -> str | None:
    """The DSN for the customer master lookup, only for a production run.

    The table is always Titan_INSE (live): a dev run must not insert dev
    folder names into it or rename folders on its say-so, so dev files by
    name as before.
    """
    if config.get("env") != "prod":
        return None
    return config.get("database", {}).get("dsn")


def _tree(config: dict) -> TreeSpec:
    fa = config.get("file_allocation", {})
    return TreeSpec(
        doc_types=tuple(fa.get("doc_types", [])),
        statuses=tuple(fa.get("statuses", [])),
        utilities=tuple(fa.get("utilities", [])),
    )


def allocate(
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
    """
    Determine the full file path and folder structure for a downloaded invoice.

    For Inspired PLC and Ignite, files go into a per-customer folder. With a
    `sug_internal_id` on a production run, the folder is the one the customer
    master holds for that Sugar account (renamed first if Sugar's name has
    changed, created under Sugar's name if the account is new). Otherwise it
    is the SugarCRM name (or the free-text `customer_name`, always the case
    for Ignite today), made safe for Windows.

    Returns a dict with keys:
        folder_location   - directory where the file will be saved
        complete_filename - filename without extension
        client_filepath   - full path including filename and extension
    """
    doc_type_str = "LETTER" if doc_type == "O" else "INVOICE"
    normalised_utility = normalise_utility(utility)
    bill_date_yyyymmdd = (bill_date_corrected or "").replace("-", "")

    if is_inspired_client(client_name):
        company_name = _resolve_inspired_company_name(customer_name, sug_internal_id, config)
        folder = destination_folder(
            root=client_location,
            client_name=client_name,
            doc_type=doc_type_str,
            utility=normalised_utility,
            tree=_tree(config),
            source=SOURCE,
            sugar_id=sug_internal_id,
            sugar_name=company_name,
            dsn=_customer_master_dsn(config),
        )
        filename = (
            f"{supplier}_{account_reference}_{normalised_utility}"
            f"_{invoice_number}_{bill_date_yyyymmdd}"
        )
    else:
        folder = destination_folder(
            root=client_location,
            client_name=client_name,
            doc_type=doc_type_str,
            utility=normalised_utility,
            tree=_tree(config),
            source=SOURCE,
        )
        filename = (
            f"{supplier}_{account_reference}_{meter_number}"
            f"_{normalised_utility}_{invoice_number}_{bill_date_yyyymmdd}"
        )

    client_filepath = folder / f"{filename}.{file_extension}"

    logger.debug(
        "FILE_ALLOCATION - folder: %s  file: %s.%s",
        folder, filename, file_extension,
    )

    return {
        "folder_location": str(folder),
        "complete_filename": filename,
        "client_filepath": str(client_filepath),
    }
