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
    suppliers: ODC_suppliers
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

pdf_auto:
  base_path: I:\BPI\Automation Team\Automated Processes\ODC

env: dev
```

### 2.2 Which keys each module needs

| Module | Requires |
|--------|----------|
| `jobstodo`, `file_save_as`, `duplicate_check`, `updatejobdetails` | `tables`, `dsn` (passed directly, not the whole config) |
| `db` | `dsn` only (passed directly) |
| `file_allocation` | `config["file_allocation"]`; for Inspired PLC also `config["sugar"]["dsn"]` and a `sug_internal_id` argument |
| `sugar_client` | `dsn` only (passed directly) |
| `graph_client` | `config["graph"]` |
| `browser_helpers` | `config["delays"]`, plus `_logs_dir`/`_process_id`/`_supplier_name` for screenshots |
| `human_in_loop` | `tables`, `dsn` (passed directly, delegated to `jobstodo`); `config` only for `browser_helpers.take_error_screenshot()` on timeout - same keys as `browser_helpers` above |
| `pdf_auto_copy` | `config["env"]`, `config["pdf_auto"]["base_path"]` |
| `validate_username` | nothing |

Graph credentials are never hardcoded in this package. A caller that omits
`tenant_id`, `client_id`, or `client_secret` gets a `ValueError`.

**Breaking for every existing caller of `jobstodo.get_job_details()`** (added
0.8.1, not just the human-wait/RDP suppliers): `tables` now requires a
`suppliers` key (`ODC_suppliers`) alongside the existing ones - `get_job_details()`
now left-joins it for the new `human_in_loop` column (§3.2/§4.3). Every
supplier project's `config.yaml`, not only `automation-odc-energia`'s, needs
this key added before it can take this version - see CLAUDE.md Outstanding
TODOs.

---

## 3. Public API

### 3.1 Shape of the API

`odc_core/__init__.py` re-exports the twelve **modules**, not their individual
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
set_human_wait(job_id: str, timeout_s: int, rdp_host: str, rdp_username: str, rdp_password: str, tables: dict, dsn: str) -> None
set_human_wait_complete(job_id: str, tables: dict, dsn: str) -> None
clear_human_wait(job_id: str, tables: dict, dsn: str) -> None
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
joined `ODC_jobs` and `ODC_job_details` columns keyed by column name, plus
`sug_internal_id` from an inner join to `ODC_scrape_accounts` on
`scrape_accounts_id` (added 0.7.0, for every client, not just Inspired PLC).
A `job_details` row with no matching `scrape_accounts` row is excluded from
the result.

`set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()` (the
first and last added 0.8.0, `rdp_*` params on `set_human_wait()` and
`set_human_wait_complete()` itself added 0.8.1) write a job-level
"waiting for a human-assisted login" signal to `ODC_jobs` - see §4.2. This
is a separate concern from the `ODC_job_details.status` vocabulary in §4.1:
that vocabulary is per-account and only meaningful once a supplier's
`search()` starts, whereas a human-assisted login wait happens once per
job, before any account is individually processed.

`human_wait_status` moves through three states: `NULL` ->
`HUMAN_WAIT_PENDING` (`"PENDING_HUMAN"`) -> `HUMAN_WAIT_COMPLETE`
(`"COMPLETE"`) -> `NULL`. `set_human_wait()` writes `HUMAN_WAIT_PENDING`
and computes `human_wait_deadline` from `SYSUTCDATETIME()` on the database
server, so a caller passes the same `timeout_s` its own wait loop uses and
the two can never drift apart. It also writes the RDS machine and login
(`rdp_host`/`rdp_username`/`rdp_password`) the poller's toolkit needs to
open the RDP session behind the login view - the human-assisted flow the
signal exists for cannot connect without them, so these three are
required, not optional, arguments. They are stored in plaintext, the same
pattern already used for portal credentials in `ODC_credentials`/
`ODC_job_details`.

Detecting that the human has actually finished logging in is the caller's
own concern, not this library's - e.g. an in-page confirm button or a
post-login URL check polled from the caller's own wait loop. Most callers
should not call `set_human_wait()`/`set_human_wait_complete()`/
`clear_human_wait()` directly at all - see `human_in_loop.wait_for_human_login()`
(§3.2a), which wraps all three around exactly this kind of poll loop. Once a
wait loop confirms
success, it calls `set_human_wait_complete()`, which writes
`HUMAN_WAIT_COMPLETE` and nulls `rdp_host`/`rdp_username`/`rdp_password` -
the cue for the toolkit to disconnect the RDP session, since those
credentials are no longer valid for this job once it does. The caller must
still call `clear_human_wait()` on every exit path regardless of whether
`set_human_wait_complete()` was reached - success, failure, and timeout
alike - which nulls all five columns (including `human_wait_status` itself)
so no credential lingers in the database and no poller mistakes a finished
or abandoned run for one still waiting on a human. `set_human_wait_complete()`
should only be called after a confirmed success; a failure/timeout exit
calls `clear_human_wait()` directly from `HUMAN_WAIT_PENDING`; a poller must
never read `HUMAN_WAIT_COMPLETE` for a login that didn't actually succeed.

Neither `set_human_wait()`, `set_human_wait_complete()`, nor
`clear_human_wait()` raises on its own for a missing column - if the
`ODC_jobs` schema change in §4.2 has not been applied to a given database
yet, the underlying `pyodbc.Error` propagates like any other query error,
and callers should treat that as "no signal support on this database yet"
rather than a fatal condition.

### 3.2a `human_in_loop` - the generalised human-assisted-login mechanism

```python
wait_for_human_login(
    page: Any,
    is_logged_in: Callable[[Any], bool],
    job_id: str,
    timeout_s: int,
    rdp_host: str,
    rdp_username: str,
    rdp_password: str,
    tables: dict,
    dsn: str,
    config: dict,
) -> bool
```

Added 0.8.1, generalised out of `automation-odc-energia`'s Phase 3 (see
`docs/energia-human-assisted-login-control-room-contract.md` in that repo,
which predates this generalisation and still describes a bespoke,
Energia-only implementation). For any supplier flagged by
`ODC_suppliers.human_in_loop` (surfaced as the `human_in_loop` column on
every row `jobstodo.get_job_details()` returns - §3.2), this is the one
call a supplier project needs instead of hand-rolling the banner/button/poll
loop and the `jobstodo.set_human_wait()`/`set_human_wait_complete()`/
`clear_human_wait()` sequencing itself.

`page` is assumed to already be sitting on (or navigating to) the login page
- this function does not navigate there itself, since portal navigation and
cookie-banner dismissal (see `browser_helpers.dismiss_cookie_banner()`) are
each supplier's own concern. `is_logged_in(page) -> bool` is the one
genuinely supplier-specific piece this function cannot provide: a check for
whatever marks a successful login on that particular portal (a URL change,
a dashboard element, etc.). It is called every poll tick and must be
defensive - an exception it raises propagates out of `wait_for_human_login()`
itself (ending the wait early, though `clear_human_wait()` still runs).

What it does, end to end:

1. `jobstodo.set_human_wait(job_id, timeout_s, rdp_host, rdp_username, rdp_password, tables, dsn)`.
2. Injects a fixed "I'm logged in - continue automation" button (a plain JS
   flag read back via `page.evaluate()` each tick, not `page.expose_function()`
   - see the module's own comments for why) and an on-page banner, then
   polls every 1.5s (`human_in_loop.POLL_INTERVAL_S`) for either the button
   or `is_logged_in(page)` to return true, logging a heartbeat every 20s
   (`HEARTBEAT_INTERVAL_S`) so a stalled wait is visible in the log.
3. On success: shows a "taking over" banner, clears the confirm button,
   calls `jobstodo.set_human_wait_complete()`, then sleeps 5s
   (`TAKEOVER_PAUSE_S`) before returning `True` - this pause does double
   duty as the human's moment to stop interacting with the page *and* as
   the window during which a poller can observe `HUMAN_WAIT_COMPLETE`
   before the next step clears it.
4. `jobstodo.clear_human_wait()` always runs in a `finally`, regardless of
   success, timeout, or an exception from `is_logged_in()`.
5. On timeout (no success within `timeout_s`): shows a "no response"
   banner, logs, and takes an error screenshot via
   `browser_helpers.take_error_screenshot()`, then returns `False`.
   `set_human_wait_complete()` is never called on this path - a poller must
   never read `HUMAN_WAIT_COMPLETE` for a login that didn't succeed.

Failures writing the `ODC_jobs` signal at any of the three steps above are
logged and swallowed, not fatal to the run, matching §4.2's "no signal
support on this database yet" tolerance - the browser-side wait still
proceeds regardless of whether the DB signal succeeded.

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
         config: dict, sug_internal_id: str | None = None) -> dict
```

Returns `{"folder_location": str, "complete_filename": str, "client_filepath": str}`
where `complete_filename` excludes the extension. Creates the directory tree as
a side effect.

| | Inspired PLC | Every other client |
|---|---|---|
| Folder | `{client_location}/{company_name}/{INVOICE\|LETTER}/NEW/{utility}` | `{client_location}/{YYYY-MM}` |
| Filename | `{supplier}_{account_reference}_{utility}_{invoice_number}_{YYYYMMDD}` | `{supplier}_{account_reference}_{meter_number}_{utility}_{invoice_number}_{YYYYMMDD}` |
| Extra | Builds the full doc_type/status/utility tree and seeds `sugar_id.txt` with `sug_internal_id` | Creates the month folder only |

`doc_type == "O"` maps to `LETTER`; anything else maps to `INVOICE`. Utility is
normalised to `Elec` or `Gas` by substring match, otherwise passed through.

`company_name` (added 0.7.0) is resolved by calling
`sugar_client.get_company_name(sug_internal_id, config["sugar"]["dsn"])` and
falls back to the raw `customer_name` when `sug_internal_id` is `None`,
`config["sugar"]["dsn"]` is absent, or the SugarCRM lookup finds no active
account. This replaced keying the folder on `customer_name` directly, which
let two job rows for the same company land in different folders if their
free-text `customer_name` differed.

### 3.4a `sugar_client` - SugarCRM account lookup

```python
get_company_name(sug_internal_id: str, dsn: str) -> str | None
```

Looks up `accounts.NAME` in SugarCRM (`DSN=Sugar Corp`, MySQL, not
Titan/Jupiter) for the given internal id, filtered to `deleted = 0`. Returns
`None` when no matching active account exists. Takes `dsn` directly rather
than a `tables` dict: `accounts` is SugarCRM's own fixed schema table, not an
ODC-owned table.

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
| `ODC_jobs` | `jobstodo`, `duplicate_check` | One row per supplier job. `process_id` is the claim marker; `human_wait_status`/`human_wait_deadline` (0.8.0) plus `rdp_host`/`rdp_username`/`rdp_password` (0.8.1) are the job-level human-assisted-login signal and its RDP connection details - see §4.2. |
| `ODC_job_details` | `jobstodo`, `duplicate_check`, `updatejobdetails` | One row per account to collect. Carries `status`. |
| `ODC_scrape_data` | `file_save_as`, `duplicate_check` | One row per downloaded document. |
| `ODC_scrape_accounts` | `jobstodo`, `duplicate_check` | Inspired PLC account pool; also the source of `sug_internal_id` for every client via `jobstodo.get_job_details()`. |
| `ODC_suppliers` | `jobstodo` | One row per supplier (pre-existing, not owned by this library). Source of `human_in_loop` (§4.3), left-joined by `jobstodo.get_job_details()`. |
| `ODC_multi_credentials`, `ODC_credentials` | `jobstodo` | Shared credential pool for multi-credential jobs. |
| `jupiter.aa[_dev].web_scrape_data` | `duplicate_check` | Older parallel pipeline, consulted for Inspired PLC only. |
| `spODC_job_details_UpdateStatus` | `updatejobdetails` | The only supported way to change a job_details status. |
| SugarCRM `accounts` (`DSN=Sugar Corp`) | `sugar_client` | Resolves the Inspired PLC company name from `sug_internal_id`. Owned by SugarCRM, not Titan. |

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

### 4.2 Human-wait signal (added 0.8.0; RDP fields, the PENDING/COMPLETE lifecycle, and `human_wait_started_at`'s removal all added in 0.8.1)

A separate, job-level signal on `ODC_jobs`, distinct from §4.1's per-account
vocabulary. Written only by `jobstodo.set_human_wait()`/
`set_human_wait_complete()`/`clear_human_wait()`; no other module reads or
writes these columns. **Confirmed applied to both `Titan_INSE_DEV` and the
live `Titan_INSE`** - checked directly via `INFORMATION_SCHEMA.COLUMNS` on
2026-09-09 (see CLAUDE.md Change Log). It never included
`human_wait_started_at`: that column was part of the original 0.8.0 design
but dropped before any DDL request went out, since nothing reads it - only
`human_wait_deadline` is needed for the timeout decision. One drift from
the DDL below: the applied `human_wait_status` is `VARCHAR(50)`, not
`NVARCHAR(30)` - harmless for the short ASCII values this library writes.

```sql
ALTER TABLE dbo.ODC_jobs ADD
    human_wait_status   NVARCHAR(30)  NULL,   -- NULL = not waiting; 'PENDING_HUMAN' = waiting; 'COMPLETE' = human finished, not yet cleared
    human_wait_deadline DATETIME2     NULL,   -- UTC, set when the wait begins = SYSUTCDATETIME() + timeout_s
    rdp_host             NVARCHAR(255) NULL,   -- RDS machine hostname/IP for the human-assisted RDP session
    rdp_username          NVARCHAR(128) NULL,   -- RDP login for rdp_host
    rdp_password          NVARCHAR(256) NULL;   -- RDP password for rdp_host, plaintext (see below)
```

`human_wait_status` lifecycle - `NULL` -> `PENDING_HUMAN` -> `COMPLETE` ->
`NULL`:

1. **`set_human_wait()`** writes `human_wait_status = 'PENDING_HUMAN'`
   (`jobstodo.HUMAN_WAIT_PENDING`), `human_wait_deadline`, and
   `rdp_host`/`rdp_username`/`rdp_password` - the cue for a poller's
   toolkit to open the RDP session and connect.
2. The caller's own wait loop - not this library - detects the human has
   actually finished logging in (e.g. `automation-odc-energia`'s
   `wait_for_human_login()` polls an in-page "I'm logged in" button and a
   post-login URL check, entirely inside its own Playwright session; it
   never reads `ODC_jobs` for this).
3. On confirmed success, **`set_human_wait_complete()`** writes
   `human_wait_status = 'COMPLETE'` (`jobstodo.HUMAN_WAIT_COMPLETE`) and
   nulls `rdp_host`/`rdp_username`/`rdp_password` - the cue for the toolkit
   to disconnect the RDP session, since those credentials are no longer
   valid for this job once it does. On a failure/timeout exit instead, this
   step is skipped entirely - a poller must never read `COMPLETE` for a
   login that didn't actually succeed.
4. **`clear_human_wait()`** nulls all five columns, including
   `human_wait_status` itself. Called on every exit path regardless of
   whether step 3 was reached - success, failure, and timeout alike - so
   nothing downstream mistakes a finished or abandoned run for one still
   waiting on a human, and no stale RDP credential is left behind.

Column notes:

- `human_wait_deadline`: computed from `SYSUTCDATETIME()` on the database
  server (not the run node), so a poller never has to know the caller's
  configured timeout - it only compares its own clock against
  `human_wait_deadline`. Meaningless once `human_wait_status` leaves
  `PENDING_HUMAN`; not read again after that.
- `rdp_host` / `rdp_username` / `rdp_password`: the RDS machine and login a
  poller's toolkit uses to open the RDP session behind the login view - the
  human-assisted flow this signal exists for cannot connect without them.
  Assigned per-job (the bot is handed a machine per run, not a fixed shared
  one), so these are written fresh by every `set_human_wait()` call rather
  than read from static config. Stored in plaintext, matching the existing
  portal-credential pattern in `ODC_credentials`/`ODC_job_details`; treat
  this as a genuinely higher-privilege credential than a portal login (RDP
  access to a machine, not a single supplier website) - `set_human_wait_complete()`
  nulls these as soon as the wait concludes successfully, and
  `clear_human_wait()` nulls them again regardless, so the plaintext-in-Titan
  exposure window is no longer than the wait itself, and often shorter.
- All three functions raise the underlying `pyodbc.Error` if these columns
  don't exist yet on the target database (this is an additive, opt-in
  schema change - see the DDL above); callers should treat that as "no
  signal support here yet", not a fatal error for the run itself.
- Not currently read by any consumer of this library; it exists for an
  external system (a bot orchestrator/toolkit) that polls `ODC_jobs`
  directly to know when to surface a human-assisted login session, how to
  connect to it, and when to disconnect.

### 4.3 `ODC_suppliers.human_in_loop` (pre-existing column, first consumed 0.8.1)

A pre-existing, Titan-owned column - not added by this library, and not part
of the §4.2 DDL. `human_in_loop` (nullable `tinyint`) lives on `ODC_suppliers`,
one row per supplier (`Wave`, `British Gas`, `Energia`, ...), **not** on
`ODC_jobs` - it is a per-supplier configuration flag, not a per-job or
per-account one. Confirmed present on both `Titan_INSE_DEV` and the live
`Titan_INSE` directly: as of 2026-09-09 only the `Energia` row has it set to
`1`; every other supplier row has it `NULL` (not `0` - both read as falsy).

`jobstodo.get_job_details()` left-joins `ODC_suppliers` on
`ODC_jobs.supplier_id` (§3.2) and returns `human_in_loop` on every row, so a
caller can branch on it directly:

```python
rows = jobstodo.get_job_details(process_id, job_id, tables, dsn)
if rows and rows[0]["human_in_loop"]:
    success = human_in_loop.wait_for_human_login(...)  # §3.2a
```

A left join, not the inner join `scrape_accounts` uses: a job row with no
matching `ODC_suppliers` row still comes back, with `human_in_loop` as
`None` rather than being silently excluded (unlike the `scrape_accounts`
gotcha - see CLAUDE.md Known Gotchas).

---

## 5. Library Internal Structure

```
src/odc_core/
├── __init__.py            # module re-exports + __version__
├── db.py                  # trusted DSN connect + transient-SQLSTATE retry
├── jobstodo.py            # claim a job, fetch job_details, multi-credential pool,
│                          # human-wait signal (set_human_wait/set_human_wait_complete/clear_human_wait)
├── human_in_loop.py       # generalised human-assisted-login: banner, confirm
│                          # button, poll loop, wraps jobstodo's human-wait signal
├── duplicate_check.py     # pre-download "already have it?" guard
├── file_allocation.py     # target folder/filename convention
├── file_save_as.py        # move file into place, insert scrape_data row
├── updatejobdetails.py    # spODC_job_details_UpdateStatus wrapper
├── sugar_client.py        # SugarCRM account name lookup by internal id
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
