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
| `human_in_loop` | no `tables`/`dsn` (0.8.12 - it no longer touches `ODC_jobs`); `job_dir` (a `Path`, passed directly - the bot's own per-run job directory, e.g. `ctx.job_file.parent`); `config` only for `browser_helpers.take_error_screenshot()` on timeout - same keys as `browser_helpers` above |
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
```

`set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()` (added
0.8.0-0.8.1, the job-level human-assisted-login signal on `ODC_jobs`) were
**removed entirely in 0.8.12**, not deprecated - inse-toolkit switched fully
to polling Control Room's own job record, fed by a file-based
`assist.request`/`assist.release` protocol instead (§3.2a, §4.2). The four
`ODC_jobs` columns they wrote (`human_wait_status`, `human_wait_deadline`,
`rdp_host`, `rdp_sessionID`) were dropped via DDL the same day - see §4.2.

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

### 3.2a `human_in_loop` - the generalised human-assisted-login mechanism

```python
request_assist(job_id: str, reason: str, job_dir: Path | str) -> None
release_assist(job_id: str, job_dir: Path | str) -> None

wait_for_human_login(
    page: Any,
    is_logged_in: Callable[[Any], bool],
    job_id: str,
    job_dir: Path | str,
    timeout_s: int,
    config: dict,
    started_message: str = DEFAULT_STARTED_MESSAGE,
) -> bool
```

**0.8.12 re-architected this mechanism entirely**, per
`automation-odc-energia`'s `docs/lib-odc-core-requirements.md`. Control
Room now has its own native "assist" feature (a node agent resident on
each automation host) that provisions and tears down a human-assisted VNC
session on its own - this library's job is only to trigger it and to
signal when it's no longer needed, via a small file-based protocol (§4.2),
not to provision anything itself. Everything below describes the current
design; see CLAUDE.md's Change Log and Known Gotchas for the superseded
`ODC_jobs`-signal/tvnserver-launching design this replaced (removed
entirely, not deprecated - `get_current_hostname()`, `get_current_session_id()`,
`start_vnc_server()`, and `prepare_vnc_session()` no longer exist).

The old single `wait_for_human_login()` call used to both signal "need a
human" and block polling for login completion - which made it impossible
to trigger the signal early (before `page.goto()` navigates anywhere)
without also blocking on a not-yet-navigated page. It is now two calls:

- **`request_assist(job_id, reason, job_dir)`** - non-blocking. Writes
  `assist.request` (`{"reason": reason}`, via a temp-file-then-rename - see
  §4.2) into `job_dir`, signalling Control Room's node agent to provision a
  human-assisted VNC session. Safe, and intended, to call **before**
  `page.goto()` - the node agent's own provisioning (an unknown, possibly
  non-trivial delay) then overlaps with the browser's own navigation and
  cookie-banner handling instead of happening strictly after it. Failures
  (job_dir doesn't exist, permissions, disk full) are logged and swallowed,
  never raised - the browser-side flow still proceeds either way, just
  without an assist session for a human to join.
- **`wait_for_human_login(...)`** - unchanged otherwise from the previous
  design (banner, confirm button, poll loop - see below), but no longer
  triggers assist itself; that already happened via `request_assist()`.
  Calls `release_assist(job_id, job_dir)` in its own `finally` on every
  exit path instead of writing to `ODC_jobs`.

`job_dir` (both functions) is the bot's own per-run job directory - the
same one Control Room's own agent already told the bot about at launch
(e.g. `ctx.job_file.parent`, where `ctx = automation_core.setup(...)`
resolved it from `--job-file`/`CR_JOB_FILE`; the same directory `lib-core`
already writes a `cr_errors.json` sidecar into). This library does not
resolve or guess this path itself - Control Room decides it per run and
could put it anywhere, so the caller supplies it explicitly, the same
philosophy as `dsn`/`tables` being caller-supplied everywhere else in this
package.

`started_message` (0.8.4, trailing/defaulted - existing positional call
sites are unaffected) is the banner text shown while waiting, default
`DEFAULT_STARTED_MESSAGE` ("Automation started - please log in, then click
'I'm logged in' (bottom right)"), generic across every human_in_loop
supplier. A caller with a more specific instruction for the human - e.g.
Energia's "Please resolve the reCAPTCHA challenge, then click 'I'm logged
in'" - passes its own text rather than this library guessing at
portal-specific wording. Only the *started* banner is overridable this way;
the "taking over" and "no response" banners stay fixed, generic text, since
neither references anything portal-specific.

Added 0.8.1, generalised out of `automation-odc-energia`'s Phase 3 (see
`docs/energia-human-assisted-login-control-room-contract.md` in that repo).
For any supplier flagged by `ODC_suppliers.human_in_loop` (surfaced as the
`human_in_loop` column on every row `jobstodo.get_job_details()` returns -
§3.2), `request_assist()`/`wait_for_human_login()` are the two calls a
supplier project needs instead of hand-rolling the file protocol and the
banner/button/poll loop itself.

`page` is assumed to already be sitting on (or navigating to) the login page
- `wait_for_human_login()` does not navigate there itself, since portal
navigation and cookie-banner dismissal (see
`browser_helpers.dismiss_cookie_banner()`) are each supplier's own concern.
`is_logged_in(page) -> bool` is the one genuinely supplier-specific piece
this function cannot provide: a check for whatever marks a successful
login on that particular portal (a URL change, a dashboard element, etc.).
**It does not end the wait on its own** (0.8.2 - see below); it is called
every poll tick and must be defensive regardless - an exception it raises
propagates out of `wait_for_human_login()` itself (ending the wait early,
though `release_assist()` in the `finally` still runs).

What `wait_for_human_login()` does, end to end:

1. Injects a fixed "I'm logged in - continue automation" button (a DOM
   attribute - `element.dataset.confirmed` - read back via `page.evaluate()`
   each tick; not `page.expose_function()`, and not a `window.*` global -
   see the module's own comments above `_LOGIN_CONFIRM_BUTTON_JS` for why:
   0.8.3 found a `window.*` global unreliable under `automation-odc-energia`'s
   use of `patchright`, a stealth-patched Playwright fork whose isolated-JS-world
   script injection shares the DOM but not `window`) and an on-page banner,
   then polls every 1.5s (`human_in_loop.POLL_INTERVAL_S`) until the button is
   clicked - the sole trigger for success (0.8.2 - see below), logging a
   heartbeat every 20s (`HEARTBEAT_INTERVAL_S`) so a stalled wait is visible
   in the log. `is_logged_in(page)` is called every tick too and included in
   the heartbeat log, but purely as a diagnostic: a URL/DOM-based marker can
   read true on an intermediate page mid-login (a reCAPTCHA redirect, for
   instance), so treating it as an equal, independent trigger risked the bot
   taking over while the human was still mid-task - found in live testing
   against `automation-odc-energia`, fixed in 0.8.2. The explicit button
   click has no such ambiguity.
2. On success: shows a "taking over" banner, clears the confirm button,
   then sleeps 5s (`TAKEOVER_PAUSE_S`) before returning `True`, giving the
   human a moment to read the banner and stop interacting while the assist
   session is still live - `release_assist()` (step 4 below) only runs
   after this sleep, so the VNC session isn't torn out from under a human
   still reading the banner.
3. On timeout (no success within `timeout_s`): shows a "no response"
   banner, logs, and takes an error screenshot via
   `browser_helpers.take_error_screenshot()`, then returns `False`.
4. `release_assist(job_id, job_dir)` runs in a `finally`, regardless of
   which path above was taken - including an exception propagating from
   `is_logged_in()`/the poll loop. This is the equivalent guarantee the
   previous design got from a `finally` around `jobstodo.clear_human_wait()`
   - but note the limit that guarantee always had and still has: it only
   covers this process exiting through its own Python call stack. A hard
   kill (crash, `SIGKILL`, power loss) before that line runs writes
   nothing - closing that gap needs Control Room's node agent to
   independently detect a dead bot process, which is outside this
   library's own reach and, as of 0.8.12, unconfirmed either way (see
   `docs/lib-odc-core-requirements.md` item 4 and CLAUDE.md Known
   Gotchas).

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
| `ODC_jobs` | `jobstodo`, `duplicate_check` | One row per supplier job. `process_id` is the claim marker. No longer carries any human-assisted-login signal columns - `human_wait_status`/`human_wait_deadline`/`rdp_host`/`rdp_sessionID`/`rdp_username`/`rdp_password` (added and dropped across 0.8.0-0.8.9) were all dropped via DDL by 0.8.12; see §4.2 for the file-based protocol that replaced them. |
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

### 4.2 Assist file protocol (0.8.12; supersedes the 0.8.0-0.8.9 `ODC_jobs` human-wait signal)

The job-level human-assisted-login signal no longer lives on `ODC_jobs` at
all. Control Room now has its own native "assist" feature (a node agent
resident on each automation host); this library's role shrank to two file
writes into the bot's own per-run job directory, which the node agent
watches directly:

- `human_in_loop.request_assist()` writes **`assist.request`**:
  ```json
  {"reason": "captcha on the Energia portal"}
  ```
- `human_in_loop.release_assist()` writes **`assist.release`** (empty file).

Both are written via a temp-file-then-rename (`human_in_loop._atomic_write()`)
so the node agent never observes a partially-written file - `Path.replace()`,
the same atomicity guarantee as `os.replace()` (`MoveFileEx` with
`MOVEFILE_REPLACE_EXISTING` on Windows, a single `rename()` syscall on
POSIX). Both writes are best-effort: a failure (job_dir doesn't exist,
permissions, disk full) is logged and swallowed, never raised.

Once assist is active, Control Room's own existing job-state endpoint
(`GET /api/v1/jobs/{job_id}` - not a new endpoint) carries these additional
fields on the job record, which `inse-toolkit` polls directly - this
library neither implements this endpoint nor reads these fields itself:

| Field | Meaning |
|---|---|
| `assist_phase` | `null` -> `starting` -> `live` -> `stopped`. `null` = not requested/not currently assisted. |
| `assist_url` | The noVNC join URL. Populated only once `assist_phase == "live"`. |
| `assist_reason` | Echo of whatever the bot wrote to `assist.request`'s `reason` field. |
| `assist_expires_in_s` | A countdown, in seconds. |

**Status: several details here are best-effort implementations of a
protocol described secondhand, not yet independently verified against
Control Room's real API/node-agent docs** - see
`automation-odc-energia/docs/lib-odc-core-requirements.md` items 3-6 and
CLAUDE.md's Known Gotchas/Outstanding TODOs for the full list of open
items (exact `assist_phase` vocabulary/casing, whether `assist_url` is
unconditionally usable as an `<iframe src>` with no extra credential,
`assist_expires_in_s`'s authoritative-vs-advisory semantics, the node
agent's actual noVNC port, and the exact `assist.request`/`assist.release`
file schema/atomicity itself). None of these are lib-odc-core's own fields
to confirm - they're Control Room/`inse-toolkit`'s.

**Crash-path teardown is not fully guaranteed.** `release_assist()` runs
from `wait_for_human_login()`'s own `finally`, covering every exit through
this process's own Python call stack (success, failure, timeout, or an
exception from `is_logged_in()`). A hard kill (crash, `SIGKILL`, power
loss) before that line runs writes nothing at all - closing that gap needs
Control Room's node agent to independently detect a dead bot process,
which is outside this library's own reach and unconfirmed either way (see
`docs/lib-odc-core-requirements.md` item 4).

**Historical note, kept for the record:** `ODC_jobs` previously carried
`human_wait_status`/`human_wait_deadline` (0.8.0), `rdp_host`/
`rdp_username`/`rdp_password` (0.8.1, RDP-era), `rdp_sessionID` (0.8.9,
after the 0.8.8 revert to VNC). `rdp_username`/`rdp_password` were dropped
via DDL in 0.8.8 once the mechanism reverted to VNC (no credential
needed); the remaining four (`human_wait_status`, `human_wait_deadline`,
`rdp_host`, `rdp_sessionID`) were dropped via
`ALTER TABLE ... DROP COLUMN` on both `Titan_INSE_DEV` and the live
`Titan_INSE` on 2026-09-14, confirmed via `INFORMATION_SCHEMA.COLUMNS`
immediately after, the same day 0.8.12 stopped writing to them. See
CLAUDE.md's Change Log for the full sequence if that history is ever
needed.

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
    human_in_loop.request_assist(job_id, "captcha on the portal", job_dir)  # before page.goto()
    # ... browser launch, page.goto(), cookie-banner dismissal ...
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
├── jobstodo.py            # claim a job, fetch job_details, multi-credential pool
├── human_in_loop.py       # generalised human-assisted-login: request_assist()/
│                          # release_assist() (assist.request/assist.release file
│                          # protocol), banner, confirm button, poll loop
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
