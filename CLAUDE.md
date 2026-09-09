# CLAUDE.md

## Purpose
Shared ODC (Online Data Collection) infrastructure library. Provides job claiming,
file allocation/save, job-status updates, Microsoft Graph mail/OTP helpers,
browser-automation pacing helpers, and a generalised human-assisted-login
mechanism (`human_in_loop.py`, 0.8.1) used by every ODC supplier bot project
(`automation-odc-wave`, `automation-odc-british-gas`, `automation-odc-energia`,
and future suppliers). This is a library, not a Control Room bot - it has no
entry point of its own.

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
  `ODC_scrape_accounts`, `ODC_suppliers`, `ODC_multi_credentials`,
  `ODC_credentials`, plus `jupiter.aa_dev.web_scrape_data` /
  `jupiter.aa.web_scrape_data` (an older parallel pipeline consulted only
  for `client_name == "inspired plc"` duplicate checks), and the stored
  procedure `spODC_job_details_UpdateStatus`.
  `ODC_jobs` also carries five columns added in 0.8.0/0.8.1 -
  `human_wait_status`/`human_wait_deadline` (0.8.0) and
  `rdp_host`/`rdp_username`/`rdp_password` (0.8.1, plaintext RDS
  connection details for the human-assisted RDP session) - written only by
  `jobstodo.set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()`;
  see spec §4.2. **Confirmed applied to both `Titan_INSE_DEV` and the live
  `Titan_INSE`** (queried directly 2026-09-09 via `INFORMATION_SCHEMA.COLUMNS`).
  One discrepancy from what this repo's DDL specifies:
  `human_wait_status` was actually created as `VARCHAR(50)`, not
  `NVARCHAR(30)` - functionally fine for the short ASCII status values this
  library writes, just noting the drift; the three `rdp_*` columns and
  `human_wait_deadline` match exactly.
  A sixth column, `human_wait_started_at`, was part of the original 0.8.0
  design but dropped before any DDL request went out - nothing reads it (and
  it does not exist on `Titan_INSE_DEV` either), so only `human_wait_deadline`
  is kept.
- `ODC_suppliers` (pre-existing, not owned by this library) carries
  `human_in_loop` (nullable `tinyint`) - a **per-supplier**, not per-job,
  flag for "does this supplier's login require a human". Confirmed on both
  `Titan_INSE_DEV` and the live `Titan_INSE` 2026-09-09: only the `Energia`
  row has it set to `1`; every other supplier row is `NULL`.
  `jobstodo.get_job_details()` (0.8.1)
  left-joins this table on `ODC_jobs.supplier_id` and returns
  `human_in_loop` on every row - see spec §4.3 and the new `human_in_loop`
  module below.
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
- Human-assisted login (0.8.1, poll-trigger fix in 0.8.2): a supplier checks
  `human_in_loop` on any row `jobstodo.get_job_details()` returns
  (left-joined from `ODC_suppliers`, §4.3) and, if truthy, calls
  `human_in_loop.wait_for_human_login()` instead of driving its own login.
  That one call owns the entire lifecycle - the on-page banner and confirm
  button, the poll loop, and the
  `jobstodo.set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()`
  sequencing - so a supplier project only has to supply `is_logged_in(page)`,
  its own portal-specific "did the login succeed" check. **`is_logged_in()`
  does not end the wait on its own** (fixed in 0.8.2 - see Change Log): only
  the injected confirm button does; `is_logged_in()` is logged at each
  heartbeat purely as a diagnostic. Generalised out of
  `automation-odc-energia`'s Phase 3, the only current `human_in_loop=1`
  supplier.

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
- **Breaking for every existing caller, not just human-wait/RDP suppliers**:
  `jobstodo._SELECT_JOB_DETAILS`'s new join to `ODC_suppliers` (0.8.1, for
  `human_in_loop`) needs `tables["suppliers"]`. Unlike the `scrape_accounts`
  join above, this one is a LEFT JOIN deliberately - a job row with no
  matching supplier row still comes back (with `human_in_loop` as `None`)
  rather than being silently dropped, so it does not repeat that gotcha. But
  every supplier project's `config.yaml` still needs a new `tables.suppliers:
  ODC_suppliers` key before upgrading, or `get_job_details()` raises
  `KeyError: 'suppliers'` immediately - see Outstanding TODOs.
- `REQUIRES RETRY` and `MISSING PARENT` are validated by `VALID_STATUSES` and
  written to the database, but nothing in the current estate resets a row in
  either status back to `pending`. `jobstodo.get_job_details()` only
  re-fetches `pending` rows for a single-credential job, so a supplier using
  these two statuses to mean "try again on a later run" needs its own
  reset mechanism (or to run as a multi-credential job, whose status filter
  does not exclude them); today no such mechanism exists.
- `jobstodo.set_human_wait()` (0.8.1) writes `rdp_host`/`rdp_username`/
  `rdp_password` to `ODC_jobs` in plaintext - consistent with how portal
  credentials are already stored in `ODC_credentials`/`ODC_job_details`, but
  an RDS machine login is a higher-privilege credential than a single
  supplier portal login. `set_human_wait_complete()` nulls the three RDP
  columns as soon as a wait concludes successfully, and `clear_human_wait()`
  nulls everything again on every exit path regardless - but a caller that
  skips both (crash before the `finally`, process killed) leaves a live RDP
  credential sitting in Titan until someone notices and clears it manually -
  there is no separate expiry/sweep job today.
- Detecting that a human has actually finished a human-assisted login is
  deliberately outside `jobstodo`'s scope - `set_human_wait_complete()` only
  records that it happened, it never detects it itself. As of 0.8.1 this
  detection is the one thing `human_in_loop.wait_for_human_login()` still
  cannot generalise: the caller supplies `is_logged_in(page)`, a
  portal-specific check (e.g. `automation-odc-energia`'s
  `_post_login_reached()`, a post-login URL marker) - but per 0.8.2, this
  callable is diagnostic-only (logged at each heartbeat) and cannot end the
  wait on its own; only the injected confirm button can. 0.8.1 originally
  let either one trigger success, and live testing against
  `automation-odc-energia` immediately hit the exact failure mode this
  entry used to warn about: the "taking over" banner fired before the human
  had clicked anything, because a URL-based marker read true on an
  intermediate page mid-login. `is_logged_in` no longer needs to be
  airtight against false positives for this reason, but it must still be
  defensive against exceptions - one propagates out of
  `wait_for_human_login()` and ends the wait early (see that function's
  docstring).

## Change Log
- 2026-09-09: v0.8.2 - fixed `human_in_loop.wait_for_human_login()`'s poll
  loop treating `is_logged_in(page)` as an equal, independent trigger
  alongside the confirm button. v0.8.1 shipped with `if button_confirmed or
  is_logged_in(page): return True`; live testing against
  `automation-odc-energia` (job 145 on `Titan_INSE_DEV`) hit this within
  minutes of the release going out - the "Automation taking over" banner
  fired, and the bot began resuming control, before the human had clicked
  anything, because a URL-based post-login marker read `True` on an
  intermediate page mid-login. Only the button click ends the wait now;
  `is_logged_in()` is still called every tick and included in the heartbeat
  log, but purely as a diagnostic. `v0.8.1` was already tagged, pushed, and
  published as a GitHub Release with its wheel attached before this was
  found - per the Hard Rules in `RELEASING.md` ("never edit, move, or
  force-push a tag"), this is a new release, not an amendment to 0.8.1.
- 2026-09-09: v0.8.1 (cont.) - generalised `automation-odc-energia`'s Phase 3
  human-assisted-login mechanism into two new pieces, so any supplier can use
  it instead of only Energia:
  - New `human_in_loop` module: `wait_for_human_login(page, is_logged_in,
    job_id, timeout_s, rdp_host, rdp_username, rdp_password, tables, dsn,
    config) -> bool`. Owns the on-page banner, an injected "I'm logged in"
    confirm button (a plain JS flag read via `page.evaluate()`, not
    `page.expose_function()`), the poll loop, and the full
    `jobstodo.set_human_wait()`/`set_human_wait_complete()`/`clear_human_wait()`
    sequencing - a caller supplies only `is_logged_in(page)`, the one
    genuinely portal-specific piece. See spec §3.2a.
  - `jobstodo.get_job_details()` now left-joins the pre-existing
    `ODC_suppliers` table on `supplier_id` and returns its `human_in_loop`
    column (nullable `tinyint`) on every row, so a caller can decide whether
    to call `human_in_loop.wait_for_human_login()` without a separate query.
    **Breaking for every existing caller**, not only human-wait/RDP
    suppliers: `tables` now requires a `suppliers` key. See spec §4.3 and
    the new Known Gotchas entry - unlike the `scrape_accounts` join, this
    one is a LEFT JOIN, so a job with no matching supplier row still comes
    back rather than being silently excluded.
  - Confirmed while building this, by querying `INFORMATION_SCHEMA.COLUMNS`
    directly: the 0.8.0/0.8.1 `human_wait_*`/`rdp_*` DDL (§4.2) is already
    applied to **both** `Titan_INSE_DEV` and the live `Titan_INSE` - the
    "not yet confirmed applied" language throughout the spec/Outstanding
    TODOs predates this and is now stale, cleaned up below. One drift from
    what this repo's DDL specifies: `human_wait_status` was created as
    `VARCHAR(50)`, not `NVARCHAR(30)` - harmless for the short values
    written here. Also confirmed `ODC_scrape_accounts.sug_internal_id`
    (0.7.0) on both databases, resolving that separate Outstanding TODO too.
- 2026-09-09: v0.8.1 - three changes to the 0.8.0 human-wait signal, none
  released yet so all folded into one version:
  - `jobstodo.set_human_wait()` gains three new **required** parameters,
    `rdp_host`/`rdp_username`/`rdp_password` (inserted between `timeout_s`
    and `tables` - a breaking signature change from 0.8.0), written to three
    more nullable `ODC_jobs` columns of the same name. The human-assisted-login
    signal from 0.8.0 needed these to be usable at all: the login session it
    signals for is itself an RDP session onto a per-job RDS machine, and the
    polling toolkit had no way to learn which machine or login to use.
    Stored in plaintext, matching the existing `ODC_credentials`/
    `ODC_job_details` pattern - see the new Known Gotchas entry about that
    being a higher-privilege credential than a portal login.
  - `human_wait_status`'s vocabulary changes from a single `WAITING_FOR_HUMAN`
    sentinel to a three-state lifecycle: `NULL` -> `jobstodo.HUMAN_WAIT_PENDING`
    (`"PENDING_HUMAN"`, written by `set_human_wait()`) -> new function
    `jobstodo.set_human_wait_complete()`'s `jobstodo.HUMAN_WAIT_COMPLETE`
    (`"COMPLETE"`, which also nulls the three RDP columns - the cue for a
    poller's toolkit to disconnect the RDP session) -> `NULL` (`clear_human_wait()`,
    called on every exit path regardless of whether `set_human_wait_complete()`
    was reached). Detecting that the human actually finished logging in stays
    entirely outside this library - see the new Known Gotchas entry.
  - Drops `human_wait_started_at` (unused - nothing ever read it; only the
    deadline matters for the timeout decision) before any DDL request went
    out for it.
  - All folded into one combined, still-not-yet-applied `ALTER TABLE` with
    0.8.0's other two columns (see spec §4.2 and Outstanding TODOs), since
    nothing had shipped to a real database yet. No consumer needs a
    compatibility shim - v0.8.0's wheel was never cut - but
    `automation-odc-energia` Phase 3's call site and its own
    `docs/energia-human-assisted-login-control-room-contract.md` (which
    still documents the old `WAITING_FOR_HUMAN`-only, two-state design) need
    updating before it can take this version.
- 2026-09-07: v0.8.0 - added `jobstodo.set_human_wait()`/`clear_human_wait()`,
  a job-level "waiting for a human-assisted login" signal on three new
  nullable `ODC_jobs` columns (`human_wait_status`, `human_wait_started_at`,
  `human_wait_deadline`), requested as an additive Titan schema change - not
  yet confirmed applied to `Titan_INSE`/`Titan_INSE_DEV` (see Outstanding
  TODOs). Distinct from the `ODC_job_details.status` vocabulary: that's
  per-account and only meaningful once a supplier's `search()` starts, while
  this wait happens once per job, before any account is individually
  processed. First consumer: `automation-odc-energia` Phase 3 (noVNC
  human-assisted login for a reCAPTCHA-gated portal), whose own bot
  orchestrator ("the Control Room") is expected to poll these columns
  directly - not through this library - to know when to surface a session.
  See spec §4.2.
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
- Confirm the root cause behind `human_wait_status` reading `NULL` for job
  145 (`Titan_INSE_DEV`, client `BoxFIsh`, supplier `Energia`) during live
  testing of v0.8.1 - `process_id` was set (987654) but every
  `human_wait_*`/`rdp_*` column was `NULL`. Inferred, not yet confirmed
  against `automation-odc-energia`'s actual run logs: `main.py` still calls
  the pre-0.8.1 4-arg `jobstodo.set_human_wait(job_id, timeout_s, tables,
  dsn)`, which raises `TypeError` against the installed 0.8.1 (7-arg)
  signature - caught, logged, and swallowed by `main.py`'s existing broad
  `except Exception`, so the signal silently never gets written. If
  confirmed, resolved the same way as the migration item below.
- **Every supplier project's `config.yaml` needs a new `tables.suppliers:
  ODC_suppliers` key before upgrading to v0.8.2** - breaking for every
  caller of `jobstodo.get_job_details()`, not only human-wait/RDP suppliers
  (see the new Known Gotchas entry). At least `automation-odc-wave`,
  `automation-odc-british-gas`, `automation-odc-crown-gas-and-power-ltd`,
  `automation-odc-totalenergies-gas-power-ltd`,
  `automation-odc-castle-water-ltd`, `automation-odc-source-for-business`,
  and `automation-odc-energia` all call it today.
- Retroactively confirm the plaintext storage of
  `rdp_host`/`rdp_username`/`rdp_password` on `ODC_jobs` (already applied to
  both `Titan_INSE_DEV` and `Titan_INSE` - see Databases above) is
  acceptable to whoever owns Titan security/compliance - it is a
  higher-privilege credential (RDP access to a machine) than the portal
  logins already stored in `ODC_credentials`, even though it follows the
  same plaintext pattern.
- Cut a `v0.8.2` release with the built wheel attached, following
  `RELEASING.md` - supersedes `v0.8.1`, which has the premature-takeover bug
  described in the Change Log above. `v0.8.1` cannot be edited or deleted
  per the Hard Rules, so anyone who already pulled it needs to move to
  `v0.8.2` directly rather than expecting a fix in place.
- Migrate `automation-odc-energia` Phase 3 to v0.8.2, ideally by adopting
  the new `human_in_loop.wait_for_human_login()` wholesale instead of
  keeping its own local banner/button/poll-loop implementation (which this
  version generalised out of that exact code) - no compatibility shim
  exists since 0.8.0 was never released. This is also the fix for both
  outstanding bugs above: the button-only trigger and the correct
  `set_human_wait()` signature both come for free by switching to it.
  - Its own `_show_banner()`/`_inject_login_confirm_button()`/
    `_login_confirmed_by_button()`/`_clear_login_confirm_button()`/
    `wait_for_human_login()` in `energia_supplier.py` become redundant;
    `main.py` would call `human_in_loop.wait_for_human_login()` directly,
    passing `_post_login_reached()` as `is_logged_in`.
  - Either way, `main.py`'s existing `set_human_wait()` call site needs the
    new signature (`rdp_host`/`rdp_username`/`rdp_password` inserted between
    `timeout_s` and `tables`), and needs `tables["suppliers"]` per the
    bullet above.
  - `docs/energia-human-assisted-login-control-room-contract.md` documents
    the old two-state (`WAITING_FOR_HUMAN`/`NULL`) design and the old
    `set_human_wait(job_id, timeout_s, tables, dsn)` signature throughout -
    needs a rewrite for the new `PENDING_HUMAN`/`COMPLETE`/`NULL` lifecycle,
    the `rdp_*` columns, and the RDP-based join mechanism (the contract as
    written still describes noVNC as the transport, not RDP).
- Cut a `v0.7.0` release with the built wheel attached, following
  `RELEASING.md`. Any supplier project whose jobs can carry
  `client_name == "inspired plc"` rows (at least `automation-odc-wave` and
  `automation-odc-british-gas` call `file_allocation.allocate()` today) needs
  to pin `v0.7.0` and add a `sugar:` block to its own `config.yaml`, and pass
  the new `sug_internal_id` value (now returned by `jobstodo.get_job_details()`)
  through to `allocate()`, before the SugarCRM-based folder naming actually
  takes effect for them - until then they keep the old `customer_name`
  fallback behaviour.
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
