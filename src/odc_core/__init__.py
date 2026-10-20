"""
odc_core - shared infrastructure for ODC (Online Data Collection) supplier bots.

Every supplier project (automation-odc-wave, automation-odc-british-gas, ...)
depends on this package instead of re-implementing job claiming, file
allocation/save, job-status updates, Microsoft Graph mail/OTP reading, or
login-failure handling (login_failure, 0.13.7), or grab-all resume
(grab_all_progress, 0.13.8).

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
    grab_all,
    grab_all_progress,
    graph_client,
    human_in_loop,
    jobstodo,
    login_failure,
    pdf_auto_copy,
    sftp_upload,
    sugar_client,
    updatejobdetails,
    validate_username,
)

__version__ = "0.13.8"

__all__ = [
    "browser_helpers",
    "db",
    "duplicate_check",
    "file_allocation",
    "file_save_as",
    "grab_all",
    "grab_all_progress",
    "graph_client",
    "human_in_loop",
    "jobstodo",
    "login_failure",
    "pdf_auto_copy",
    "sftp_upload",
    "sugar_client",
    "updatejobdetails",
    "validate_username",
]
