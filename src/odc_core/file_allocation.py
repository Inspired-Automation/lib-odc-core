"""Determine target folder/filename convention for downloaded ODC invoices."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from . import sugar_client

logger = logging.getLogger(__name__)

_INSPIRED_PLC = "inspired plc"


def _normalise_utility(utility: str) -> str:
    u_lower = (utility or "").lower()
    if "elec" in u_lower:
        return "Elec"
    if "gas" in u_lower:
        return "Gas"
    return utility or ""


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

    For Inspired PLC, the company folder is named from the SugarCRM account
    resolved via `sug_internal_id`, not the free-text `customer_name`.

    Returns a dict with keys:
        folder_location   - directory where the file will be saved
        complete_filename - filename without extension
        client_filepath   - full path including filename and extension
    """
    doc_type_str = "LETTER" if doc_type == "O" else "INVOICE"

    normalised_utility = _normalise_utility(utility)

    bill_date_yyyymmdd = (bill_date_corrected or "").replace("-", "")

    is_inspired = (client_name or "").lower() == _INSPIRED_PLC

    if is_inspired:
        company_name = _resolve_inspired_company_name(customer_name, sug_internal_id, config)
        folder = (
            Path(client_location)
            / company_name
            / doc_type_str
            / "NEW"
            / normalised_utility
        )
        filename = (
            f"{supplier}_{account_reference}_{normalised_utility}"
            f"_{invoice_number}_{bill_date_yyyymmdd}"
        )
    else:
        month_str = datetime.now().strftime("%Y-%m")
        folder = Path(client_location) / month_str
        filename = (
            f"{supplier}_{account_reference}_{meter_number}"
            f"_{normalised_utility}_{invoice_number}_{bill_date_yyyymmdd}"
        )

    client_filepath = folder / f"{filename}.{file_extension}"

    if is_inspired:
        _create_inspired_folder_structure(
            client_location, company_name, sug_internal_id, config,
        )
    else:
        folder.mkdir(parents=True, exist_ok=True)

    logger.debug(
        "FILE_ALLOCATION - folder: %s  file: %s.%s",
        folder, filename, file_extension,
    )

    return {
        "folder_location": str(folder),
        "complete_filename": filename,
        "client_filepath": str(client_filepath),
    }


def _create_inspired_folder_structure(
    client_location: str,
    company_name: str,
    sug_internal_id: str | None,
    config: dict,
) -> None:
    """Create the required directory tree for an Inspired PLC company."""
    fa = config.get("file_allocation", {})

    customer_root = Path(client_location) / company_name
    for dt in fa.get("doc_types", []):
        for status in fa.get("statuses", []):
            if status == "NEW":
                for util in fa.get("utilities", []):
                    (customer_root / dt / status / util).mkdir(
                        parents=True, exist_ok=True
                    )
            else:
                (customer_root / dt / status).mkdir(
                    parents=True, exist_ok=True
                )
    sugar_id_file = customer_root / "sugar_id.txt"
    if not sugar_id_file.exists():
        sugar_id_file.write_text(sug_internal_id or "", encoding="ansi")
