# `lib-odc-core` - ODC Shared Infrastructure Library Specification

The canonical functional and technical specification for `lib-odc-core`. When the
public API, the config schema, or a database contract changes, this document is
updated in the same commit (see `RELEASING.md`). `README.md` and `CLAUDE.md`
summarise this document; where they disagree, this document is correct.

---

## 1. Repo and Package Identity

| Item | Value |
|------|-------|
| Repository | `https://github.com/Inspired-Automation/lib-odc-core` |
| Distribution name | `odc-core` |
| Import name | `odc_core` |
| Layout | src-layout (`src/odc_core/`) |
| Python | 3.14 or later |
| Runtime deps | `pyodbc>=5.3`, `msal>=1.31`, `requests>=2.32` |
| Distribution | Wheel attached to a GitHub Release. Not published to PyPI. |

Consuming supplier projects pin an exact release in their own `requirements.txt`:

```
odc-core @ https://github.com/Inspired-Automation/lib-odc-core/releases/download/vX.Y.Z/odc_core-X.Y.Z-py3-none-any.whl
```

This library declares dependency *floors* rather than exact pins so it does not
fight the supplier project's own resolution. Exact pinning is the consumer's job.

---

## 2. Configuration Architecture

This package reads no configuration file of its own. Every function receives
`config`, `tables`, and/or `dsn` explicitly; the calling supplier project owns
config loading. There is no `automation_core.setup()` equivalent here and no
module-level config state.

### 2.1 Expected config shape

```yaml
database:
  dsn: Jupiter

tables:
  dev:
    jobs: ODC_jobs
    job_details: ODC_job_details
    scrape_data: ODC_scrape_data
    scrape_accounts: ODC_scrape_accounts
    web_scrape_data: jupiter.aa_dev.web_scrape_data
    multi_credential: ODC_multi_credentials
    credential: ODC_credentials
    db_name: Titan_INSE_DEV
  live:
    # same keys, pointing at Titan_INSE and jupiter.aa.web_scrape_data

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

pdf_auto:
  base_path: I:\BPI\Automation Team\Automated Processes\ODC

env: dev
```

### 2.2 Which keys each module needs

| Module | Requires |
|--------|----------|
| `jobstodo`, `file_save_as`, `duplicate_check`, `updatejobdetails` | `tables`, `dsn` (passed directly, not the whole config) |
| `db` | `dsn` only (passed directly) |
| `file_allocation` | `config["file_allocation"]` |
| `graph_client` | `config["graph"]` |
| `browser_helpers` | `config["delays"]`, plus `_logs_dir`/`_process_id`/`_supplier_name` for screenshots |
| `pdf_auto_copy` | `config["env"]`, `config["pdf_auto"]["base_path"]` |
| `validate_username` | nothing |

Graph credentials are never hardcoded in this package. A caller that omits
`tenant_id`, `client_id`, or `client_secret` gets a `ValueError`.

---

## 3. Public API

### 3.1 Shape of the API

`odc_core/__init__.py` re-exports the nine **modules**, not their individual
functions:

```python
from odc_core import jobstodo, file_allocation, file_save_as
rows = jobstodo.get_job_details(process_id, job_id, tables, dsn)
```

This is a deliberate divergence from `automation_core`, which flattens its
seven public callables into the package namespace. Flattening roughly forty
functions across nine modules here would collide on the short verbs this API
uses (`save`, `copy`, `update`, `allocate`, `validate`) and read far worse than
the module-qualified form. Callers should not import private helpers (leading
underscore); everything else in a module is public.

### 3.2 `jobstodo` - job claiming and retrieval

```python
MULTI_CREDENTIAL_SENTINEL = "multi_credential"

is_job_claimed(job_id: str, tables: dict, dsn: str) -> bool
get_client_location(job_id: str, tables: dict, dsn: str) -> str | None
is_multi_credential_job(username: str | None, password: str | None) -> bool
get_job_details(process_id: str, job_id: str, tables: dict, dsn: str) -> list[dict]
get_multi_credentials(client_id: str, supplier_id: str, tables: dict, dsn: str) -> list[dict]
revert_to_pending(job_detail_id: str, tables: dict, dsn: str) -> None
clear_job_claim(job_id: str, tables: dict, dsn: str) -> None
```

`get_job_details()` stamps `ODC_jobs.process_id` **before** reading
`ODC_job_details`, so two concurrent runs of the same job cannot both claim it.
It then branches on the job's own credentials:

- Single-credential job: returns only rows with `status = 'pending'`.
- Multi-credential job (`username` and `password` both equal
  `multi_credential`, case-insensitive): returns every row whose trimmed,
  uppercased status is not one of `DOWNLOADED`, `FOUND`, `FAILED`,
  `NOT REQUIRED`, `PARTIALLY DOWNLOADED`, `NOT FOUND`.

Returns `[]` when the job row does not exist. Each returned dict carries the
joined `ODC_jobs` and `ODC_job_details` columns keyed by column name.

### 3.3 `duplicate_check` - pre-download guard

```python
is_duplicate(account_reference: str, supplier: str, client_name: str,
             unique_file_ref: str, tables: dict, dsn: str) -> bool
```

**Call this before downloading a document, not after.** Callers skip the
download entirely when it returns `True`.

- Standard clients: counts matching rows across `jobs -> job_details -> scrape_data`.
- `client_name == "inspired plc"` (case-insensitive): counts against a
  `UNION ALL` of `scrape_data`-derived rows (joined through `scrape_accounts`,
  `zip_address IS NULL`) and the older `web_scrape_data` pipeline, because
  either table can independently already hold the record.

### 3.4 `file_allocation` - target path convention

```python
allocate(client_name, customer_name, account_reference, supplier,
         client_location, utility, meter_number, doc_type,
         bill_date_corrected, file_extension, invoice_number,
         config: dict) -> dict
```

Returns `{"folder_location": str, "complete_filename": str, "client_filepath": str}`
where `complete_filename` excludes the extension. Creates the directory tree as
a side effect.

| | Inspired PLC | Every other client |
|---|---|---|
| Folder | `{client_location}/{customer_name}/{INVOICE\|LETTER}/NEW/{utility}` | `{client_location}/{YYYY-MM}` |
| Filename | `{supplier}_{account_reference}_{utility}_{invoice_number}_{YYYYMMDD}` | `{supplier}_{account_reference}_{meter_number}_{utility}_{invoice_number}_{YYYYMMDD}` |
| Extra | Builds the full doc_type/status/utility tree and seeds `sugar_id.txt` | Creates the month folder only |

`doc_type == "O"` maps to `LETTER`; anything else maps to `INVOICE`. Utility is
normalised to `Elec` or `Gas` by substring match, otherwise passed through.

### 3.5 `file_save_as` - move and record

```python
save(staging_path, target_path, job_id, job_details_id, account_reference,
     unique_file_ref, bill_date, bill_date_corrected, original_filename,
     complete_filename, file_extension, folder_location, client_filepath,
     customer_name, doc_type, download_location, process_id,
     tables: dict, dsn: str) -> bool
```

Creates the target's parent directory, moves the file from staging, inserts one
`ODC_scrape_data` row, and marks that row `status = 'VOID'` when the moved file
is under 1024 bytes (treated as a failed or empty download). Returns `True`.

All values are bound as query parameters. Do not pre-escape quotes before
calling: pyodbc handles quoting, and escaping first stores the doubled quote
literally. `folder_location` is currently accepted but unused; it is scheduled
for removal at the next MAJOR version.

### 3.6 `updatejobdetails` - status transitions

```python
VALID_STATUSES: frozenset[str]
update(job_detail_id: str, status: str, update_parent_account: bool,
       tables: dict, dsn: str) -> None
```

Validates `status` against `VALID_STATUSES` and raises `ValueError` before
opening a connection if it does not match, then calls
`{db_name}.dbo.spODC_job_details_UpdateStatus`. `update_parent_account` is sent
to the proc as the string `"true"` or `"false"`.

`REQUIRES RETRY` and `MISSING PARENT` (added in 0.6.0) are for suppliers with
parent/child account grouping: `REQUIRES RETRY` marks a row that failed in a
way that should not be treated as permanently `FAILED`/`ERROR`; `MISSING
PARENT` marks a row searched under a parent account reference where the
portal reports the actual document lives under a specific child account
instead. Neither is re-fetched automatically by `jobstodo.get_job_details()`
for a single-credential job (see 3.2) - only `pending` rows are re-queried,
so something else must reset the row to `pending` if it needs to run again.

### 3.7 `graph_client` - Microsoft Graph

```python
send_mail(config: dict, recipient: str, subject: str, body: str) -> None
read_otp_code(config: dict, mailbox: str, login_time: datetime, *,
              subject_contains: str, body_start: str, body_end: str,
              timeout_s: int = 3600, poll_interval_s: int = 10) -> str
```

`send_mail()` sends as `config["graph"]["sender_address"]` and raises
`RuntimeError` on any non-2xx Graph response.

`read_otp_code()` polls `mailbox` for messages received after `login_time` whose
subject contains `subject_contains`, extracts the first 4-8 digit group found
between `body_start` and `body_end`, and returns it. Poll errors are logged and
retried. Raises `TimeoutError` when `timeout_s` elapses with no code. A message
that matches the subject but contains no digit group is skipped rather than
accepted, so polling continues.

### 3.8 `validate_username`

```python
validate(username: str) -> bool
```

`True` when the username retains at least one word character after stripping
non-word characters. Guards against blank or punctuation-only portal usernames.

### 3.9 `pdf_auto_copy`

```python
copy(source_filepath: str, client_name: str, config: dict) -> Path
```

Copies the filed invoice to `{base}/{env}/PDFAuto/{client_name}/{filename}` and
returns the destination. `base` comes from `config["pdf_auto"]["base_path"]`,
defaulting to `I:\BPI\Automation Team\Automated Processes\ODC`; `env` comes from
`config["env"]`, defaulting to `dev`. Call only when the job's `pdf_auto` flag is set.

### 3.10 `browser_helpers`

```python
human_delay(config: dict) -> None
move_mouse_toward(page, locator) -> None
human_click(page, locator) -> None
human_type(page, selector: str, text: str, config: dict) -> None
take_error_screenshot(page, label: str, config: dict) -> None
dismiss_cookie_banner(page) -> bool
dismiss_cookie_banners(page) -> None
```

Supplier-agnostic Playwright/patchright helpers taking `page` and `config`
explicitly rather than living on a base class, since each supplier is now its
own project rather than a dynamically dispatched module. `locator` may be a
Locator or a CSS/text selector string. Pacing comes from `config["delays"]`.

`take_error_screenshot()` never raises: a screenshot failure must not interrupt
the run. It writes `{_process_id}_{_supplier_name}_{label}_{timestamp}.png` into
`config["_logs_dir"]`.

### 3.11 `db` - connection and transient-failure retry

```python
TRANSIENT_SQLSTATES: frozenset[str]   # 08001, 08S01, HYT00, HYT01, 40001
sqlstate(exc: BaseException) -> str
is_transient(exc: BaseException) -> bool
run(dsn, work, *, description, attempts=4, base_delay_s=1.0) -> T
connect(dsn, *, description, attempts=4, base_delay_s=1.0)  # context manager
```

The single entry point for MSSQL access in this library. `run()` opens a trusted
DSN connection, calls `work(conn)`, closes the connection, and retries the whole
unit on a fresh connection when the failure carries a transient SQLSTATE, with
1s/3s/9s backoff. Anything else, a syntax error or a constraint violation,
propagates on the first attempt: retrying those wastes the run and buries the
cause.

Every ODC bot runs for hours against Jupiter over the corporate network, so a
momentary DBNETLIB drop is normal and used to cost whatever account was in
flight. Replaying the units in this library is safe because a transient SQLSTATE
means the connection or transaction failed, so nothing was committed, and `40001`
is the deadlock victim which SQL Server has already rolled back. Do not wrap a
partially-committed multi-statement unit in `run()` without checking that
replaying it is idempotent.

`connect()` retries only the connect and hands the caller a live handle, closing
it on the way out. Prefer `run()`: a drop part way through a query is not covered
by `connect()`, because by then the body of the `with` block is already running.

---

## 4. Database Contracts

All access goes through the `db` module: `pyodbc` with trusted connections
(`DSN={dsn}`, no credentials in the connection string), parameterised queries, and
retry on transient SQLSTATEs only. `db.run()` closes the connection it opened;
callers that mutate data commit explicitly inside their `work(conn)` callable.

| Object | Used by | Purpose |
|--------|---------|---------|
| `ODC_jobs` | `jobstodo`, `duplicate_check` | One row per supplier job. `process_id` is the claim marker. |
| `ODC_job_details` | `jobstodo`, `duplicate_check`, `updatejobdetails` | One row per account to collect. Carries `status`. |
| `ODC_scrape_data` | `file_save_as`, `duplicate_check` | One row per downloaded document. |
| `ODC_scrape_accounts` | `duplicate_check` | Inspired PLC account pool. |
| `ODC_multi_credentials`, `ODC_credentials` | `jobstodo` | Shared credential pool for multi-credential jobs. |
| `jupiter.aa[_dev].web_scrape_data` | `duplicate_check` | Older parallel pipeline, consulted for Inspired PLC only. |
| `spODC_job_details_UpdateStatus` | `updatejobdetails` | The only supported way to change a job_details status. |

This library issues no DDL. Schema ownership sits with the Titan database.

### 4.1 Status vocabulary

Shared across every supplier. Written to `ODC_job_details.status` via
`updatejobdetails.update()`:

```
DOWNLOADED, PARTIALLY DOWNLOADED, NOT REQUIRED, NOT FOUND, ERROR, FAILED,
IN PROGRESS, FOUND, REQUIRES RETRY, MISSING PARENT
```

`ODC_scrape_data.status` additionally uses `VOID`, set by `file_save_as.save()`
for sub-1 KB downloads. `pending` (lowercase) is the pre-run state of a
`job_details` row and is not part of the vocabulary above.

---

## 5. Library Internal Structure

```
src/odc_core/
├── __init__.py            # module re-exports + __version__
├── db.py                  # trusted DSN connect + transient-SQLSTATE retry
├── jobstodo.py            # claim a job, fetch job_details, multi-credential pool
├── duplicate_check.py     # pre-download "already have it?" guard
├── file_allocation.py     # target folder/filename convention
├── file_save_as.py        # move file into place, insert scrape_data row
├── updatejobdetails.py    # spODC_job_details_UpdateStatus wrapper
├── graph_client.py        # Graph send_mail + read_otp_code
├── validate_username.py   # portal username sanity check
├── pdf_auto_copy.py       # copy into the PDF Auto drop folder
└── browser_helpers.py     # human pacing, screenshots, cookie banners
```

Each module owns one concern and holds its SQL as module-private `_UPPER`
constants with `{}` placeholders for table names, formatted from the caller's
`tables` dict at call time. Table names are never taken from user input.

---

## 6. Implementation Notes

- Every module starts with `from __future__ import annotations` (the package
  `__init__.py` does not, matching `automation_core`).
- Every module takes its logger via `logging.getLogger(__name__)` and prefixes
  messages with its own uppercase tag, for example `"JOBSTODO - ..."`. Logging
  uses `%s` lazy formatting, never f-strings. This library never configures
  logging; the calling project does that through `automation_core.setup()`.
- No `print()` anywhere in the library.
- No bare `except:` and no silently swallowed exceptions. Where a failure must
  not interrupt a run (cosmetic browser pacing, error screenshots), the handler
  logs at debug with `exc_info=True` rather than passing.
- `pathlib.Path` for all filesystem paths, never `os.path`.
- Type hints on every function signature, PEP 604 `X | None` unions.
- No em-dashes in code, comments, docstrings, log messages, or documentation.
- The library holds no module-level mutable state, so nothing needs resetting
  between test runs.

---

## 7. Versioning and Release Process

Semantic versioning. PATCH for a bug fix, MINOR for a backward-compatible
addition or behaviour fix, MAJOR for a signature, return-shape, or required
config change.

The version lives in exactly two hand-maintained places that must always match:
`pyproject.toml` `[project] version` and `src/odc_core/__init__.py` `__version__`.

`RELEASING.md` holds the full runbook. In short: bump both versions, add a
`CHANGELOG.md` entry and a `CLAUDE.md` change-log line, update this spec if the
API or config schema moved, run `pytest`, tag `vX.Y.Z`, build the wheel with
`py -m build --wheel --outdir dist`, and publish a GitHub Release with the wheel
attached as an asset. Tags are never moved and releases are never deleted.

---

## 8. Out of Scope

This library deliberately does not provide:

- Logging or notification setup. That is `automation-core`'s job; supplier
  projects call `automation_core.setup()` and use `collect_errors()`.
- Config file loading. Supplier projects own their `config.yaml`.
- Supplier-specific login or search logic. A supplier project supplies its own
  `login()`/`search()` and calls into this library.
- Any Control Room entry point. This is a library and has no `main()`.
