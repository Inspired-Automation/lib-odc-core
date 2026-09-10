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
  `ODC_job_details` rows (joined to `ODC_scrape_accounts` for `sug_internal_id`
  and left-joined to `ODC_suppliers` for `human_in_loop`), revert/clear a
  claim, look up multi-credential rows. Also owns the job-level
  human-assisted-login signal on `ODC_jobs`: `set_human_wait()` /
  `set_human_wait_complete()` / `clear_human_wait()` - see `human_in_loop.py`
  below for the higher-level entry point most callers should use instead.
- `human_in_loop.py` - `wait_for_human_login()`: the full human-assisted-login
  mechanism for any supplier flagged by `ODC_suppliers.human_in_loop` - an
  on-page banner, an injected "I'm logged in" confirm button, a poll loop,
  and the `jobstodo` DB signal lifecycle around it, all in one call. The
  caller supplies `is_logged_in(page) -> bool`, the one genuinely
  portal-specific piece (each supplier's post-login marker differs), and
  may override the "started" banner text via `started_message` (default:
  generic wording) for a portal-specific instruction, e.g. "resolve the
  reCAPTCHA challenge". On success, `human_wait_status` is left at
  `COMPLETE` (a persistent terminal state, not auto-cleared). Generalised
  out of `automation-odc-energia`'s
  Phase 3. Also exports `get_current_hostname()` (`socket.gethostname()`) for
  building `wait_for_human_login()`'s `rdp_host` argument from this run
  node's own identity rather than static, driftable config. This mechanism
  connects over VNC, which needs only a host - 0.8.8 removed
  `get_current_windows_username()`/`get_rdp_password()` and the
  `rdp_username`/`rdp_password` arguments they built, added in 0.8.1-0.8.7
  when this was briefly built around RDP instead.
- `file_allocation.py` - work out the target folder/filename for a downloaded
  invoice from `ODC_jobs`/`ODC_job_details` fields. For Inspired PLC, resolves
  the company folder name from SugarCRM via `sugar_client` and `sug_internal_id`
  rather than trusting the free-text `customer_name`.
- `sugar_client.py` - `get_company_name()`: look up a SugarCRM account name by
  its internal id (`DSN=Sugar Corp`), used by `file_allocation` for Inspired
  PLC folder naming.
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
IN PROGRESS, FOUND, REQUIRES RETRY, MISSING PARENT
```

`REQUIRES RETRY` and `MISSING PARENT` were added for suppliers with
parent/child account grouping (a business account umbrella reference that
covers several `ODC_job_details` rows for individual meters). Note that
`jobstodo.get_job_details()` only re-fetches `pending` rows for a
single-credential job, so a row left in either of these two statuses is
not automatically retried by a later run unless something else resets it
back to `pending` first.

## Configuration

Every function here takes `config`, `tables`, and/or `dsn` explicitly rather
than reading a config file itself - the calling project owns config loading.
Callers must supply:

```yaml
database:
  dsn: Jupiter

tables:
  dev:  {jobs: ..., job_details: ..., scrape_data: ..., scrape_accounts: ..., suppliers: ..., web_scrape_data: ..., multi_credential: ..., credential: ..., db_name: ...}
  live: {jobs: ..., job_details: ..., scrape_data: ..., scrape_accounts: ..., suppliers: ..., web_scrape_data: ..., multi_credential: ..., credential: ..., db_name: ...}

graph:
  client_id:
  client_secret:
  tenant_id:
  sender_address:

sugar:
  dsn: Sugar Corp

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
