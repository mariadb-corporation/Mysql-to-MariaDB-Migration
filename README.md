# MySQL to MariaDB Migration

© 2026 MariaDB plc. All rights reserved.

This tool is proprietary software developed and maintained by MariaDB plc. It is provided to customers and partners under approved usage terms.

## Purpose
Private repository to design, execute, and validate end-to-end MySQL to MariaDB migrations in a repeatable and auditable manner.

## Scope
- Schema migration
- Data migration
- User & privilege migration
- Authentication plugin compatibility
- Validation & rollback planning

## Supported Versions
- MySQL: 8.0, 8.4 (mode-dependent)
- MariaDB: 11.x (LTS)
- MariaDB Cloud as a target (validated for `staged`,`two_step` and `one_step`)

Mode-specific notes:
- MySQL 8.0 / 8.4 migrations are supported via `one_step`, `two_step`, `binlog`, or `staged`.
- Sources behind TLS-required endpoints (e.g. AWS RDS, Aurora) are supported via `SRC_SSL_MODE` for `one_step` and `staged`.

## Migration modes at a glance

| Mode | Type | Best for | Tooling |
|---|---|---|---|
| `one_step` | Offline | Smaller databases, standard maintenance windows | `mariadb-dump` piped to target `mariadb` |
| `two_step` | Offline | Larger datasets needing schema-then-parallel-data | `mariadb-dump` (schema) + SQLines Data (parallel data) |
| `binlog` | Online | Low-downtime cutover, ongoing replication | `mariadb-dump` snapshot + MySQL binlog replication into MariaDB |
| `staged` | Offline | Source/target not network-reachable; deferred or two-host load | `mariadb-dump` → on-disk file (per-DB, compressed) → `mariadb` client |

## Prerequisites (required)
- For `binlog`: MariaDB must be pre-installed on the target and configured per customer requirements.
- For `one_step`, `two_step`, and `staged`: the tool can install MariaDB using OS + version inputs when enabled.
- Python 3 is required on the orchestrator host to run the migration orchestrator/CLI workflow.
- For `two_step`, SQLines Data (`sqldata`/`sqlinesdata`) must be pre-installed and available on `PATH` (or set via `SQLINESDATA_BIN`).
- SQLines Data may provide a temporary/default license for evaluation; use a proper production license before production migration runs.
- Ensure network connectivity from the orchestrator host to both source MySQL and target MariaDB. (Exception: `staged` mode in `dump_only` or `load_only` phase only needs connectivity to one side.)
- The orchestrator can run on a third host; SSH access to the target is required for validation.
- The tool prompts for required inputs if not provided in config/env.
- `pv` is recommended for live progress visibility but optional. When missing, `staged` falls back to a 60-second file-size probe and `one_step` falls back to a 60-second heartbeat.

## Prerequisites (user privileges)
The tool expects valid privileges to already exist for the entered users.

Admin users (`SRC_ADMIN_USER` / `TGT_ADMIN_USER`):
- Must be able to connect from the migration tools host.
- Must be able to check/create/drop target database objects as needed by workflow.
- Must be able to run dump/restore and configure replication where applicable.
- In practice, this means admin-level privileges, including grant capability.

For TLS-required sources (RDS, Aurora):

```bash
MYSQL_PWD='***' mysql --protocol=TCP -h<SRC_HOST> -P<SRC_PORT> -u<SRC_ADMIN_USER> \
  --ssl-mode=VERIFY_IDENTITY --ssl-ca=/path/to/ca-bundle.pem -e "SELECT 1;"
```

Optional grant inspection:

```sql
SHOW GRANTS FOR 'admin'@'<orchestrator_ip_or_%>';
```

## Prerequisites (two_step data load)
If you hit foreign key / unique constraint ordering errors during `two_step_parallel_data`, run the following on the **target MariaDB** before starting `two_step`:

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

## Status
Beta. All six modes have been exercised end-to-end against representative source/target pairs. `staged` mode is the most recent addition and has been validated against AWS RDS sources and MariaDB Cloud targets.

## Migration playbooks

### One-step (dump/restore)
Best for smaller databases and standard maintenance windows.
- Uses `mariadb-dump` (or `mysqldump`) on source and streams to target `mariadb`.
- Supports single DB (`SRC_DB`) or multi-DB (`SRC_DBS="db1,db2"`).
- Strips DEFINER clauses by default to avoid permission errors on target.
- Live progress: `pv` lines every 10s in `run.log`; falls back to a 60s heartbeat when `pv` is unavailable.

### Two-step (schema + parallel data)
Best for larger datasets or tighter windows.
- Schema-only dump first, then parallel load, then finalize objects.
- Assumes SQLines Data is installed and available on `PATH` (`sqldata` or `sqlinesdata`), or set `SQLINESDATA_BIN`.
- Requires admin users (`SRC_ADMIN_USER`/`TGT_ADMIN_USER`); preflight fails fast if those logins are not ready.

### Binlog (seed + replication)
Best for low-downtime cutover.
- Seeds target from a consistent dump snapshot with embedded binlog coordinates.
- Starts MariaDB replication from MySQL binlog using `REPL_USER`/`REPL_PASS`.
- Verifies replication thread health and lag after start.
- For MySQL 8.4 sources, an upstream `mysqldump` ≥ 8.4 must be available (8.4 servers reject `SHOW MASTER STATUS`); the tool detects this and fails fast at preflight.

### Staged (offline two-phase via on-disk dump)
Best when source and target are not directly network-reachable, or when a checkpoint between dump and load is desirable.
- Phase-driven via `STAGED_PHASE`:
  - `dump_and_load` (default): dump from source, then load to target on the same host
  - `dump_only`: dump from source to a file directory, then exit (target untouched)
  - `load_only`: load existing dumps into target (no source connection needed; DB list comes from the manifest)
- Per-database compressed dumps (`<db>.sql.gz`) under `${RUN_DIR}/dumps/` by default, or a custom location set via `STAGED_DUMP_DIR`.
- Manifest (`manifest.txt`) tracks per-DB SHA-256, byte size, and approximate row count.
- SHA-256 verification at load time (default on, `STAGED_VERIFY_SHA256=1`).
- Configurable per-DB parallelism (`STAGED_PARALLEL=4` default for dumps; sequential loads).
- Post-load finalize step compares manifest vs. target `information_schema.tables`; hard-fails on missing/empty databases, soft-warns on row-count drift above `STAGED_FINALIZE_DRIFT_PCT` (default 50%).
- Live progress: `pv` lines every 10s when available; otherwise a 60s file-size probe with bytes/elapsed/rate/percent.
- Phase-aware completion banners: "DUMP COMPLETE" / "LOAD COMPLETE" / "MIGRATION SUCCESSFUL".
- Auto-detects most recent `artifacts/run_staged_*/dumps/` as the `load_only` default — re-runs are one Enter press.
- **Caveat**: offline mode. Writes to source during dump are not captured. Use `binlog` if downtime is unacceptable.

## Orchestrator usage

Interactive (recommended):
```bash
./mariadb-migrator
```

Non-interactive CLI:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r orchestrator/requirements.txt
python -m orchestrator.migrationctl plan --config config/migration.yaml --mode one_step --out artifacts/plan
python -m orchestrator.migrationctl run  --config config/migration.yaml --mode one_step --out artifacts/run
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
- `./mariadb-migrator` runs assess → plan → run, and resumes automatically if a previous run failed.
- `./mariadb-migrator` asks for source/target admin credentials at runtime; root is blocked by default unless `ALLOW_ROOT_USERS=1`.
- Saving `config/migration.yaml` is optional and defaults to `No`; if saved, passwords are redacted by default.

## One-step required envs (config/migration.yaml)
Source:
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` or `SRC_DBS` (comma-separated)
- Optional: `SRC_SSL_MODE` (DISABLED|PREFERRED|REQUIRED|VERIFY_CA|VERIFY_IDENTITY) for TLS-required sources

Target:
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`
- `TGT_SSH_HOST`, `TGT_SSH_USER`, `TGT_SSH_OPTS` (required when running from a third host)
- `INSTALL_TARGET_MARIADB` (`0` or `1`, default `1`)
- `TARGET_INSTALL_OS` (required when install flag is `1`)
- `TARGET_MARIADB_VERSION` (required when install flag is `1`)

## Two-step required envs (config/migration.yaml)
Source:
- `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS`
- `SRC_DB` (single DB) or `SRC_DBS` (comma-separated, looped one by one)

Target:
- `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS`
- `TGT_SSH_HOST`, `TGT_SSH_USER`, `TGT_SSH_OPTS` (if running from a third host)
- `INSTALL_TARGET_MARIADB` (`0` or `1`, default `1`)
- `TARGET_INSTALL_OS` (required when install flag is `1`)
- `TARGET_MARIADB_VERSION` (required when install flag is `1`)

Optional:
- `SQLINESDATA_BIN` (auto-detected: `sqldata` then `sqlinesdata`)

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
- `TGT_SSH_HOST`, `TGT_SSH_USER`, `TGT_SSH_OPTS` (required when running from a third host)
- `INSTALL_TARGET_MARIADB` (`0` or `1`, default `1`; auto-set to `0` for `dump_only`)

Dump configuration (optional):
- `STAGED_DUMP_DIR` (default `${RUN_DIR}/dumps`; required for `load_only` to point at the manifest directory)
- `STAGED_COMPRESS` (`1` default — gzip-compress the dump)
- `STAGED_PV` (`1` default — show progress meter via `pv` or fallback probe)
- `STAGED_PARALLEL` (`4` default — concurrent per-DB dumps)
- `STAGED_LOAD_PARALLEL` (`1` default — concurrent per-DB loads)
- `STAGED_VERIFY_SHA256` (`1` default — checksum each dump file before load)
- `STAGED_DISK_HEADROOM_FACTOR` (`2` default — multiplier on source data size for the dump-volume free-space check)
- `STAGED_FINALIZE_DRIFT_PCT` (`50` default — row-count drift threshold above which finalize emits a warning)
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

A phase-equivalence test exists for `staged` mode under `tests/test_staged_phase_matrix.sh`. It runs `dump_and_load` once and `dump_only` + `load_only` separately against the same source, then compares target signatures (schema DDL via `mariadb-dump --no-data`, per-table `CHECKSUM TABLE`) to verify both paths produce identical targets.

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
- `staged` per-DB load resume is not supported in v1. If a load fails partway through a multi-DB run, drop the partially-loaded databases on the target and re-run with `STAGED_PHASE=load_only`.
- `pv` fallback in `one_step` is a heartbeat only (no byte counts), because the data path is a network pipe with no on-disk file to probe. The full file-size probe is available in `staged` mode where the dump is on disk.
