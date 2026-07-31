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
- Status vocabulary shared across all suppliers: `DOWNLOADED`,
  `PARTIALLY DOWNLOADED`, `NOT REQUIRED`, `NOT FOUND`, `ERROR`, `FAILED`,
  `IN PROGRESS`, `FOUND`.

## Known Gotchas
- The original per-supplier framework hardcoded Graph `client_id`/`client_secret`/
  `tenant_id` directly in source (`graph_otp.py`, `mailer.py`). This package
  fixes that: credentials always come from the caller's `config["graph"]`.
- `pyodbc`'s connection context manager commits/rolls back on exit but does
  not close the connection; this matches the team's own documented example
  (team-instructions.mdc Section 2) and is intentional, not an oversight.
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

## Change Log
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
- Cut a `v0.4.0` release with the built wheel attached, matching the
  `lib-core` release flow (see RELEASING.md).
- Once released, pin it in each supplier project's `requirements.txt`.
- Remove the unused `folder_location` parameter from `file_save_as.save()`
  at the next MAJOR version.
