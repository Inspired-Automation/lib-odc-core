"""
odc_core - shared infrastructure for ODC (Online Data Collection) supplier bots.

Every supplier project (automation-odc-wave, automation-odc-british-gas, ...)
depends on this package instead of re-implementing job claiming, file
allocation/save, job-status updates, or Microsoft Graph mail/OTP reading.

Status vocabulary returned by a supplier's own search()/scrape function and
used across ODC_job_details.status / updatejobdetails.update():
    DOWNLOADED, PARTIALLY DOWNLOADED, NOT REQUIRED, NOT FOUND, ERROR, FAILED,
    IN PROGRESS, FOUND, REQUIRES RETRY, MISSING PARENT
"""

from . import (
    browser_helpers,
    db,
    duplicate_check,
    file_allocation,
    file_save_as,
    graph_client,
    human_in_loop,
    jobstodo,
    pdf_auto_copy,
    sugar_client,
    updatejobdetails,
    validate_username,
)

__version__ = "0.8.4"

__all__ = [
    "browser_helpers",
    "db",
    "duplicate_check",
    "file_allocation",
    "file_save_as",
    "graph_client",
    "human_in_loop",
    "jobstodo",
    "pdf_auto_copy",
    "sugar_client",
    "updatejobdetails",
    "validate_username",
]
