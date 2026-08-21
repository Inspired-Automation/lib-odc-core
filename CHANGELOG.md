# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
