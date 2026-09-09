# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.3] - 2026-09-09
### Fixed
- `human_in_loop`'s confirm-button signal now uses a DOM attribute
  (`element.dataset.confirmed`), not a `window.*` global. Found in live
  testing against `automation-odc-energia` immediately after 0.8.2: a human
  clicked "I'm logged in" (confirmed by the button's own text visibly
  updating), but `human_wait_status`/the wait itself never progressed - the
  heartbeat log kept showing `confirm_button_clicked=False` indefinitely.
  Root cause: `automation-odc-energia` drives the browser via `patchright`,
  a stealth-patched Playwright fork used specifically to avoid tripping
  this portal's reCAPTCHA bot detection. Patches like this commonly route
  injected scripts (`add_init_script`) through an isolated JS world to hide
  automation fingerprints from the page. The DOM is shared across worlds
  (so the button rendered and its own `textContent` update was visible),
  but a `window.*` global set inside that isolated world is not reliably
  visible to a separate `page.evaluate()` call reading it back - so the
  click was genuine but its signal never reached the poll loop. DOM state
  has no such isolation, so switching the flag to a DOM attribute removes
  this failure mode regardless of the exact stealth-patching mechanism in
  use.

## [0.8.2] - 2026-09-09
### Fixed
- `human_in_loop.wait_for_human_login()`'s poll loop no longer treats
  `is_logged_in(page)` as an equal, independent trigger alongside the
  confirm button - only the button click ends the wait successfully now.
  Found in live testing against `automation-odc-energia`: the "Automation
  taking over" banner (and the bot resuming control) could fire before a
  human had actually clicked "I'm logged in", because a URL/DOM-based
  marker can read as "logged in" on an intermediate page mid-login (e.g.
  during a reCAPTCHA redirect) - exactly the ambiguity the original
  `automation-odc-energia` code's own docstring flagged as "not fully
  confirmed against the live portal" when this was ported into 0.8.1's new
  `human_in_loop` module. `is_logged_in(page)` is still called every poll
  tick and logged at each heartbeat, now purely as a diagnostic - not as a
  second way to end the wait.

## [0.8.1] - 2026-09-09
### Changed
- `jobstodo.set_human_wait()` gains three new **required** parameters -
  `rdp_host`, `rdp_username`, `rdp_password` - inserted between `timeout_s`
  and `tables`. This is a breaking signature change from 0.8.0; no consumer
  had pinned to 0.8.0 yet (its wheel was never cut - see Outstanding TODOs),
  so nothing downstream needs a compatibility shim, but `automation-odc-energia`
  Phase 3's call site needs updating before it can take this version.
- `human_wait_status`'s vocabulary changes from a single `WAITING_FOR_HUMAN`
  sentinel to a three-state lifecycle - `NULL` -> `jobstodo.HUMAN_WAIT_PENDING`
  (`"PENDING_HUMAN"`) -> `jobstodo.HUMAN_WAIT_COMPLETE` (`"COMPLETE"`) ->
  `NULL` - see the new `set_human_wait_complete()` entry below.
- `jobstodo.get_job_details()` now left-joins the pre-existing `ODC_suppliers`
  table on `ODC_jobs.supplier_id` and returns its `human_in_loop` column
  (nullable `tinyint`) on every row. **Breaking for every existing caller**,
  not only human-wait/RDP suppliers: `tables` now requires a `suppliers` key.
### Added
- `ODC_jobs` gains three more nullable columns - `rdp_host`, `rdp_username`,
  `rdp_password` - written by `set_human_wait()` and cleared by
  `clear_human_wait()` alongside `human_wait_status`/`human_wait_deadline`.
  The human-assisted-login signal added in 0.8.0 turned out to need these to
  be useful at all: the login flow it signals for is itself an RDP session
  onto a per-job RDS machine, and the polling toolkit has no other way to
  learn which machine or login to use. Stored in plaintext, the same pattern
  already used for portal credentials in `ODC_credentials`/
  `ODC_job_details` - but flagged as a higher-privilege credential than a
  portal login. Combined with 0.8.0's DDL into one `ALTER TABLE` request -
  see spec §4.2. Confirmed applied to both `Titan_INSE_DEV` and the live
  `Titan_INSE` directly via `INFORMATION_SCHEMA.COLUMNS`.
- `jobstodo.set_human_wait_complete(job_id, tables, dsn)` - writes
  `human_wait_status = HUMAN_WAIT_COMPLETE` and nulls the three `rdp_*`
  columns, the cue for a poller's toolkit to disconnect the RDP session.
  Call it once the caller's own wait loop confirms the human actually
  finished logging in (detecting that is the caller's concern, not this
  library's). `clear_human_wait()` still needs calling afterwards on every
  exit path regardless of whether this new function was reached, exactly as
  before - it nulls all five columns together, keeping the
  plaintext-in-Titan window no longer than the wait itself and often
  shorter now that `set_human_wait_complete()` nulls the RDP fields as soon
  as the wait concludes successfully.
- New `human_in_loop` module: `wait_for_human_login(page, is_logged_in,
  job_id, timeout_s, rdp_host, rdp_username, rdp_password, tables, dsn,
  config) -> bool`. Generalises `automation-odc-energia`'s Phase 3 mechanism
  (on-page banner, an injected "I'm logged in" confirm button, a poll loop)
  for any supplier flagged by the new `human_in_loop` column above, and owns
  the full `set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()`
  sequencing internally - a caller supplies only `is_logged_in(page)`, the
  one genuinely portal-specific piece (e.g. a post-login URL/DOM marker).
  See spec §3.2a.
### Removed
- `human_wait_started_at` (added in 0.8.0, never applied to a real
  database) - nothing read it; only `human_wait_deadline` is needed to
  decide when a wait has timed out.

## [0.8.0] - 2026-09-07
### Added
- `jobstodo.set_human_wait(job_id, timeout_s, tables, dsn)` /
  `jobstodo.clear_human_wait(job_id, tables, dsn)` - a job-level "waiting for
  a human-assisted login" signal on three new nullable `ODC_jobs` columns
  (`human_wait_status`, `human_wait_started_at`, `human_wait_deadline`),
  requested as an additive schema change on `Titan_INSE`/`Titan_INSE_DEV`.
  Separate from `ODC_job_details.status` (§4.1 of the spec): that vocabulary
  is per-account and only meaningful once a supplier's `search()` starts,
  whereas a human-assisted login wait happens once per job, before any
  account is individually processed - there is no `job_details_id` to attach
  a per-account status to at that point. `human_wait_deadline` is computed
  from `SYSUTCDATETIME()` server-side so a poller never needs to separately
  know the caller's configured timeout. First consumer: `automation-odc-energia`
  Phase 3 (noVNC human-assisted login for a reCAPTCHA-gated portal). See
  spec §4.2.

## [0.7.0] - 2026-08-21
### Fixed
- Inspired PLC file allocation now names the company folder from the
  SugarCRM account name resolved via `sug_internal_id`, instead of the
  free-text `ODC_job_details.customer_name`. Two jobs for the same company
  with differently-typed `customer_name` values used to land in two
  different folders; they now land in the one SugarCRM-resolved folder.
  `sugar_id.txt` is now seeded with the real id instead of being left empty.
### Added
- New `sugar_client` module: `get_company_name(sug_internal_id, dsn)` looks
  up `accounts.NAME` in SugarCRM (`DSN=Sugar Corp`, MySQL) by internal id.
- `jobstodo._SELECT_JOB_DETAILS` now inner-joins `scrape_accounts` on
  `scrape_accounts_id` and returns `sug_internal_id`, for every client, not
  just Inspired PLC. A `job_details` row with no matching `scrape_accounts`
  row is now excluded from the result (previously included).
- `file_allocation.allocate()` gains an optional `sug_internal_id` parameter
  (default `None`, backward compatible) and falls back to the old
  `customer_name`-keyed folder when it is absent, or when the caller's
  config has no `sugar.dsn`, or when the SugarCRM lookup misses.
- New config key `sugar.dsn` (see README "Expected config shape").

## [0.6.0] - 2026-08-07
### Added
- `updatejobdetails.VALID_STATUSES` gains `REQUIRES RETRY` and
  `MISSING PARENT`, needed by the EDF Energy port's parent/child account
  grouping (a parent reference umbrella covering several `ODC_job_details`
  rows for individual meters). `REQUIRES RETRY` is for a failure that
  should not be treated as permanently `FAILED`/`ERROR`; `MISSING PARENT`
  is for a row searched under a parent account reference where the portal
  says the actual document lives under a specific child account instead.
  Neither is re-fetched automatically by `jobstodo.get_job_details()` for a
  single-credential job - only `pending` rows are re-queried - so a caller
  relying on either status being retried needs its own mechanism to reset
  the row back to `pending`.

## [0.5.0] - 2026-08-04
### Added
- New `db` module: `db.run(dsn, work)` opens a trusted DSN connection, runs a
  unit of work and retries it on transient SQLSTATEs only (`08001`, `08S01`,
  `HYT00`, `HYT01`, `40001`), 4 attempts with 1s/3s/9s backoff. `db.connect(dsn)`
  is a context manager that retries just the connect, for callers that cannot
  express their work as a replayable callable. Anything else, a syntax error or
  a constraint violation, still surfaces on the first attempt.
### Changed
- `jobstodo`, `duplicate_check`, `updatejobdetails` and `file_save_as` now go
  through `db.run()` instead of calling `pyodbc.connect()` directly. Every ODC
  bot runs for hours against Jupiter over the corporate network, so a momentary
  DBNETLIB drop used to cost whatever account was in flight: on 2026-08-02 a
  Pozitive Energy run lost accounts to `('08001', ... SQL Server does not exist
  or access denied ... ConnectionOpen (Connect()))` raised inside
  `duplicate_check.is_duplicate()`, and to `('HYT00', ... Login timeout
  expired)` raised inside `updatejobdetails.update()`. Replaying these units is
  safe: a transient SQLSTATE means nothing was committed, and `file_save_as`
  keeps its `shutil.move` outside the retry so only the insert is replayed.
- `__init__.__version__` was left at `0.4.0` while `pyproject.toml` said
  `0.4.1`; both now read `0.5.0`.
- Tests for the four DB modules patch `odc_core.db.pyodbc.connect` rather than
  each module's own `pyodbc`, which those modules no longer import.

## [0.4.1] - 2026-07-31
### Fixed
- `graph_client` no longer parses digits out of HTML markup as if they were the
  OTP. Graph returns the HTML alternative of the OTP mail by default, and the
  Wave code is wrapped in a styled element - `Your one-time code is: ... <div
  style="color:#202020">965206</div>` - so the 4-8 digit search matched the hex
  colour `202020` on **every** run and the portal rejected it as an invalid
  code. `_list_recent_messages()` now sends
  `Prefer: outlook.body-content-type="text"`, and `_parse_otp_from_body()`
  strips tags, unescapes entities and collapses whitespace before matching, so
  it is correct even if an HTML body arrives anyway (and tolerates a marker
  broken across tags).

## [0.4.0] - 2026-07-30
### Fixed
- `file_save_as.save()` no longer pre-escapes apostrophes before binding them
  as pyodbc query parameters. Values were being double-escaped, so a customer
  name like `Sainsbury's` was stored in `ODC_scrape_data` as `Sainsbury''s`.
  The `VOID` lookup used the same escaping, so both were changed together.
- `graph_client._parse_otp_from_body()` returns `""` instead of the raw
  remaining body text when no 4-8 digit group is found. Previously any email
  containing `body_start` ended the poll loop and returned body text as if it
  were the OTP; `read_otp_code()` now keeps polling.
- Replaced two silent `except Exception: pass` blocks in `browser_helpers`
  with `logger.debug(..., exc_info=True)`, per team-instructions section 11.
### Changed
- Runtime dependencies use minimum-version floors (`pyodbc>=5.3`, `msal>=1.31`,
  `requests>=2.32`) instead of exact pins, matching `lib-core`. Exact pinning
  belongs in the consuming supplier project's `requirements.txt`.
- Added `from __future__ import annotations` to all nine modules and completed
  the type hints on `jobstodo.get_multi_credentials()`.
- `__all__` no longer lists `__version__`, matching `automation_core`.
### Added
- `lib-odc-core-spec.md` - canonical functional/technical spec, mirroring
  `lib-core-spec.md`. `RELEASING.md` now requires keeping it current.
- `CLAUDE.md` `## Migrations` section (required by team-instructions section 5)
  and four new `## Known Gotchas` entries.
- `.cursor/team-instructions.mdc`, copied from `lib-core`.
- Test coverage for `browser_helpers.take_error_screenshot()` and
  `move_mouse_toward()`; hardened the OTP timeout test against a brittle
  fixed `time.time()` sequence.

## [0.3.0] - 2026-07-30
### Changed
- Migrated package layout to `src/odc_core` (setuptools `src`-layout), matching
  `lib-core`/`automation_core`. Import path (`import odc_core`) is unchanged.
- Added `__version__` to `odc_core/__init__.py`, kept in sync with `pyproject.toml`.
- Added `[project.optional-dependencies] dev` (`pytest`, `pytest-mock`) and
  `[tool.pytest.ini_options]` to `pyproject.toml`.
### Added
- Full `pytest` suite under `tests/` covering all 9 modules, mocking
  `pyodbc`/`requests`/`msal`/Playwright at the call site (no real DB/HTTP/browser).
- `RELEASING.md` manual release runbook, mirroring `lib-core`'s.

## [0.2.0] - 2026-07-29
### Added
- `duplicate_check.py` - fuller "already downloaded" check, with an
  `inspired plc` branch cross-checking `ODC_scrape_accounts`/`web_scrape_data`.
- `validate_username.py` - basic username sanity check before login.
### Removed
- Wave's `file_save_as.is_already_downloaded()` (a narrower duplicate of
  `duplicate_check.is_duplicate()`), removed in favour of the new module.

## [0.1.0] - 2026-07-29
### Added
- Package created, porting `shared_resources/` from the legacy
  `supplier_web_scrape` Automation Anywhere framework into an installable,
  config-driven library: `jobstodo`, `file_allocation`, `file_save_as`,
  `updatejobdetails`, `graph_client`, `browser_helpers`, `pdf_auto_copy`.
