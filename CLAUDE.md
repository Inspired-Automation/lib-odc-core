# CLAUDE.md

## Purpose
Shared ODC (Online Data Collection) infrastructure library. Provides job claiming,
file allocation/save, job-status updates, Microsoft Graph mail/OTP helpers, and
browser-automation pacing helpers used by every ODC supplier bot project
(`automation-odc-wave`, `automation-odc-british-gas`, and future suppliers).
This is a library, not a Control Room bot - it has no entry point of its own.

## Tech Stack
- Python 3.14
- `pyodbc` - MSSQL access (trusted connections, parameterised queries)
- `msal`, `requests` - Microsoft Graph token acquisition and API calls
- Built and distributed as a wheel via GitHub Releases (same pattern as
  `lib-core`/`automation_core`), not published to PyPI.

## Databases
- No fixed DSN - every function takes `dsn` and `tables` explicitly; the
  calling project owns `config.yaml` and passes these in. In practice all
  current callers use `DSN=Jupiter` against `Titan_INSE_DEV`/`Titan_INSE`
  tables: `ODC_jobs`, `ODC_job_details`, `ODC_scrape_data`,
  `ODC_scrape_accounts`, `ODC_multi_credentials`, `ODC_credentials`, plus
  `jupiter.aa_dev.web_scrape_data` / `jupiter.aa.web_scrape_data` (an older
  parallel pipeline consulted only for `client_name == "inspired plc"`
  duplicate checks), and the stored procedure `spODC_job_details_UpdateStatus`.
- `sugar_client.py` (added 0.7.0) is the one module that talks to a second,
  unrelated database: SugarCRM itself (`DSN=Sugar Corp`, MySQL), read-only,
  looking up `accounts.NAME` by id. Not Titan, not Jupiter - a caller must
  supply this DSN separately via `config["sugar"]["dsn"]`.

## External Integrations
- Microsoft Graph (`graph_client.py`): `send_mail()` and `read_otp_code()`.
  Both require the caller to supply `config["graph"]` with `client_id`,
  `client_secret`, `tenant_id`, and (for `send_mail`) `sender_address`.
  Credentials are never hardcoded here - each calling project supplies them
  via its own `config.yaml`.

## Configuration
- This package reads no config file itself. See README.md "Expected config
  shape" for the `database`/`tables`/`graph`/`delays`/`file_allocation` keys
  every calling project must provide.

## Migrations
- None. This library issues no DDL and owns no `migrations/` folder. The
  `ODC_*` tables, `jupiter.aa*.web_scrape_data`, and the stored procedure
  `spODC_job_details_UpdateStatus` are owned by the Titan database; schema
  changes to them are made and tracked outside this repo.

| File | DEV applied | PROD applied |
|------|-------------|--------------|
| n/a  | n/a         | n/a          |

## Key Business Logic
- Job claiming: `jobstodo.get_job_details()` stamps `ODC_jobs.process_id`
  before reading `ODC_job_details`, so two concurrent runs of the same job
  do not double-process it.
- Multi-credential jobs: a job row with `username == password == "multi_credential"`
  is handled by iterating `ODC_multi_credentials`/`ODC_credentials` against a
  shared account pool instead of single fixed credentials.
- `file_save_as.save()` marks a record `VOID` when the downloaded file is
  under 1 KB (treated as a failed/empty download).
- `duplicate_check.is_duplicate()` must be called before downloading a
  document, not after - callers skip the download entirely when it returns
  True. `client_name == "inspired plc"` accounts take a different query path
  (checks `scrape_accounts`/`web_scrape_data` too) than every other client.
- File allocation for `client_name == "inspired plc"` (added 0.7.0) names the
  company folder from the SugarCRM account name resolved via
  `sug_internal_id` (fetched by `jobstodo.get_job_details()`'s join to
  `ODC_scrape_accounts`), not from the free-text `ODC_job_details.customer_name`.
  `file_allocation.allocate()` falls back to `customer_name` if
  `sug_internal_id` is missing, `config["sugar"]["dsn"]` is not supplied, or
  the SugarCRM lookup misses - so an unconfigured caller keeps the old
  behaviour rather than erroring.
- Status vocabulary shared across all suppliers: `DOWNLOADED`,
  `PARTIALLY DOWNLOADED`, `NOT REQUIRED`, `NOT FOUND`, `ERROR`, `FAILED`,
  `IN PROGRESS`, `FOUND`, `REQUIRES RETRY`, `MISSING PARENT`. `NOT FOUND` is
  a business fact: the portal answered and the account or document is
  absent. A timeout is `ERROR`. Suppliers must not collapse the two.
  `REQUIRES RETRY`/`MISSING PARENT` (added 0.6.0, for EDF Energy's
  parent/child account grouping) are not re-fetched automatically - see
  Known Gotchas.
- All MSSQL access goes through `db.run(dsn, work)`, which retries the unit of
  work on the transient SQLSTATEs in `db.TRANSIENT_SQLSTATES` and lets every
  other error surface on the first attempt. Replaying is safe for the units in
  this package because a transient SQLSTATE means nothing was committed; check
  that before wrapping a partially-committed multi-statement unit.

## Known Gotchas
- The original per-supplier framework hardcoded Graph `client_id`/`client_secret`/
  `tenant_id` directly in source (`graph_otp.py`, `mailer.py`). This package
  fixes that: credentials always come from the caller's `config["graph"]`.
- `db.run()` closes the connection it opened, so a `work()` callable that
  mutates data must `conn.commit()` itself. The modules here all do. This
  differs from the bare `with pyodbc.connect(...)` these modules used before
  0.5.0, which committed on exit and left the connection open.
- Tests for the DB modules patch `odc_core.db.pyodbc.connect`, not each module's
  own `pyodbc`: those modules no longer import it.
- `file_save_as.save()` still accepts a `folder_location` argument that it
  never reads (the target directory is derived from `target_path`). Left in
  place because removing it breaks every caller's signature; drop it at the
  next MAJOR version.
- `duplicate_check._CHECK_INSPIRED` hardcodes the literal
  `a.client_name = 'Inspired PLC'` while the branch that selects it lowercases
  the caller's `client_name`. If `ODC_scrape_accounts` ever stores a different
  casing or spelling, the query silently returns zero matches and the document
  is re-downloaded rather than skipped.
- `graph_client.read_otp_code()` calls `_acquire_token()` on every poll
  iteration, so a full 3600s wait at the default 10s interval requests ~360
  Graph tokens. Works, but acquire-once-and-refresh would be cheaper.
- `file_allocation` writes `sugar_id.txt` with `encoding="ansi"`, which Python
  resolves to `mbcs`. That codec is Windows-only, so this module cannot run on
  Linux. Fine for Control Room bots; worth knowing before any container move.
- `jobstodo._SELECT_JOB_DETAILS`'s new join to `ODC_scrape_accounts` (0.7.0)
  is an INNER JOIN and applies to every client, not just Inspired PLC. A
  `job_details` row whose `scrape_accounts_id` has no matching
  `ODC_scrape_accounts` row is now silently excluded from the pending/searchable
  results for every supplier - confirmed acceptable because every current row
  is expected to have a match, but worth checking first if a future supplier's
  data does not guarantee that.
- `REQUIRES RETRY` and `MISSING PARENT` are validated by `VALID_STATUSES` and
  written to the database, but nothing in the current estate resets a row in
  either status back to `pending`. `jobstodo.get_job_details()` only
  re-fetches `pending` rows for a single-credential job, so a supplier using
  these two statuses to mean "try again on a later run" needs its own
  reset mechanism (or to run as a multi-credential job, whose status filter
  does not exclude them); today no such mechanism exists.

## Change Log
- 2026-08-21: v0.7.0 - fixed Inspired PLC file allocation to key the company
  folder on the SugarCRM account name (resolved from the new `sug_internal_id`,
  via `jobstodo`'s inner join to `ODC_scrape_accounts` and the new
  `sugar_client.get_company_name()`) instead of the free-text
  `ODC_job_details.customer_name`. `sugar_id.txt` is now seeded with the real
  id instead of being left empty. New config key: `sugar.dsn`. See the new
  Known Gotchas entry about the join being an unconditional INNER JOIN.
- 2026-08-07: v0.6.0 - added `REQUIRES RETRY` and `MISSING PARENT` to
  `updatejobdetails.VALID_STATUSES` for the EDF Energy port's parent/child
  account grouping. See the new Known Gotchas entry: neither status is
  automatically re-picked-up by `jobstodo.get_job_details()` today.
- 2026-08-04: v0.5.0 - added the `db` module and routed `jobstodo`,
  `duplicate_check`, `updatejobdetails` and `file_save_as` through it, so a
  momentary DBNETLIB drop no longer costs whatever account was in flight. On
  2026-08-02 a Pozitive Energy run lost accounts to `('08001', ... SQL Server
  does not exist or access denied ... ConnectionOpen (Connect()))` inside
  `duplicate_check.is_duplicate()` and to `('HYT00', ... Login timeout expired)`
  inside `updatejobdetails.update()`, with no retry anywhere in the stack. Only
  the SQLSTATEs in `db.TRANSIENT_SQLSTATES` retry; a syntax error or constraint
  violation still surfaces immediately. Also fixed `__init__.__version__`, which
  had been left at `0.4.0` while `pyproject.toml` said `0.4.1`.
- 2026-07-29: Package created, porting `shared_resources/` from the legacy
  `supplier_web_scrape` Automation Anywhere framework into an installable,
  config-driven library for use by Control Room ODC supplier projects.
- 2026-07-29: v0.2.0 - added `duplicate_check.py` (fuller "already
  downloaded" check, with an `inspired plc` branch) and
  `validate_username.py`, ported from `shared_resources/`. Wave's
  `file_save_as.is_already_downloaded()` (a narrower duplicate of this) was
  removed in favour of `duplicate_check.is_duplicate()`.
- 2026-07-30: v0.3.0 - restructured to match `lib-core`'s conventions:
  migrated to `src/odc_core` layout, added `__version__`, added a full
  `pytest` suite under `tests/` (mocking pyodbc/requests/msal/Playwright),
  added `CHANGELOG.md` and `RELEASING.md`.
- 2026-07-30: v0.4.0 - code-level parity pass against
  `.cursor/team-instructions.mdc`: added `from __future__ import annotations`
  to every module, replaced two silent `except Exception: pass` blocks in
  `browser_helpers`, and completed the type hints. Fixed two defects found in
  the ported code - `file_save_as` double-escaped apostrophes into the
  database, and `graph_client._parse_otp_from_body()` returned raw body text
  as if it were an OTP when no digit group matched. Added
  `lib-odc-core-spec.md` and this `## Migrations` section.
- 2026-07-30: repo pushed to `github.com/Inspired-Automation/lib-odc-core`
  (note: the org name is hyphenated, same as `Inspired-Automation/lib-core`;
  the un-hyphenated `InspiredAutomation` form used briefly in earlier docs
  does not exist and made the documented `pip install` URLs 404).

## Outstanding TODOs
- Cut a `v0.7.0` release with the built wheel attached, following
  `RELEASING.md`. Any supplier project whose jobs can carry
  `client_name == "inspired plc"` rows (at least `automation-odc-wave` and
  `automation-odc-british-gas` call `file_allocation.allocate()` today) needs
  to pin `v0.7.0` and add a `sugar:` block to its own `config.yaml`, and pass
  the new `sug_internal_id` value (now returned by `jobstodo.get_job_details()`)
  through to `allocate()`, before the SugarCRM-based folder naming actually
  takes effect for them - until then they keep the old `customer_name`
  fallback behaviour.
- Confirm `ODC_scrape_accounts.sug_internal_id` actually exists in both
  `Titan_INSE_DEV` and `Titan_INSE` with that exact column name before
  releasing v0.7.0 - it was assumed present per the SugarCRM/Titan sync, not
  verified against the live schema from this repo.
- Cut a `v0.6.0` release with the built wheel attached, following
  `RELEASING.md`. Only `automation-odc-edf-energy` needs to pin `v0.6.0`
  (it is the only consumer of the two new statuses so far); every other
  supplier stays on `v0.5.0`.
- Verify `spODC_job_details_UpdateStatus` (Titan-owned, outside this repo)
  does not itself reject `REQUIRES RETRY`/`MISSING PARENT` as unrecognised
  status text before the EDF Energy bot relies on it in production.
- Decide whether `REQUIRES RETRY`/`MISSING PARENT` need an actual retry
  mechanism (a scheduled reset back to `pending`, or running affected jobs
  as multi-credential) now that a real consumer (EDF Energy) exists, per
  the Known Gotchas entry above.
- Remove the unused `folder_location` parameter from `file_save_as.save()`
  at the next MAJOR version.
