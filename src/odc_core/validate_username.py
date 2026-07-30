"""Basic sanity check for a portal username before attempting login."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def validate(username: str) -> bool:
    """Return True if username contains at least one alphanumeric character."""
    cleaned = re.sub(r"\W", "", username or "")
    is_valid = len(cleaned) > 0
    if is_valid:
        logger.debug("VALIDATE_USERNAME - valid: %s", username)
    else:
        logger.error("VALIDATE_USERNAME - invalid or empty: '%s'", username)
    return is_valid
