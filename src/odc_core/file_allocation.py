"""Determine target folder/filename convention for downloaded ODC invoices."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_INSPIRED_PLC = "inspired plc"


def _normalise_utility(utility: str) -> str:
    u_lower = (utility or "").lower()
    if "elec" in u_lower:
        return "Elec"
    if "gas" in u_lower:
        return "Gas"
    return utility or ""


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
) -> dict:
    """
    Determine the full file path and folder structure for a downloaded invoice.

    Returns a dict with keys:
        folder_location   - directory where the file will be saved
        complete_filename - filename without extension
        client_filepath   - full path including filename and extension
    """
    doc_type_str = "LETTER" if doc_type == "O" else "INVOICE"

    normalised_utility = _normalise_utility(utility)

    bill_date_yyyymmdd = (bill_date_corrected or "").replace("-", "")

    if (client_name or "").lower() == _INSPIRED_PLC:
        folder = (
            Path(client_location)
            / customer_name
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

    _create_folder_structure(folder, client_name, client_location, customer_name, config)

    logger.debug(
        "FILE_ALLOCATION - folder: %s  file: %s.%s",
        folder, filename, file_extension,
    )

    return {
        "folder_location": str(folder),
        "complete_filename": filename,
        "client_filepath": str(client_filepath),
    }


def _create_folder_structure(
    base_folder: Path,
    client_name: str,
    client_location: str,
    customer_name: str,
    config: dict,
) -> None:
    """Create the required directory tree."""
    fa = config.get("file_allocation", {})

    if (client_name or "").lower() == _INSPIRED_PLC:
        customer_root = Path(client_location) / customer_name
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
            sugar_id_file.write_text("", encoding="ansi")
    else:
        base_folder.mkdir(parents=True, exist_ok=True)
