"""Copy a filed invoice into the PDF Auto drop folder when pdf_auto = 1."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# UNC form of I:\BPI\... - Control Room run nodes' service accounts do not
# necessarily have the I: drive letter mapped.
_DEFAULT_BASE = r"\\inspiredenergysolutions.local\DFS\Public\!IES\BPI\Automation Team\Automated Processes\ODC"

# Bot env value -> the folder spODC_job_details_UpdateStatus hard-codes into
# ODC_job_details_pdfauto.filepath ({base}\LIVE\PDFAuto on Titan_INSE,
# {base}\DEV\PDFAuto on Titan_INSE_DEV). Every supplier bot sets env "prod" in
# production; before 0.10.0 that was used as the folder name verbatim, so
# copies landed in {base}\prod\PDFAuto where the PDF Auto upload never looks.
_ENV_FOLDERS = {"prod": "LIVE", "live": "LIVE", "dev": "DEV"}


class PdfAutoCopyError(OSError):
    """The copy did not land in the PDF Auto folder intact."""


def copy(source_filepath: str, client_name: str, config: dict) -> Path:
    """
    Copy *source_filepath* into:
      {base}/{LIVE|DEV}/PDFAuto/{client_name}/{same filename}

    *base* defaults to the I: drive's UNC path; override via config['pdf_auto']['base_path'].
    The LIVE/DEV folder comes from config['env']: "prod" or "live" -> LIVE,
    "dev" (the default) -> DEV. Any other value raises ValueError.

    The copy is verified before returning: the destination must exist with the
    same size as the source, or PdfAutoCopyError is raised.
    """
    env = str(config.get("env") or "dev").lower()
    folder = _ENV_FOLDERS.get(env)
    if folder is None:
        raise ValueError(
            f"PDF_AUTO_COPY - unknown env {env!r}; expected one of {sorted(_ENV_FOLDERS)}"
        )
    base = (config.get("pdf_auto") or {}).get("base_path") or _DEFAULT_BASE
    dest_dir = Path(base) / folder / "PDFAuto" / client_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(source_filepath).name
    shutil.copy2(source_filepath, dest)
    size = _verify(Path(source_filepath), dest)
    logger.info("PDF_AUTO_COPY - verified %s (%d bytes)", dest, size)
    return dest


def _verify(source: Path, dest: Path) -> int:
    """Return the copied size, or raise PdfAutoCopyError if *dest* is missing or truncated."""
    if not dest.is_file():
        raise PdfAutoCopyError(f"PDF_AUTO_COPY - copy of {source} not found at {dest}")
    expected = source.stat().st_size
    actual = dest.stat().st_size
    if actual != expected:
        raise PdfAutoCopyError(
            f"PDF_AUTO_COPY - size mismatch copying {source} -> {dest}: "
            f"{actual} bytes, expected {expected}"
        )
    return actual
