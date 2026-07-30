# lib-odc-core

Shared ODC (Online Data Collection) infrastructure used by every supplier
project (`automation-odc-wave`, `automation-odc-british-gas`, ...). Package
name: `odc_core`. Mirrors the `lib-core` / `automation_core` pattern: built
to a wheel and pinned in each supplier project's `requirements.txt` from a
GitHub release, not published to PyPI.

## Installation

Pin a released version in the consuming project's `requirements.txt`:

```
odc-core @ https://github.com/Inspired-Automation/lib-odc-core/releases/download/vX.Y.Z/odc_core-X.Y.Z-py3-none-any.whl
```

or by git tag:

```
odc-core @ git+https://github.com/Inspired-Automation/lib-odc-core.git@vX.Y.Z
```

## Usage

```python
from odc_core import jobstodo, file_save_as, updatejobdetails

job_details = jobstodo.get_job_details(process_id, job_id, tables, dsn)

for detail in job_details:
    # ... login, search, download the invoice for `detail` ...
    file_save_as.save(
        staging_path, target_path, job_id, detail["id"],
        detail["account_reference"], unique_file_ref, bill_date,
        bill_date_corrected, original_filename, complete_filename,
        file_extension, folder_location, client_filepath,
        detail["customer_name"], doc_type, download_location,
        process_id, tables, dsn,
    )
    updatejobdetails.update(detail["id"], "DOWNLOADED", False, tables, dsn)
```

## What's in here

- `jobstodo.py` - claim a job (`ODC_jobs.process_id`), fetch pending/searchable
  `ODC_job_details` rows, revert/clear a claim, look up multi-credential rows.
- `file_allocation.py` - work out the target folder/filename for a downloaded
  invoice from `ODC_jobs`/`ODC_job_details` fields.
- `file_save_as.py` - move a downloaded file into place and insert the
  `ODC_scrape_data` row (marks it `VOID` if under 1 KB).
- `duplicate_check.py` - `is_duplicate()`: has this invoice already been
  recorded? Standard suppliers check `ODC_scrape_data` directly; `client_name
  == "inspired plc"` accounts are also cross-checked against
  `ODC_scrape_accounts`/`web_scrape_data` (an older parallel pipeline for
  that client). Call this before downloading a document, not after.
- `validate_username.py` - `validate()`: basic sanity check that a portal
  username isn't blank/non-alphanumeric before attempting login.
- `updatejobdetails.py` - call `spODC_job_details_UpdateStatus`.
- `pdf_auto_copy.py` - copy a filed invoice into the PDF Auto drop folder
  when a job's `pdf_auto` flag is set.
- `graph_client.py` - Microsoft Graph helpers: `send_mail()` and
  `read_otp_code()` (polls a mailbox for a passwordless-login OTP email).
  Both read `client_id`/`client_secret`/`tenant_id`/`sender_address` from a
  caller-supplied `config["graph"]` dict - never hardcode Graph credentials
  here or in a calling project.
- `browser_helpers.py` - human-like pacing/click/type helpers and error
  screenshot capture for Playwright/patchright-based suppliers. Plain
  functions taking `page`/`config` explicitly (no base class - each supplier
  is its own project/process now, not a dynamically-dispatched module).

## Status vocabulary

Supplier projects' own scraping code returns one of these strings, used in
`ODC_job_details.status` and passed to `updatejobdetails.update()`:

```
DOWNLOADED, PARTIALLY DOWNLOADED, NOT REQUIRED, NOT FOUND, ERROR, FAILED,
IN PROGRESS, FOUND
```

## Configuration

Every function here takes `config`, `tables`, and/or `dsn` explicitly rather
than reading a config file itself - the calling project owns config loading.
Callers must supply:

```yaml
database:
  dsn: Jupiter

tables:
  dev:  {jobs: ..., job_details: ..., scrape_data: ..., scrape_accounts: ..., web_scrape_data: ..., multi_credential: ..., credential: ..., db_name: ...}
  live: {jobs: ..., job_details: ..., scrape_data: ..., scrape_accounts: ..., web_scrape_data: ..., multi_credential: ..., credential: ..., db_name: ...}

graph:
  client_id:
  client_secret:
  tenant_id:
  sender_address:

delays:
  keystroke_min_ms: 50
  keystroke_max_ms: 150
  action_min_s: 1.0
  action_max_s: 3.0

file_allocation:
  doc_types: [LETTER, INVOICE, LEGAL, DEBT, PAYREM]
  statuses:  [ARCHIVE, HOLDING, NEW]
  utilities: [ELEC, WATER, GAS, OTHER]
```

## Development

```
pip install -e ".[dev]"
pytest
```

## Versioning

Semantic versioning (`MAJOR.MINOR.PATCH`). Tag a release with:

```
git tag vX.Y.Z
git push --tags
```

See `RELEASING.md` for the full manual release runbook (wheel build, GitHub
Release, pinning in supplier projects).

## Adding a new supplier project

A supplier project only needs its own `login()`/`search()` (or equivalent)
functions and config, calling into `odc_core.jobstodo`, `odc_core.file_allocation`,
`odc_core.file_save_as`, `odc_core.updatejobdetails`, `odc_core.graph_client`,
and `odc_core.browser_helpers` as needed. Do not copy these modules into a
supplier project - bump this package's version instead if a shared function
needs to change, and re-pin the new release in the supplier's `requirements.txt`.
