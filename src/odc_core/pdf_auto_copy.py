"""Copy a filed invoice into the PDF Auto drop folder when pdf_auto = 1."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BASE = r"I:\BPI\Automation Team\Automated Processes\ODC"


def copy(source_filepath: str, client_name: str, config: dict) -> Path:
    """
    Copy *source_filepath* into:
      {base}/{env}/PDFAuto/{client_name}/{same filename}

    *base* defaults to the I: drive path; override via config['pdf_auto']['base_path'].
    *env* comes from config['env'] (dev | live).
    """
    env = config.get("env", "dev")
    base = (config.get("pdf_auto") or {}).get("base_path") or _DEFAULT_BASE
    dest_dir = Path(base) / env / "PDFAuto" / client_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(source_filepath).name
    shutil.copy2(source_filepath, dest)
    logger.debug("PDF_AUTO_COPY - %s -> %s", source_filepath, dest)
    return dest
