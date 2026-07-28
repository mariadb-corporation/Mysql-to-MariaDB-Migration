# MySQL to MariaDB Migration

© 2026 MariaDB plc. All rights reserved.

This tool is proprietary software developed and maintained by MariaDB plc. It is provided to customers and partners for free under [MariaDB terms](https://github.com/mariadb-corporation/Mysql-to-MariaDB-Migration#license).

> **Note on naming:** `mariadb-mtk` is the data-transfer engine used by Parallel Restartable Streaming Copy (the MariaDB-packaged SQLines Data engine). It was previously distributed as `sqldata`; that name is the legacy binary name and is still auto-detected as a fallback, but `mariadb-mtk` is the current name and is used throughout this document. Its path can be set explicitly via `SQLINESDATA_BIN`.

## Purpose
Private repository to design, execute, and validate end-to-end MySQL to MariaDB migrations in a repeatable and auditable manner.

## Scope
- Schema migration
- Data migration
- User & privilege migration
- Authentication plugin compatibility
- Validation & rollback planning

## Supported Versions
- MySQL: 5.7, 8.0, 8.4
- MariaDB: Supported Enterprise and Community editions
- MariaDB Cloud as a target (validated for Offline Copy (`staged`), Parallel Restartable Streaming Copy (`two_step`), and Serial Streaming Copy (`one_step`))
- mariadb-migrator: Tested and built for linux on x86-64 and ARM64 

Mode-specific notes:
- MySQL 8.0 / 8.4 migrations are supported via any of the four modes.
- **MySQL 5.7 is supported for the three offline data-movement modes** — Serial Streaming Copy (`one_step`), Parallel Restartable Streaming Copy (`two_step`), and Offline Copy (`staged`) — validated 5.7 → MariaDB 11.8, 12.3, and 13.1. Replication (`binlog`) is **not** supported from a 5.7 source (see below). The assessment prechecks recognize 5.7: checks that inspect 8.0-only catalog objects (functional indexes, CHECK constraints, partial revokes, column-level SRIDs, resource groups, roles) detect their absence on 5.7 and report "not present" rather than erroring, so assessment completes normally. Note that 5.7 parses but does not enforce CHECK constraints; the definition is carried forward on migration.
- **Replication (`binlog`) requires a MySQL 8.0+ source, requires `binlog_format=ROW`, and does not support schemas containing JSON columns.** A pre-8.0 source (e.g. 5.7) is categorically ineligible and is blocked by a source-version gate that is evaluated before the JSON and `binlog_format` checks — a 5.7 source is refused with a clear "requires a MySQL 8.0+ source" message and routed to an offline mode. All three conditions (source version, `binlog_format=ROW`, no JSON columns) are enforced at three layers (launcher, assessment, preflight). To use Replication mode from an 8.0+ source, set `binlog_format = ROW` under `[mysqld]` in the source `my.cnf` and restart the source MySQL server.

## Migration modes at a glance

The interactive launcher presents these as a numbered menu (1–4). Internal identifiers (`one_step`, `two_step`, `staged`, `binlog`) are unchanged and remain the canonical form in `config/migration.yaml`, env vars (`MODE=...`), state files, and the orchestrator CLI (`--mode <id>`).

| # | Mode | Internal id | Type | Best for | Tooling |
|---|---|---|---|---|---|
| 1 | Serial Streaming Copy (mariadb-dump) | `one_step` | Offline | Smaller databases, standard maintenance windows | `mariadb-dump` piped to target `mariadb`; single pipe, tables transferred sequentially |
| 2 | Parallel Restartable Streaming Copy (sqldata) | `two_step` | Offline | Larger datasets needing schema-then-parallel-data | `mariadb-dump` (schema) + `mariadb-mtk` (multiple concurrent sessions per database) |
| 3 | Offline Copy (mariadb-dump) | `staged` | Offline | Source/target not network-reachable; deferred or two-host load | `mariadb-dump` → on-disk file (per-DB, compressed) → `mariadb` client |
| 4 | Replication (binlog) | `binlog` | Online | Low-downtime cutover, ongoing replication | `mariadb-dump` snapshot + MySQL binlog replication into MariaDB |

The two streaming modes differ in their transfer topology: **Serial Streaming Copy** uses a single `mariadb-dump | mariadb` pipe and migrates tables sequentially, while **Parallel Restartable Streaming Copy** uses `mariadb-mtk` (the SQLines Data engine) with multiple concurrent worker sessions per database (controlled by the `-ss` parameter).

## Top-level menu

Invoked interactively (no CLI flag), the launcher presents two choices before mode selection:

```
1) Assess & Plan    Inspect source and target, validate connectivity and
                    compatibility, and produce an assessment report and the
                    migration plan. No data is moved.
2) Assess + Run     Assess the source, then proceed to the full migration
                    (plan + run, with confirm steps between phases).
q) Quit
```

Pressing Enter selects option 2. Operators previewing a migration before committing should pick option 1 — the assess and plan phases produce artifacts under `artifacts/assess_<ts>/` and `artifacts/plan_<ts>/` respectively, and no writes happen on the target. Operators ready to migrate can pick option 2 to run the full flow with confirm steps between phases.

The same phases are reachable non-interactively via `--assess`, `--plan`, `--run` — see `./mariadb-migrator --help`. CLI flags bypass the menu.

## Prerequisites (required)
- **MariaDB must be installed and running on the target host before running the tool.** The tool verifies the target version during preflight but does not install MariaDB. 
- For Replication (`binlog`): MariaDB on the target must additionally be configured per customer requirements (replication user, binlog format, etc.).
- Python 3.9+ is required on the orchestrator host. On first run the launcher creates a project-local virtual environment (`.venv`) and installs the Python dependencies into it automatically — no manual `pip install` step is needed. On Debian/Ubuntu, install the venv module first: `sudo apt-get install -y python3-venv`.
- The `mariadb` client must be available on the orchestrator host (used for connectivity, version, and database checks). If it is missing, the launcher detects your platform and offers to install it on first run; you can also install it manually (`dnf install mariadb`, `apt-get install mariadb-client`, `zypper install mariadb-client`, or `brew install mariadb`).
- For Parallel Restartable Streaming Copy (`two_step`), `mariadb-mtk` — the MariaDB-packaged SQLines Data engine — must be installed. It is available from the [MariaDB community page](https://mariadb.com/downloads/community/). The launcher auto-detects a `mariadb-mtk` binary on `PATH` (falling back to the legacy `sqldata` name); if it is installed under a different name or location, point `SQLINESDATA_BIN` at its full path.
- Ensure network connectivity from the orchestrator host to both source MySQL and target MariaDB. (Exception: Offline Copy (`staged`) in `dump_only` or `load_only` phase only needs connectivity to one side.)
- The orchestrator can run on a third host; SSH access to the target is only required for the deprecated install path and for `replace_slave` mode.
- The tool prompts for required inputs if not provided in config/env.
- `pv` is recommended for live progress visibility but optional. When missing, Offline Copy (`staged`) falls back to a 60-second file-size probe and Serial Streaming Copy (`one_step`) falls back to a 60-second heartbeat.

## Prerequisites (user privileges)
The tool expects valid privileges to already exist for the entered users.

Admin users (`SRC_ADMIN_USER` / `TGT_ADMIN_USER`):
- Must be able to connect from the migration tools host.
- Must be able to check/create/drop target database objects as needed by workflow.
- Must be able to run dump/restore and configure replication where applicable.
- In practice, this means admin-level privileges, including grant capability.

## Prerequisites (Parallel Restartable Streaming Copy data load)
If you hit foreign key / unique constraint ordering errors during `two_step_parallel_data`, run the following on the **target MariaDB** before starting Parallel Restartable Streaming Copy (`two_step`):

```sql
SET GLOBAL FOREIGN_KEY_CHECKS=0;
SET GLOBAL UNIQUE_CHECKS=0;
```

After `two_step_finalize_objects` completes, re-enable both:

```sql
SET GLOBAL FOREIGN_KEY_CHECKS=1;
SET GLOBAL UNIQUE_CHECKS=1;
```

Notes:
- Run these statements using an account with sufficient privileges to set global variables.
- This is a manual DBA pre/post step; the scripts do not toggle these globals automatically.

## Application user migration

When the operator answers `y` to the `Migrate application users? (y/n)` prompt, the tool migrates application users and roles from the source MySQL to the target MariaDB during the run phase. From 1.2.3-beta, this runs in every user-facing mode (`one_step`, `two_step`, `staged`, `binlog`), and the assess phase produces a prediction report ahead of the run.

### What happens to each user

User creation on target is plugin-aware. The source plugin determines which path each user takes:

| Source plugin | Outcome on target |
|---|---|
| `mysql_native_password` (with hash) | Migrated with original password preserved (hash ported via MariaDB's `IDENTIFIED VIA ... USING` syntax). |
| `mysql_native_password` (no hash) | Created with the default password supplied at the prompt, with `PASSWORD EXPIRE` set so the user must change it on first login. |
| `caching_sha2_password`, `sha256_password` | Same as above — default password + `PASSWORD EXPIRE`. The hash formats are not portable across the engine boundary. |
| `auth_socket`, `unix_socket`, `auth_pam`, `mysql_no_login`, anything else | **Skipped.** Not created on target. Configure these manually after the migration if needed. |

Roles are detected separately (using the standard MySQL fingerprint of locked, expired, no authentication string) and replayed on target via `CREATE ROLE IF NOT EXISTS`. They are not migrated as users.

### Grants

After each user is created, the tool runs `SHOW GRANTS` against the source and replays each grant on the target. Grants that fail at replay time are dropped from the migration and listed in the per-user report. Common causes:

- MySQL 8.0 / 8.4 dynamic privileges that MariaDB doesn't recognize (e.g. `APPLICATION_PASSWORD_ADMIN`, `SYSTEM_USER`, `SET_ANY_DEFINER`)
- Grants to a database that doesn't exist on target
- Grants to a role that wasn't migrated (because the source user was identified as a role and skipped from the user loop)
- Permission denial when the target admin user lacks `GRANT OPTION` on the database being granted on

### Reports

Two artifacts are produced when user migration is enabled:

- **`artifacts/assess_<ts>/user_assessment_report.txt`** — written by the assess phase. Predicts which users would be preserved, defaulted, or skipped. No writes to the target are performed.
- **`artifacts/run_<mode>_<ts>/user_migration_report.txt`** — written by the run phase. Records what actually happened: roles created, users preserved, users with password reset to default, users skipped, users that failed to migrate, and the full list of grants that were dropped.

The two reports use parallel structure for direct comparison. Operators running `Assess + Run` can diff them post-migration to confirm the prediction matched the outcome.

### Default password handling

When the operator supplies a default password at the `Default password for app users:` prompt, that password is used for every user that cannot have their original password preserved (i.e. all non-native-password users and native-password users without a hash). Every such user is also marked `PASSWORD EXPIRE`, so they must change their password on first login.

The default password is recorded in plain text in `user_migration_report.txt`. Treat that file as sensitive and rotate the default after the migration completes.

### Limitations and known behavior

- Replication, monitoring, and backup-tool accounts (e.g. `repl_user`, `dbpwf*`, `pmm_*`, `xtrabackup`, `mysqld_exporter`) are migrated as application users in this release. They need to be cleaned up manually on target post-migration. A pattern-based exclusion list is planned.
- MySQL 8.4 sources default to `caching_sha2_password` and ship with `mysql_native_password` disabled. Most or all users on a fresh 8.4 source will land on the default-password path. Plan a password rotation pass before re-enabling application traffic on the target.
- The dump phase (one_step, two_step, staged) may replay user-related rows from `mysql.user` as part of the data load, which can produce duplicate or conflicting entries alongside what the user migration script created. Worth a `SELECT user, host, plugin, is_role FROM mysql.user` review on target post-migration if the user set looks off.

## Post-load optimizer statistics (ANALYZE TABLE)

After a dump/restore the target's optimizer statistics are empty or stale, which can lead to poor query plans until they are refreshed. From 1.2.6-beta, the tool can run `ANALYZE TABLE` across the migrated databases on the target as a final step, so the optimizer has accurate statistics immediately.

A prompt — `Run ANALYZE TABLE on target after load? (y/n)` — appears during setup just after the `Migrate application users?` prompt. It defaults to `y` (opt-out) and is shown only for the modes where a fresh load occurs: Serial Streaming Copy (`one_step`), Parallel Restartable Streaming Copy (`two_step`), and Offline Copy (`staged`). The step runs after the load and before final validation. It is not part of Replication (`binlog`) mode, where replication keeps changing the tables after the seed.

Behavior and controls:
- Set `ANALYZE_TARGET=0` (or answer `n`) to skip it.
- The step is skipped automatically when Offline Copy runs as `dump_only` (no load happens on that host).
- A connection failure to the target stops the run; an `ANALYZE TABLE` error on an individual table is reported but does not fail the migration.
- A report is written to `artifacts/run_<mode>_<ts>/analyze_target_report.txt` listing the tables analyzed, status counts, and any per-table errors.

## Status
Beta. All four modes have been exercised end-to-end against representative source/target pairs. Offline Copy (`staged`) has been validated against AWS RDS sources and MariaDB Cloud targets. In v1.2.0-beta, mode selection moved from a free-text prompt to a numbered interactive menu. Restartable, chunked loading is now native to `mariadb-mtk`, so the earlier experimental resumable load variant has been retired (v1.2.9-beta).

## Migration playbooks

### Serial Streaming Copy (`one_step`)
Best for smaller databases and standard maintenance windows.
- Uses `mariadb-dump` (or `mysqldump`) on source and streams to target `mariadb`.
- Single pipe, tables transferred sequentially — predictable memory and network profile, no concurrency tuning required.
- Supports single DB (`SRC_DB`) or multi-DB (`SRC_DBS="db1,db2"`).
- Strips DEFINER clauses by default to avoid permission errors on target.
- Live progress: `pv` lines every 10s in `run.log`; falls back to a 60s heartbeat when `pv` is unavailable.

### Parallel Restartable Streaming Copy (`two_step`)
Best for larger datasets or tighter windows.
- Schema-only dump first, then parallel data load via `mariadb-mtk` (with a post-load source/target row-count validation), then finalize objects (triggers, routines, events).
- `mariadb-mtk` uses multiple concurrent worker sessions per database for the data phase.
- Assumes `mariadb-mtk` is installed; the launcher auto-detects a `mariadb-mtk` binary on `PATH` (falling back to the legacy `sqldata` name), or set `SQLINESDATA_BIN` to its full path.
- Requires admin users (`SRC_ADMIN_USER`/`TGT_ADMIN_USER`); preflight fails fast if those logins are not ready.

#### Restart behavior

Restartable, chunked loading is native to `mariadb-mtk` and on by default — there is no variant to select. Large tables (by default 1,000,000+ rows, set by `large_tables_rows`; chunking requires an `AUTO_INCREMENT` column, so tables without one transfer as a single stream) are loaded as parallel chunks, and transient errors are retried automatically within a run (`restart_attempts`, default 10). Defaults and tuning live in `sqldata.cfg-example`; no extra options are needed.

`mariadb-mtk` does not resume a load across separate invocations. An interrupted or failed `two_step` run is restarted by dropping the target database(s) and re-running from a clean target — the launcher always starts a fresh run for this mode and reports a leftover target database as a conflict to drop.

#### Row-count validation

After the parallel data load completes successfully, the tool validates row counts between source and target for each migrated database, using `mariadb-mtk`'s own validate command (`-cmd=validate -vopt=rowcount`). It runs once per database (looping the same `SRC_DB`/`SRC_DBS` list used for the load).

This is a post-load **report, not a gate**. The data transfer is the gate: a failed load stops the run before validation is reached, so reaching validation means every database transferred successfully. A row-count mismatch is recorded and surfaced but does **not** fail the migration.

Each database's per-table comparison and `Equal/Different tables` summary are appended to **both** the per-database load log (`<run>/sqldata/<db>/sqldata.log`) and the run log (`<run>/run.log`), under a `===== row-count validation: <db> =====` header. A concise `row counts OK` / `ROW-COUNT MISMATCH` verdict per database is also echoed to the console.

The check runs with `mariadb-mtk`'s configured session count (the `-ss` default from `sqldata.cfg`), matching the data load. Set `MIGRATOR_SKIP_ROWCOUNT_VALIDATE=1` to skip it entirely.

### Replication (`binlog`)
Best for low-downtime cutover.
- Requires a **MySQL 8.0+ source**. A pre-8.0 source (e.g. 5.7) is blocked upfront by a source-version gate — replication into MariaDB is not reliable from these versions, and the block is categorical (independent of schema). Use one of the offline modes for a 5.7 source.
- Seeds target from a consistent dump snapshot with embedded binlog coordinates.
- Starts MariaDB replication from MySQL binlog using `REPL_USER`/`REPL_PASS`.
- Verifies replication thread health and lag after start.
- For MySQL 8.4 sources, an upstream `mysqldump` ≥ 8.4 must be available (8.4 servers reject `SHOW MASTER STATUS`); the tool detects this and fails fast at preflight.
- **JSON column caveat**: replication is not safe for schemas containing JSON columns when the source uses `binlog_format=MIXED` binlog_format=ROW check is enforced. See Supported Versions for the full explanation. If the source is JSON-bearing, choose one of the offline modes.

### Offline Copy (`staged`)
Best when source and target are not directly network-reachable, or when a checkpoint between dump and load is desirable.
- Phase-driven via `STAGED_PHASE`:
  - `dump_and_load` (default): full end-to-end from source to target
  - `dump_only`: dump from source to a file directory, then exit (target untouched)
  - `load_only`: load existing dumps into target (no source connection needed; DB list comes from the manifest)
- Per-database compressed dumps (`<db>.sql.gz`) under `${RUN_DIR}/dumps/` by default, or a custom location set via `STAGED_DUMP_DIR`.
- Manifest (`manifest.txt`) tracks per-DB SHA-256, byte size, and approximate row count.
- Per-file checksum verification at load time (default on, `STAGED_VERIFY_CHECKSUM=1`). Internally a SHA-256 hash; named "checksum" in customer-facing output to avoid confusion with MariaDB authentication plugins like `caching_sha2_password`.
- Configurable per-DB parallelism (`STAGED_PARALLEL=4` default for dumps; sequential loads).
- Post-load finalize step compares manifest vs. target `information_schema.tables`; hard-fails on missing/empty databases, soft-warns on row-count variance above `STAGED_FINALIZE_VARIANCE_PCT` (default 50%). Both source and target row counts are InnoDB sampled estimates; small variance is expected and does not indicate data loss.
- Live progress: `pv` lines every 10s when available; otherwise a 60s file-size probe with bytes/elapsed/rate/percent.
- Phase-aware completion banners: "DUMP COMPLETE" / "LOAD COMPLETE" / "MIGRATION SUCCESSFUL".
- Auto-detects most recent `artifacts/run_staged_*/dumps/` as the `load_only` default — re-runs are one Enter press.
- **Caveat**: offline mode. Writes to source during dump are not captured. Use Replication (`binlog`) if downtime is unacceptable.
- **Caveat**: target connections currently negotiate TLS where the server requires it (e.g. MariaDB Cloud), but server-certificate verification is not yet configurable on the target side. Connections are encrypted in transit but not authenticated against a trusted CA. Source-side TLS verification works as expected via `SRC_SSL_MODE`. Configurable target TLS is planned for a future release.

## Installation and first run

The MySQL to MariaDB Migrator is distributed as a release archive (`.tar.gz` or `.zip`) from the [MariaDB community downloads page](https://mariadb.com/downloads/community/) or [MariaDB Enterprise GIT](https://github.com/mariadb-corporation/Mysql-to-MariaDB-Migration/releases). Download the latest version (e.g. `v1.3.2-beta`), extract it, and run the launcher — it bootstraps its own Python environment, so there is no manual setup beyond the prerequisites above.

```bash
# Download the release archive from https://github.com/mariadb-corporation/Mysql-to-MariaDB-Migration/releases
# Extract and run (replace <version> with the release you downloaded, e.g. v1.3.2-beta):
tar -xzf Mysql-to-MariaDB-Migration-<version>.tar.gz
cd Mysql-to-MariaDB-Migration-<version>
./mariadb-migrator
```

The `.zip` archive is equivalent (`unzip Mysql-to-MariaDB-Migration-<version>.zip`, then `cd` into the extracted directory). The version embedded in the archive and directory names matches the release you download.

The data-transfer engine used by Parallel Restartable Streaming Copy — **`mariadb-mtk`** (the MariaDB-packaged SQLines Data engine) — is available from the same downloads page. Put it on `PATH` as `mariadb-mtk` (the legacy `sqldata` name is also detected) or point `SQLINESDATA_BIN` at its full path (see [Prerequisites](#prerequisites-required) above).

On first run the launcher will:

1. Create a project-local virtual environment at `./.venv` (unless one is already active) and install the Python dependencies (`typer`, `click`, `rich`, `PyYAML`) into it. Your system Python is never modified.
2. Detect the `mariadb` client and, if it is missing, show the correct install command for your platform and offer to run it. `pv` (optional) is offered the same way, without blocking the run.
3. Present the interactive menu.

Subsequent runs reuse `./.venv` and go straight to the menu. `.venv` is git-ignored and must not be committed or included in a release archive — it is recreated automatically.

First-run prompts can be controlled for unattended or CI hosts:

- `MIGRATOR_ASSUME_YES=1` — accept install prompts automatically.
- `MIGRATOR_NO_SYSTEM_INSTALL=1` — never run system installs; print the commands only.
- `MIGRATOR_NO_AUTO_VENV=1` — do not auto-create `.venv`; print manual venv steps and exit.

## Orchestrator usage

Interactive (recommended):
```bash
./mariadb-migrator
```

The launcher presents a numbered menu for mode selection (1–4):

```
Select a migration mode:

  1) Serial Streaming Copy (mariadb-dump)   [OFFLINE]
  2) Parallel Restartable Streaming Copy (sqldata)      [OFFLINE]
  3) Offline Copy (mariadb-dump)            [OFFLINE]
  4) Replication (binlog)                   [ONLINE]

  d) Show detailed descriptions (caveats, disk requirements, version notes)
  q) Quit
```

Sub-menus then prompt for the `staged` phase (dump_and_load / dump_only / load_only) when applicable. From any sub-menu, `b` returns to the top-level mode menu without restarting.

For scripted runs and the regression matrix harness, preset env values skip the corresponding prompts: `MODE`, `STAGED_PHASE`, `STAGED_DUMP_DIR`, and `STAGED_CONFIRM_OFFLINE` are all respected. This is also how the hidden modes (`inplace`, `replace_slave`) are reached — they are intentionally not in the numbered menu.

Non-interactive CLI:

The launcher creates and uses `.venv` automatically. To call the orchestrator CLI directly, activate that environment first (run the launcher once to create it, or create it yourself):

```bash
source .venv/bin/activate    # created on the first ./mariadb-migrator run
python -m orchestrator.migrationctl plan --config config/migration.yaml --mode one_step --out artifacts/plan
python -m orchestrator.migrationctl run  --config config/migration.yaml --mode one_step --out artifacts/run
```

To manage the environment yourself instead of letting the launcher bootstrap it:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r orchestrator/requirements.txt
```

Plan:
```bash
python3 -m orchestrator.migrationctl plan --config config/migration.yaml --mode one_step --out artifacts/plan
```

Run:
```bash
python3 -m orchestrator.migrationctl run --config config/migration.yaml --mode one_step --out artifacts/run
```

Resume:
```bash
python3 -m orchestrator.migrationctl resume --config config/migration.yaml --mode one_step --out artifacts/run
```

### Watching live progress

The orchestrator captures all script output to `run.log` rather than streaming it to your terminal. Each phase prints a `tail -f` hint you can paste into another shell:

```
==> Running: staged (out: artifacts/run_staged_20260507_154955)
    Live progress: tail -f artifacts/run_staged_20260507_154955/run.log
```

Notes:
- `./mariadb-migrator` runs assess → plan → run, and resumes a failed run automatically for `one_step`, `binlog`, `replace_slave`, and `staged`. `two_step` does not resume — it always starts a fresh run, since `mariadb-mtk` has no cross-invocation resume (drop the target and re-run to restart).
- To force a fresh run instead of resuming, set `FORCE_NEW_RUN=1`. The launcher prints a clear banner distinguishing "FORCE_NEW_RUN=1 set; ignoring previous run" from "inputs changed since last run".
- `./mariadb-migrator` asks for source/target admin credentials at runtime; root is blocked by default unless `ALLOW_ROOT_USERS=1`.
- Saving `config/migration.yaml` is optional and defaults to `No`; if saved, passwords are redacted by default.

## Environment variables

For a complete reference of all variables consumed by the tool — grouped by section (source, target, mode-specific) with defaults and descriptions — see [`docs/environment-variables.md`](docs/environment-variables.md).

The sections below list the variables **required** to run each mode. Defaults and optional tuning variables are in the reference doc.

## One-step required envs (config/migration.yaml)
Source:
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` or `SRC_DBS` (comma-separated)
- Optional: `SRC_SSL_MODE` (DISABLED|PREFERRED|REQUIRED|VERIFY_CA|VERIFY_IDENTITY) for TLS-required sources

Target:
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`

Optional (applies to one_step, two_step, and staged):
- `ANALYZE_TARGET` (`1` default — run `ANALYZE TABLE` on the target after load to refresh optimizer statistics; set `0` to skip)

## Two-step required envs (config/migration.yaml)
Source:
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` (single DB) or `SRC_DBS` (comma-separated, looped one by one)

Target:
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`

Optional:
- `SQLINESDATA_BIN` (full path to the `mariadb-mtk` binary; auto-detected from a `mariadb-mtk` on `PATH`, falling back to the legacy `sqldata` name)
- `MIGRATOR_SKIP_ROWCOUNT_VALIDATE` (set to `1` to skip the post-load source/target row-count validation; see the Parallel Restartable Streaming Copy playbook)

## Binlog required envs (config/migration.yaml)
Source:
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` (single DB) or `SRC_DBS` (comma-separated)

Target:
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`

Replication:
- `REPL_USER`, `REPL_PASS`
- Optional: `SRC_BINLOG_FILE`, `SRC_BINLOG_POS` (auto-captured during seed if not set)
- Optional: `BINLOG_COORD_FILE` (default: `artifacts/binlog_coords.env`)
- Optional: `BINLOG_MAX_LAG_SECS` (default: `30`)

## Staged required envs (config/migration.yaml)
Phase selector:
- `STAGED_PHASE` (`dump_and_load` | `dump_only` | `load_only`, default `dump_and_load`)

Source (required for `dump_and_load` and `dump_only`):
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` or `SRC_DBS` (comma-separated)
- Optional: `SRC_SSL_MODE` for TLS-required sources

Target (required for `dump_and_load` and `load_only`):
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`

Dump configuration (optional):
- `STAGED_DUMP_DIR` (default `${RUN_DIR}/dumps`; required for `load_only` to point at the manifest directory)
- `STAGED_COMPRESS` (`1` default — gzip-compress the dump)
- `STAGED_PV` (`1` default — show progress meter via `pv` or fallback probe)
- `STAGED_PARALLEL` (`4` default — concurrent per-DB dumps)
- `STAGED_LOAD_PARALLEL` (`1` default — concurrent per-DB loads)
- `STAGED_VERIFY_CHECKSUM` (`1` default — file-integrity checksum each dump file before load; alias: `STAGED_VERIFY_SHA256`, deprecated)
- `STAGED_DISK_HEADROOM_FACTOR` (`2` default — multiplier on source data size for the dump-volume free-space check)
- `STAGED_FINALIZE_VARIANCE_PCT` (`50` default — row-count variance threshold above which finalize emits a warning; alias: `STAGED_FINALIZE_DRIFT_PCT`, deprecated)
- `STAGED_CONFIRM_OFFLINE` (set to bypass the interactive offline-acknowledgment prompt for non-interactive runs)

Example: dump now, load later on a different host

```bash
# On source host:
STAGED_PHASE=dump_only STAGED_DUMP_DIR=/mnt/transfer/dumps ./mariadb-migrator

# Transfer the directory:
scp -r /mnt/transfer/dumps target-host:/mnt/load/

# On target host:
STAGED_PHASE=load_only STAGED_DUMP_DIR=/mnt/load/dumps ./mariadb-migrator
```

## Multi-DB example
```yaml
SRC_DBS: "sakila,world"
```

## Testing

A phase-equivalence test exists for Offline Copy (`staged`) under `tests/test_staged_phase_matrix.sh`. It runs `dump_and_load` once and `dump_only` + `load_only` separately against the same source, then compares target signatures (schema DDL via `mariadb-dump --no-data`, per-table `CHECKSUM TABLE`) to verify both paths produce identical targets.

```bash
SRC_HOST=... SRC_USER=... SRC_PASS=... \
TGT_HOST=... TGT_USER=... TGT_PASS=... \
TEST_DB=sakila \
tests/test_staged_phase_matrix.sh
```

## Notes
- Use a fresh `--out` directory per run to avoid step skips. The interactive launcher (`./mariadb-migrator`) handles this automatically.
- Orchestrator mode: `python -m orchestrator.migrationctl plan/run --config config/migration.yaml --mode <one_step|two_step|binlog|staged> --out artifacts/<dir>`
- Safety default: migration fails if target DB already exists. Set `ALLOW_TARGET_DB_OVERWRITE=1` only when overwrite is intentional. Note that `staged` `load_only` derives its DB list from the manifest, so the same overwrite check applies before load begins.
- Live progress: every long-running phase prints a `tail -f run.log` hint. Open another terminal and paste the command to watch dump/load progress in real time.
- Platform coverage note: this tool has been tested primarily on Ubuntu and Rocky Linux. Support hooks are included for additional Linux flavors, but validate in your target environment before production use.

## Known limitations
- **Replication (`binlog`) is not supported from a MySQL 5.7 (pre-8.0) source.** A source-version gate blocks this upfront at all three layers (launcher, assessment, preflight), evaluated ahead of the JSON and `binlog_format` checks, and routes the operator to an offline mode. The three offline modes are fully supported from 5.7. See Supported Versions.
- **Replication (`binlog`) does not support schemas containing JSON columns** when the source uses `binlog_format=MIXED` (the MySQL default since 8.0). This is detected and blocked upfront at three layers (launcher, assessment, preflight) as of v1.2.1-beta — the operator is refused with a clear message and routed to an offline mode rather than hitting a runtime replication failure. See Supported Versions for the full explanation.
- MySQL 5.7 CHECK constraints are parsed but not enforced by the 5.7 server. On migration to MariaDB (which does enforce them) the constraint definition is carried forward; there is no pre-existing enforced state on the 5.7 source to preserve. Validate constraint-sensitive data after migration if the source relied on application-level enforcement.
- Offline Copy (`staged`) per-DB load resume is not supported in v1. If a load fails partway through a multi-DB run, drop the partially-loaded databases on the target and re-run with `STAGED_PHASE=load_only`.
- `pv` fallback in Serial Streaming Copy (`one_step`) is a heartbeat only (no byte counts), because the data path is a network pipe with no on-disk file to probe. The full file-size probe is available in Offline Copy (`staged`) where the dump is on disk.
- Target-side TLS verification is not yet configurable. Connections to TLS-required targets (e.g. MariaDB Cloud) are encrypted but not server-verified. Source-side TLS works as expected via `SRC_SSL_MODE`.
- Client-tool noise (the `mysql: Deprecated program name` banner and the `--ssl-verify-server-cert is disabled` warning) has been removed from the Serial Streaming Copy (`one_step`) run path as of 1.2.6-beta, except for one source-side check in its preflight. The other modes' preflight and phase scripts still emit these cosmetic lines; full cleanup is planned for a later release. They are presentation-only and do not affect migration correctness.

## Feedback

Join the [MariaDB Community on Slack](https://mariadb.com/docs/general-resources/community/joining-the-community) to share your feedback. Once you're in, the [#migration channel](https://mariadb-community.slack.com/archives/C0BEPVC8PC6) is the best place for questions and feedback about the MySQL to MariaDB Migrator.


## License

[MariaDB Software License Terms](https://legal.mariadb.com/agreements/enterprise/MariaDB_Software_License_Terms_2026-05-15.pdf) apply to all MariaDB Software unless otherwise stated. They do not alter the license terms of any free and open-source software (FOSS) or software subject to the Business Source License (BSL) (see Section 7 of the MariaDB Software License Terms).

For additional legal information, see [MariaDB Terms](https://mariadb.com/terms/).
