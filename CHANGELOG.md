# Changelog

## [1.4.0-beta] - 2026-09-04
### Added
- **Application users authenticating with `caching_sha2_password` now keep their
  original passwords.** Previously these accounts were recreated with the shared
  default password. This requires the `caching_sha2_password` plugin on the
  target (MariaDB 11.4.9 / 11.8.4 or later, not loaded by default); when it is
  absent, or a stored password is not in the expected format, the account falls
  back to the default password with the reason recorded in the report. New
  options `PORT_SHA2_PASSWORDS` (default `1`) and `PORT_SHA2_INSTALL_PLUGIN`
  (default `0`). `sha256_password` accounts continue to receive the default
  password.
- **MySQL 5.7 source support for the offline data-movement modes.** Serial
  Streaming Copy (`one_step`), Parallel Restartable Streaming Copy (`two_step`),
  and Offline Copy (`staged`) are now validated end-to-end from a MySQL 5.7
  source into MariaDB. Migrations were exercised 5.7 → MariaDB 11.8, 12.3`
  parity confirmed on the target. JSON columns,which cannot replicate via binlog,
  transfer correctly through all three offline modes.
- Replication (`binlog`) is explicitly **not** supported from a MySQL 5.7
  source. A pre-8.0 source is now blocked by a dedicated source-version gate
  (see below) rather than failing later in binlog setup.

### Changed
- **The launcher now fails immediately when run under bash older than 4.0.**
  The launcher and phase scripts use bash 4.0+ parameter expansion; on macOS,
  which ships bash 3.2.57, this previously surfaced as an opaque
  `bad substitution` error part-way through a run, after connection details
  had already been entered. The check reports the version found and the
  remediation.
- **The source-side client binary is now resolved by probe rather than a fixed
  default.** Phase scripts defaulted `MYSQL_BIN` inconsistently — some to
  `mysql`, some to `mariadb` — so a host carrying only one of the two clients
  could fail in some phases while succeeding in others. All call sites now
  prefer `mariadb` and fall back to `mysql`. An explicit `MYSQL_BIN` in the
  environment continues to take precedence.
- The default replication user offered in Replication (`binlog`) mode is now
  `repl_user` (previously `repl`).
- **Assessment prechecks now guard for 8.0-only catalog objects and degrade
  gracefully on pre-8.0 sources.** Several precheck queries referenced
  `INFORMATION_SCHEMA`/`mysql` columns and tables that do not exist before
  MySQL 8.0. Against a 5.7 source these raised `ERROR 1054` (unknown column) or
  `ERROR 1109`/`1146` (unknown table) and aborted the entire assessment
  (`precheck failed rc=3`). Each affected check now probes for the object's
  existence first (via `INFORMATION_SCHEMA.COLUMNS`/`.TABLES`) and runs its real
  query only when the object is present; otherwise it returns an empty result of
  the same shape, so the check reports "not present" (correct for 5.7) and the
  assessment continues. Metadata objects absent before 8.0 and now guarded:

  | Check (`sql/checks/…`) | 8.0-only object referenced | Kind |
  |---|---|---|
  | `functional_indexes.sql` | `INFORMATION_SCHEMA.STATISTICS.EXPRESSION` | column (8.0+) |
  | `check_constraints.sql` | `INFORMATION_SCHEMA.CHECK_CONSTRAINTS` | table (8.0.16+) |
  | `partial_revokes.sql` | `mysql.user.User_attributes` | column (8.0+) |
  | `gis_srid_usage.sql` | `INFORMATION_SCHEMA.COLUMNS.SRS_ID` | column (8.0+) |
  | `resource_groups.sql` | `INFORMATION_SCHEMA.RESOURCE_GROUPS` | table (8.0+) |
  | `mysql_roles.sql` | `mysql.role_edges` | table (8.0+) |

  The probes are version-agnostic: on an 8.0/8.4 source each object is present
  and the original query runs unchanged, so existing behavior for 8.x sources is
  preserved.
- Replication source-compatibility now checks source version **first**, ahead of
  the JSON-column and `binlog_format` sub-checks. Because a pre-8.0 source is
  categorically ineligible for replication regardless of schema, the version
  gate short-circuits and reports only the version failure — a 5.7 source no
  longer produces a misleading "remove your JSON columns" message, and a
  JSON-free 5.7 source (previously ungated) is now blocked at this point instead
  of failing downstream in binlog setup. The gate is enforced at all three
  layers (launcher, assessment, preflight), matching the existing JSON and
  `binlog_format` gates.

### Fixed
- **Replication mode now works against sources where native password
  authentication is unavailable.** The tool selects an authentication method the
  source supports and enables TLS on the replication link when required.
- **The replication account created on the source is no longer copied to the
  target.** Previously this could stop replication on the target.
- **Replication status is confirmed over several checks before a run reports
  success.** A replica that stops shortly after starting is no longer reported
  as healthy.
- **Rows exceeding the target's `max_allowed_packet` are now detected before the run.**
  A single row larger than the limit stops the load with `ERROR 2006 (Server has
  gone away)` and cannot be split across packets. Before the run phase the tool
  compares the limits on both ends; when the target has less than twice the
  source's limit, tables holding TEXT, BLOB, or JSON columns are scanned and any
  oversized rows reported, with an offer to raise the limit on the target.
  Serial Streaming Copy (`one_step`) checks again at preflight and exits `7`.
- Corrected the `mariadb-mtk` binary lookup in the launcher, which still
  searched only for `sqldata` and failed on hosts carrying the renamed binary.
- **`localhost` as a host input silently ignored the port.** The MySQL and
  MariaDB clients treat `localhost` as an instruction to use a Unix socket,
  discarding the port entirely, so an operator entering `localhost` with a
  non-default port reached the wrong endpoint or failed with error 2002. The
  launcher now substitutes `127.0.0.1` and says so on screen — worth noting
  because `'user'@'localhost'` and `'user'@'127.0.0.1'` are distinct grants.
- **Client option files that alter output format now fail the precheck up
  front.** Options such as `verbose` and `vertical` change how the client frames
  its output, which corrupts the values this program reads back from the servers
  and can fail a gate against a correctly configured server. The precheck now
  detects these locally, before any server connection, and stops with the
  offending options named.
- **Assessment could stall indefinitely at a prompt the operator never sees.**
  When `mariadb-migrate-config-file` is present on the host, `00_precheck.sh`
  offers to run it interactively. Under `migrationctl` the spawned script
  inherited the caller's terminal on stdin, so its `[[ -t 0 ]]` guard evaluated
  true and the prompt fired — but its output went to the captured stream rather
  than the terminal, leaving the run apparently hung with no indication why.
  Operators saw assessment stop after `==> Assessing` and resume only after
  pressing Enter. Scripts spawned from `orchestrator/checks.py`,
  `orchestrator/migrationctl.py`, and `orchestrator/runner.py` now run with
  stdin closed, so any interactive prompt reached under orchestration falls
  through to its non-interactive default instead of blocking.
- **Assessment reported findings from databases outside the migration scope.**
  The precheck SQL in `sql/checks/` queries `INFORMATION_SCHEMA` instance-wide,
  and `orchestrator/checks.py` consumed those results without filtering to the
  selected databases. A migration scoped to one database could therefore raise
  HIGH- and MEDIUM-severity warnings for objects belonging to unrelated
  databases, and include sample rows naming their tables and columns in
  `artifacts/report.json`. Thirteen schema-scoped checks now filter on
  `SRC_DBS`/`SRC_DB`: `json_columns`, `mysql8_collations`,
  `mysql8_column_collations`, `gis_srid_usage`, `check_constraints`,
  `generated_columns`, `functional_indexes`, `functional_defaults`,
  `invisible_columns`, `partitioned_tables`, `schema_charsets`,
  `fk_name_lengths`, and `trigger_order`. Instance-scoped checks
  (`partial_revokes`, `mysql_roles`, `resource_groups`, `active_plugins`,
  `definers_inventory`, `xplugin_status`, and the server-setting checks) are
  deliberately unfiltered, as are `schema_sizes` (whole-instance footprint is
  useful when planning a migration window) and `view_routine_bodies` (its first
  column is an object type, not a schema name). Data movement was never
  affected — the run phase passes the selected databases to `mariadb-dump`
  directly. Inventory counts for the affected checks will be lower than in
  1.4.0-beta for anyone with multiple databases on the source.
- Resource-group precheck no longer false-positives on 8.0 sources. The query
  now excludes the two built-in default groups (`USR_default`, `SYS_default`),
  which are always present, so `resource_groups_in_use` warns only on
  user-defined resource groups — the actual migration concern.
- Interactive replication-compatibility loop no longer exits silently on a
  compatibility failure. Under `set -e`, a non-zero return from the compat check
  was terminating the launcher before it could branch, so neither the
  version-gate advisory nor the JSON re-prompt was reached. The check's return
  code is now captured explicitly; a categorical version failure prints its
  advisory and stops (re-entering a database name cannot help a 5.7 source),
  while a recoverable JSON-column failure re-prompts for the database list so
  the operator can drop the offending database and retry the same mode.
- Corrected the mode name printed in the replication-incompatibility advisory
  from "Parallel Streaming Copy" to "Parallel Restartable Streaming Copy"
  (launcher, assessment, and preflight paths).
- **Assessment output no longer lists objects from databases outside the
  migration.** Precheck queries run instance-wide, so the run log reported
  tables, collations, and columns from every database on the source rather than
  only the selected ones. The assessment report was already scoped correctly,
  which meant the two disagreed. The log is now scoped to the selected
  databases and records which databases were in scope for the run.
- **Rows larger than 16 MiB no longer fail part-way through a load.** The client
  used to move data carries its own packet-size limit, independent of the source
  and target servers. A single row above that limit stopped the load with
  `ERROR 2020`, even when both servers had been raised to accommodate it — and
  even after the pre-run check reported sufficient headroom, because that check
  compares the two servers and cannot see the client's own limit. All data-path
  client invocations now set the limit explicitly. Verified with 20 MiB rows
  migrating intact into a MariaDB target at its 16 MiB default.

### Compatibility notes
- `PORT_SHA2_PASSWORDS` and `PORT_SHA2_INSTALL_PLUGIN` are both optional. On a
  target without the `caching_sha2_password` plugin, behavior is unchanged.
- No configuration changes required. Behavior for MySQL 8.0 and 8.4 sources is
  unchanged across all modes; the precheck probes and the version gate are
  no-ops on 8.0+ sources.
- MySQL 5.7 CHECK constraints are parsed but not enforced by the 5.7 server; a
  5.7 → MariaDB migration therefore carries the constraint definition forward
  without any pre-existing enforced state to preserve.

## [1.3.2-beta] - 2026-07-14
### Changed
- Application user migration: operators can now choose whether users who
  receive the default password are required to change it on first login.
  Previously these users were always forced to reset on first login; the
  interactive flow now asks, and non-interactive runs control it via the
  APP_USER_PWD_EXPIRE option in migration.yaml (default: required, matching
  prior behavior). Users whose original password is carried over are
  unaffected.
- Parallel Restartable Streaming Copy (`two_step`): binary detection now looks for
  `mariadb-mtk` first, matching the rename of the SQLines Data engine from `sqldata`
  to `mariadb-mtk`. The legacy `sqldata` name is retained as a fallback probe, so
  hosts still carrying the old binary name continue to work. Applies to both
  `scripts/12_two_step_sqldata.sh` and `scripts/00_preflight_two_step.sh`.
- Docs (`README.md`): `mariadb-mtk` is now described as the current binary name
  (renamed from `sqldata`), with `sqldata` documented as the legacy fallback.
### Removed
- Dead `sqlinesdata` fallback probe in `scripts/12_two_step_sqldata.sh` — no binary
  by that name has ever been distributed in this toolchain.
### Fixed
- Operator-facing "binary not found" message now names `mariadb-mtk`.
- Assessment no longer continues when the source database list is left empty.
  The first database-name prompt now requires at least one name and re-prompts
  on a blank entry, instead of silently proceeding to a later failure.
### Compatibility notes
- No configuration changes required. `sqldata.cfg` and the `sqldata`-named artifact
  paths and logs are unchanged. An explicitly set `SQLINESDATA_BIN` is honored as
  before, regardless of the binary's name.

## [1.3.0-beta] - 2026-06-16
### Added
- Parallel Restartable Streaming Copy (`two_step`): post-load source/target row-count
  validation. After a successful load, row counts are compared per database via
  `sqldata -cmd=validate -vopt=rowcount`; results are appended to both the per-database
  `sqldata.log` and the run `run.log`. Report-only — the transfer step remains the gate.
  Skip with `MIGRATOR_SKIP_ROWCOUNT_VALIDATE=1`.
### Changed
- Install: use the release archive (`.tar.gz`/`.zip`) from the MariaDB downloads page
  instead of `git clone`.
- Docs: `mariadb-mtk` adopted as the primary name for the SQLines Data engine
  (binary still invoked as `sqldata`).

## [1.2.9-beta] — 2026-06-08

Retires the experimental resumable load variant of Parallel Restartable
Streaming Copy (`two_step`) now that chunked, parallel loading with in-run
retries is native to SQLines Data, renames the mode to match, and settles on a
simple restart model: an interrupted load is restarted by dropping the target
and re-running, while SQLines Data self-heals transient errors within a run.

### Parallel Restartable Streaming Copy — variant retired, mode renamed

The `two_step` mode previously offered a `TWO_STEP_VARIANT` choice between
`single_pass` and an EXPERIMENTAL `resumable` load. SQLines Data now provides
restartable, chunked loading natively, so the variant has been removed and the
mode renamed from **Parallel Streaming Copy** to **Parallel Restartable
Streaming Copy** across the menu, `--help`, `mode_label()` banners, and the
sqldata-not-found message.

- The internal mode id `two_step` is unchanged — `config/migration.yaml`,
  `--mode`, env vars, and `state.json` from earlier runs are unaffected.
- Large tables (by default 1,000,000+ rows, set by `large_tables_rows`;
  chunking requires an `AUTO_INCREMENT` column, so tables without one transfer
  as a single stream) are loaded as parallel chunks, and transient errors are
  retried automatically within a run (`restart_attempts`, default 10). On by
  default, no extra arguments; defaults and tuning are documented in
  `scripts/sqldata.cfg-example`.
- `step_map.yaml` (`two_step_data`) now runs `scripts/12_two_step_sqldata.sh`
  directly; the per-variant dispatcher is gone.

### Restart model — drop and re-run, no cross-invocation resume

SQLines Data does not resume a load across separate invocations. Rather than
carry cross-run resume logic in the launcher, an interrupted or failed
`two_step` run is restarted by dropping the target database(s) and re-running
from a clean target.

- The launcher always starts a fresh run for `two_step` (it is excluded from
  the signature-match resume path), and the target-DB pre-existence check
  always runs — so a leftover target database from an interrupted run is
  reported as a conflict to drop. Resume behavior for `one_step`, `binlog`,
  `replace_slave`, and `staged` is unchanged.
- Within a single run, SQLines Data's own `restart_attempts` retry transient
  errors (network blips, brief target restarts), so a run survives them
  without operator action.

### Removed

- The `single_pass`/`resumable` variant added in v1.2.0-beta: the variant
  sub-menu, the `TWO_STEP_VARIANT` prompt and run-signature entry, and the
  resumable-variant resume detection in the launcher.
- `scripts/12_two_step_dispatch.sh` and
  `scripts/12_two_step_sqldata_resumable.sh` (no longer referenced).
- The resumable-only `PRIMARY KEY` preflight check in
  `scripts/00_preflight_two_step.sh` — the native loader does not require it.

### Deprecated

- `TWO_STEP_VARIANT` is accepted-but-ignored this release (a one-line notice is
  printed if it is set); it will be removed in a future release. There is no
  replacement — restartable, chunked loading is always on.

### Compatibility notes

- No configuration changes required. A stale `TWO_STEP_VARIANT` in env or
  `config/migration.yaml` is ignored.
- `large_tables_mb` (size-based large-table selection) is not yet supported for
  MySQL sources; row-based selection (`large_tables_rows`) applies.
- Verified end-to-end on a `two_step` migration from MySQL 8.4.7 to MariaDB
  11.8.1. There is no cross-invocation resume for `two_step`; an interrupted
  run is restarted by dropping the target and re-running.

## [1.2.6-beta] — 2026-06-01

Adds an optional post-load ANALYZE TABLE phase so the target's optimizer
statistics are refreshed automatically after a migration, and reduces the
client-tool noise in the Serial Streaming Copy (`one_step`) run logs.

### Post-load ANALYZE TABLE — new optional phase

After a dump/restore the target's optimizer statistics are empty or stale,
which can produce poor query plans until something triggers a refresh. A new
phase (`scripts/28_analyze_target.sh`) runs `ANALYZE TABLE` across the base
tables of each migrated database on the target, so the MariaDB optimizer has
accurate statistics immediately after the load.

A new prompt — `Run ANALYZE TABLE on target after load? (y/n)` — appears
during setup directly after the `Migrate application users?` prompt, with a
default of `y` (opt-out). It is shown only for the modes where a fresh load
occurs: Serial Streaming Copy (`one_step`), Parallel Streaming Copy
(`two_step`), and Offline Copy (`staged`). The phase runs after the load step
and before final validation in each. It is not wired into Replication
(`binlog`) — replication continues to mutate tables after the seed, so a
post-snapshot analyze would be meaningless — nor into the internal `inplace`
mode, which has no fresh load.

Behavior:

- The phase self-skips when the operator declines (`ANALYZE_TARGET=0`) or when
  Offline Copy runs as `dump_only` (no load happens on that host), consistent
  with how the other optional phases gate themselves.
- A connection failure to the target is fatal (the run stops). An `ANALYZE
  TABLE` error on an individual table is recorded in the report but is
  non-fatal — a statistics-refresh problem on one table must not mark an
  otherwise-successful data migration as failed.
- A per-run report is written to `<run_dir>/analyze_target_report.txt`
  (and echoed to the run log) summarizing tables analyzed, status counts, and
  any per-table errors.

`ANALYZE_TARGET` is consumed by the orchestrator like the other phase
settings and is persisted in `config/migration.yaml.example` so saved
configurations carry the choice forward.

### Client noise — reduced in the Serial Streaming Copy chain

The `one_step` run path emitted two recurring cosmetic lines from its client
invocations: the `mysql: Deprecated program name` banner (from invoking the
legacy `mysql` client name when it is a MariaDB alias) and the
`WARNING: option --ssl-verify-server-cert is disabled` warning (emitted when
the client silently disables certificate verification on a passwordless
login). These are now addressed at the source across the `one_step` chain:

- `scripts/00_preflight_one_step.sh`, `scripts/10_one_step_migration.sh`:
  source-side client queries default to the canonical `mariadb` binary
  (`MYSQL_BIN` default changed from `mysql` to `mariadb`; override still
  honored) and pass `--ssl-verify-server-cert` explicitly so the client does
  not auto-disable-and-warn.
- `scripts/10_one_step_migration.sh`: the data-path dump and load client
  invocations also pass the flag explicitly.
- `scripts/07_validate.sh`: all target validation queries (TCP and SSH) pass
  the flag explicitly.

The deprecation banner is eliminated from the `one_step` chain. This is a
presentation change only — the underlying connection behavior (encrypted, not
server-verified, on a passwordless login) is unchanged; the option is simply
stated rather than left implicit. Dump-tool selection — including the
requirement for an upstream `mysqldump` 8.4+ when migrating from MySQL 8.4
sources in binlog seeding — is deliberately untouched.

### Repository cleanup

- Removed stale dated backup copies of phase and orchestrator scripts
  (`*-functioning-*`, `*-depricated-remove`, dated suffixes) that duplicated
  tracked files and were an edit-the-wrong-copy hazard.

### Known issues

- One source-side check in `scripts/00_preflight_one_step.sh` (source database
  existence) still emits the `--ssl-verify-server-cert is disabled` warning.
- The client-noise reduction covers the `one_step` chain only. Other modes'
  preflight and phase scripts still emit the deprecation banner and/or SSL
  warning; converting them is planned for a later release.

### Compatibility notes

- No configuration changes required. The new ANALYZE prompt defaults to yes;
  set `ANALYZE_TARGET=0` (or answer `n`) to skip it. Existing config files and
  env-var workflows are unaffected.
- Verified end-to-end on a Serial Streaming Copy migration from MySQL 8.4.7 to
  MariaDB 11.8.1.

## [1.2.3-beta] — 2026-05-28

Rewrites application user migration to be plugin-aware, role-aware, and
consistent across all four user-facing modes. Adds a read-only assess-phase
counterpart so operators can preview which users would migrate, which would
have their passwords reset, and which would be skipped, before committing to
a run.

### Application user migration — plugin-aware behavior

The user migration step (`scripts/09_migrate_app_users.sh`) previously had
two code paths: `mysql_native_password` users had their hash ported via
`IDENTIFIED BY PASSWORD '<hash>'`, and everyone else was recreated on the
target with a default password. This collapsed too many cases. Notably,
`caching_sha2_password` users (the MySQL 8.0 default plugin) were silently
re-credentialed with the default password and no per-user audit trail, and
non-password authentication plugins (`auth_socket`, `unix_socket`,
`auth_pam`, `mysql_no_login`) were treated identically — pushed onto the
target as password-authenticated accounts even though their original
authentication model wouldn't have been password-based at all.

User creation is now a four-way branch:

| Source plugin | Target outcome |
|---|---|
| `mysql_native_password` (with hash) | Hash ported via MariaDB-native `IDENTIFIED VIA mysql_native_password USING '<hash>'`. Original password preserved. |
| `mysql_native_password` (no hash) | Default password + `PASSWORD EXPIRE`. |
| `caching_sha2_password`, `sha256_password` | Default password + `PASSWORD EXPIRE`. Hash formats are not portable across engines. |
| `auth_socket`, `unix_socket`, `auth_pam`, `mysql_no_login`, anything else | Skipped. Non-password auth does not translate cleanly; manual configuration on target required. |

The `IDENTIFIED VIA ... USING` form replaces the prior
`IDENTIFIED BY PASSWORD '<hash>'`. Both work on current MariaDB releases, but
the new form is the documented MariaDB-native syntax and is more
future-proof.

Every user creation that uses the default password also sets `PASSWORD
EXPIRE`, so the user is forced to change their password on first login
rather than relying on operator follow-through after reading the report.

### Roles — distinguished from users and replayed via `CREATE ROLE`

In MySQL 8.0+, roles are stored in `mysql.user` alongside users, and prior
versions of `09_migrate_app_users.sh` did not distinguish them. A source
role would land on the target as a regular password-authenticated user with
the default password — both incorrect (the row should be a role, not a
user) and a security smell (an apparently-loginable account with a known
default password).

Role discovery now runs as a separate pass before user migration, using the
standard MySQL fingerprint:

```sql
account_locked = 'Y' AND password_expired = 'Y' AND authentication_string = ''
```

Each identified role is replayed on target via `CREATE ROLE IF NOT EXISTS`.
The user loop then skips role rows so they are not re-created as users.
Grants of roles to users (`GRANT <role> TO <user>`) are attempted as normal
during grant replay; if the role was created successfully in the prior pass,
the grant succeeds.

### Application user migration — wired into all user-facing modes

Previously, the `migrate_app_users` step was wired only into
`one_step_prepare` in `orchestrator/step_map.yaml`. Operators who picked
Parallel Streaming Copy (`two_step`), Offline Copy (`staged`), or
Replication (`binlog`) and answered `y` to the
`Migrate application users? (y/n)` prompt found their application users had
silently not been migrated. The step is now part of the prepare phase for
all four user-facing modes. Internal mode (`inplace`) and deprecated mode
(`replace_slave`) are not wired.

### Application user assessment — new read-only assess-phase step

A new `scripts/01_assess_app_users.sh` runs during the assess phase when
`MIGRATE_APP_USERS=1`. It performs the same discovery the run-phase script
does (source role identification, user enumeration, plugin classification,
grant count) but writes nothing to the target. It emits a prediction report
at `<assess_dir>/user_assessment_report.txt` framed in "would create / would
preserve / would default-password / would skip" language.

The assess script is invoked from `orchestrator/migrationctl.py` after the
existing assessment checks complete and before the gate decision. Failures
in user assessment are logged but do not fail the assess gate — user
migration is operator-optional, so a discovery failure should not block a
migration the operator wants to proceed with regardless.

Operators picking `Assess & Plan` from the top-level menu (added in
1.2.2-beta) now see exactly what user migration would do without committing
to a run. Picking `Assess + Run` produces both a prediction report (under
`assess_<ts>/`) and a write report (under `run_<mode>_<ts>/`) for the same
migration, allowing post-hoc diff to confirm prediction matched outcome.

### Per-user audit trail

Both the run-phase and assess-phase scripts emit a summary block at the end
of their execution, to stdout and to a report file at
`<run_dir>/user_migration_report.txt` (run) and
`<assess_dir>/user_assessment_report.txt` (assess). The summary tabulates:

- Roles created (or to be created)
- Users with original password preserved (or to be preserved)
- Users with password reset to default — with `PASSWORD EXPIRE` set
- Users skipped due to non-password authentication plugin
- Users that failed to migrate (CREATE/ALTER USER returned an error)
- Grants attempted, succeeded, and dropped (or grant count, in assess)

Per-user detail lists follow the counts, naming each affected user@host
identifier. Operators can grep the report for "ROTATE THESE" pointers or
diff the assess and run reports across the same migration.

### Client noise — filtered in user migration scripts

Both user migration scripts now filter known cosmetic noise from the
underlying client invocations:

- The `mysql: Deprecated program name. It will be removed in a future
  release, use '/usr/bin/mariadb' instead` banner emitted when `mysql` is a
  legacy alias for `mariadb`.
- The `WARNING: option --ssl-verify-server-cert is disabled, because of an
  insecure passwordless login.` warning emitted whenever credentials are
  passed via `MYSQL_PWD` env var instead of on the command line.

The same noise from other phase scripts (preflight, dump, validate) is
unaffected in this release and is tracked for a future cleanup pass.

The default for `MYSQL_BIN` is also changed from `mysql` to `mariadb` in the
user migration scripts, eliminating the deprecation banner at its source on
systems where `mysql` is a legacy alias. Operators who genuinely need the
upstream `mysql` client can still override via `MYSQL_BIN=mysql`.

### Compatibility notes

- No configuration changes required. The `Migrate application users? (y/n)`
  prompt and the `MIGRATE_APP_USERS` env var behave the same as before;
  what changed is what happens when the operator says yes.

- Tested against MySQL 8.0 and 8.4 sources with MariaDB 11.x as the target,
  across Serial Streaming Copy, Parallel Streaming Copy, and Offline Copy.
  Replication (`binlog`) uses the same wiring but wasn't exercised this
  release.

- For MySQL 8.4 sources, expect most users to land on the default-password
  path — 8.4 ships with `mysql_native_password` disabled by default, so
  there's usually no portable hash to preserve. Plan a password rotation
  pass before bringing traffic up on the target.

- Replication, monitoring, and backup-tool accounts (`repl_user`,
  `replicator`, `dbpwf*`, `aws_*`, `pmm_*`, `xtrabackup*`,
  `mysqld_exporter`, and similar) still come across as application users.
  A pattern-based exclusion list is planned for the next release; for now,
  drop these on target after migration if you don't want them.

- Grants that fail at replay time all show up as "dropped (incompatible
  with MariaDB)" — the report doesn't distinguish between MariaDB not
  understanding a privilege (e.g. `APPLICATION_PASSWORD_ADMIN`,
  `SYSTEM_USER`, `SET_ANY_DEFINER`) and the target admin lacking
  `GRANT OPTION`. If you're seeing more dropped grants than expected,
  double-check the target admin's grants before assuming syntax
  incompatibility.

## [1.2.2-beta] — 2026-05-27

Introduces a top-level operator menu separating assess-only flow from the
full migration, moves the optional my.cnf converter above mode selection so
it applies regardless of mode, and adds a back-navigation option on the
mode-selection menu so operators can change their initial choice without
restarting the tool.

### Top-level menu — Assess & Plan vs. Assess + Run

The launcher previously dropped operators directly into mode selection on
invocation. There was no way to express "I want to look at the source and
generate a plan but not run anything" except by invoking the orchestrator's
phase flags (`--assess`, `--plan`) directly, which bypasses the interactive
flow entirely. Operators evaluating a migration before commitment had to
either run the full flow and decline the run-phase confirm, or learn the
non-interactive invocation.

A new menu now precedes mode selection in the interactive flow:

```
What would you like to do?
  1) Assess & Plan    Inspect source and target, validate connectivity and
                      compatibility, and produce an assessment report and the
                      migration plan. No data is moved.
  2) Assess + Run     Assess the source, then proceed to the full migration
                      (plan + run, with confirm steps between phases).
  q) Quit
```

Pressing Enter selects option 2 (Assess + Run) to match the most common
operator intent. The menu also displays the direct-invocation flags
(`--assess`, `--plan`, `--run`, `--help`) for operators who prefer the CLI
form.

Internally, option 1 sets `PHASE_MODE="assess_plan"` — a new composite mode
that invokes `migrationctl assess` and then `migrationctl plan`, stopping
cleanly before the run phase. Direct invocation with `--assess`, `--plan`,
or `--run` bypasses the menu entirely; the CLI flags remain atomic
(one phase per flag) and the new `assess_plan` mode is not exposed as a
flag.

### Mode-selection menu — back-to-top-level option

A `b) Back to top-level menu` option is added to the mode-selection menu.
Selecting it re-runs the top-level menu so operators can change their
Assess & Plan / Assess + Run choice without restarting the tool. The
my.cnf converter is not re-offered when returning from this path; it was
already answered earlier in the flow.

### my.cnf converter — moved above mode selection

The optional MySQL `my.cnf` → MariaDB conversion (via
`mariadb-migrate-config-file`) was previously invoked after mode selection,
source/target prompts, and the user-migration prompt — inside a guard that
checked for `needs_endpoint_preflight`, which is mode-dependent. The
conversion itself is mode-agnostic (it reads a file and produces a file;
no source or target involvement), so the placement was a historical
accident rather than a design choice.

The conversion prompt is now offered immediately after the top-level menu
and before mode selection, unguarded by mode. Operators can convert a
my.cnf regardless of which migration mode they ultimately pick, and the
prompt appears at the same logical point in every flow rather than buried
in mid-flow.

### CLI help text — describes the menu

The `--help` output previously read:

```
Default behavior (no flag): assess -> plan -> run
```

This is no longer accurate. The updated text reads:

```
Default behavior (no flag): present an interactive top-level menu with two
choices: 'Assess & Plan' (run assess + plan, stop before run) or 'Assess +
Run' (run the full migration: assess -> plan -> run, with confirm steps
between phases). The phase flags below bypass the menu and run a single
phase.
```

Each phase flag's description also gains "(bypasses the menu)" so the
interactive vs. CLI behavior is unambiguous.

### Compatibility notes

- No configuration changes required. Saved `config/migration.yaml` files
  from prior releases continue to work; the top-level menu only affects the
  interactive flow.

- `--assess`, `--plan`, and `--run` CLI flags behave identically to prior
  releases. They remain atomic (one phase per flag) and bypass the new
  menu entirely.

- The my.cnf converter relocation has no behavioral impact when the
  converter is declined (operators who answered `n` previously will continue
  to see and decline the prompt; only its position in the flow changed).
  When the converter is accepted, the resulting file is unchanged.

## [1.2.1-beta] — 2026-05-26

Hardens Replication (`binlog`) mode against two source-side configurations
documented as Known issues in 1.2.0-beta. The failure modes are now caught at
three independent points in the operator's journey rather than surfacing as a
downstream replication abort, and the prior auto-coercion of `binlog_format`
has been replaced with an explicit requirement.

### Replication (`binlog`) mode — source compatibility gate

Two source-side configurations were known to break Replication (`binlog`)
mode in 1.2.0-beta and earlier: schemas containing JSON columns, and sources
running `binlog_format` other than `ROW`. Both are now enforced as a single
composite gate (`binlog_source_compatibility`) at three independent points in
the operator's journey. Operators with either issue are routed to one of the
offline modes — Serial Streaming Copy (`one_step`), Parallel Streaming Copy
(`two_step`), or Offline Copy (`staged`) — which are unaffected by the JSON
limitation, or instructed to remediate the source `binlog_format`
configuration and re-run.

- **JSON columns.** Schemas containing one or more JSON columns are detected
  via `sql/checks/json_columns.sql` and the result set is filtered to the
  schemas selected for migration. Offending `schema.table.column` triples
  are listed inline in every layer's failure message.

- **`binlog_format`.** The source must be at `binlog_format=ROW`. MIXED and
  STATEMENT are both rejected. The preflight previously auto-coerced the
  source to `binlog_format=MIXED` via `SET GLOBAL`; this has been removed
  because `SET GLOBAL` only affects sessions established after the change,
  so existing application writer sessions continue using the prior format
  until they reconnect. The remediation message instructs setting
  `binlog_format = ROW` under `[mysqld]` in the source `my.cnf` and
  restarting the source MySQL server.

Enforcement layers:

- **Launcher.** Fires immediately after source DB verification, before any
  target credentials are requested. On failure, the operator is returned to
  the mode-selection menu with the source connection info preserved, so a
  compatible mode can be picked without restarting the tool. Both sub-checks
  run; if both fail, both findings are presented in a single unified
  advisory rather than as a sequence of run→fix→re-run cycles.

- **Assessment.** `migrationctl assess` now accepts `--mode` and emits
  `ASSESSMENT: FAIL` when binlog mode is selected against an incompatible
  source. This replaces the prior informational `ASSESSMENT: PASS` that
  contradicted the downstream preflight outcome.

- **Preflight.** `scripts/00_preflight_binlog.sh` enforces both checks for
  non-interactive `--run` callers that bypass the assessment phase. The
  checks remain at distinct exit codes (exit 9 for `binlog_format`, exit 10
  for JSON columns) to preserve granularity for ops automation that
  distinguishes between failure categories.

Failure messages at every layer reference the three alternative modes by
their user-facing labels only — internal mode identifiers do not appear in
operator-facing output.

### Menu labels — serial vs. parallel distinction

- Menu options 1 and 2 now indicate the practical difference in data-transfer
  behavior at selection time:
  - `Streaming Copy (mariadb-dump)` → `Serial Streaming Copy (mariadb-dump)`
  - `Streaming Copy (sqldata)` → `Parallel Streaming Copy (sqldata)`
- The new labels apply consistently across the short menu, the
  detailed-descriptions view (`d`), the two_step variant sub-menu header,
  the `mode_label()` function used in completion banners and logs, and the
  sqldata-not-found error message. Internal mode identifiers (`one_step`,
  `two_step`, `staged`, `binlog`) are unchanged; only display strings
  differ. Existing config files, env-var workflows, and `step_map.yaml`
  entries need no changes.

### Source-side compatibility checks — extension point

- The launcher gains a dispatcher (`run_source_side_compatibility_checks`)
  called after source DB verification and before target prompts. The
  dispatcher routes to per-mode helper functions; only `binlog` is populated
  at this release (`check_binlog_source_compat`). Other modes fall through
  with no checks, preserving prior behavior exactly. Future mode-specific
  source-side blockers should be added at three layers in parallel —
  launcher dispatcher, mode preflight, assessment gate — as documented in
  the comment block above the dispatcher in `mariadb-migrator`.

### Compatibility notes

- Operators previously attempting binlog mode against a JSON-bearing schema
  will now be blocked. The migration was not succeeding in those
  configurations in 1.2.0-beta; the failure mode shifts from a runtime
  replication abort to an upfront refusal with a clear remediation path.

- Operators previously running binlog mode against a source at
  `binlog_format=MIXED` (the prior auto-coerced default) or `STATEMENT`
  will now be blocked at all three enforcement layers, not just the
  run-phase preflight. Remediation: set `binlog_format = ROW` in the source
  `my.cnf` and restart the source server, or switch to an offline mode.

- No changes to non-binlog modes. Serial Streaming Copy (`one_step`),
  Parallel Streaming Copy (`two_step`), and Offline Copy (`staged`) are
  unaffected by any gate added in this release; the launcher's
  compatibility-check dispatcher is a no-op for these modes.

## [1.2.0-beta] 

### Added
- Numbered interactive menu (1–4) for mode selection, replacing the free-text prompt. Press `d` from the top-level menu for detailed descriptions (caveats, disk, version notes); `q` to quit.
- Sub-menus for Parallel Streaming Copy (`two_step`) variant and Offline Copy (`staged`) phase selection, with `b` to return to the top-level mode menu.
- `TWO_STEP_VARIANT` env var (`single_pass` | `resumable`, default `single_pass`).
- Resumable Parallel Streaming Copy (`two_step`) variant **[EXPERIMENTAL]**: tables grouped into batches with a manifest written after each batch; failed loads resume from the first non-complete batch instead of restarting from scratch. Requires `PRIMARY KEY` on every migrated table.
- PRIMARY KEY preflight check in `scripts/00_preflight_two_step.sh` — when `TWO_STEP_VARIANT=resumable`, scans `information_schema.STATISTICS` and lists any PK-less tables before load begins; load is blocked if any are found.
- `TWO_STEP_VARIANT` participates in the run signature so switching variants between runs invalidates resume and starts a fresh run directory.
- `mode_label` helper centralizes user-facing mode names; completion banners (`MIGRATION SUCCESSFUL`, `MIGRATION FAILED`, `DUMP COMPLETE`, `LOAD COMPLETE`) show the descriptive label.
- MariaDB Cloud documented as a target — validated for Offline Copy (`staged`), Parallel Streaming Copy (`two_step`), and Serial Streaming Copy (`one_step`).

### Changed
- Mode names — user-facing labels reordered per menu numbering. Internal identifiers (`one_step`, `two_step`, `staged`, `binlog`) are **unchanged** in `config/migration.yaml`, env vars (`MODE=...`), `state.json`, run-directory naming, and the orchestrator CLI (`--mode <id>`). The relabel is strictly surface-level:

  | # | New label | Internal id (unchanged) |
  |---|---|---|
  | 1 | Serial Streaming Copy (mariadb-dump) | `one_step` |
  | 2 | Parallel Streaming Copy (sqldata) | `two_step` |
  | 3 | Offline Copy (mariadb-dump) | `staged` |
  | 4 | Replication (binlog) | `binlog` |

- `print_modes_briefing` and `print_modes_detailed` reordered to match menu numbering (the `staged` and `binlog` blocks swapped position).
- The Offline Copy (`staged`) offline-acknowledgment banner now reads "IMPORTANT: Offline Copy IS AN OFFLINE MIGRATION" (was: "STAGED MODE IS AN OFFLINE MIGRATION") and refers operators to "Replication (`binlog`) mode" (was: "'binlog' mode") for low-downtime scenarios.

### Fixed
- `FORCE_NEW_RUN=1` is now honored on all four resume branches (Serial Streaming Copy, Parallel Streaming Copy, Replication, Offline Copy). Previously the variable was only checked on the binlog/replace_slave branch; signature-match resume branches ignored it and proceeded with the previous run.
- Banner now distinguishes "FORCE_NEW_RUN=1 set; ignoring previous run and starting a new run directory" from "Previous run found, but inputs changed. Starting a new run directory."
- Preset env values for `TWO_STEP_VARIANT` and `STAGED_PHASE` are now respected. Previously the launcher unconditionally re-prompted and overwrote env-supplied values, which prevented fully scripted runs of those sub-options.

### Known issues
- **SQLines Data may silently truncate reads on very large tables** (>~5M rows) in some configurations, leading to row-count shortfalls on the target without an error from the tool. Affects Parallel Streaming Copy (`two_step`) regardless of variant. Validate post-load with `COUNT(*)` parity on both sides. For production-critical runs in this release prefer Offline Copy (`staged`) or Serial Streaming Copy (`one_step`). Investigation ongoing with the SQLines developer.
- The resumable Parallel Streaming Copy variant is labeled **[EXPERIMENTAL]** for this reason — the manifest/resume mechanics are correct, but they sit on the affected SQLines Data load path.
- **Replication (`binlog`) is not safe for schemas containing JSON columns** when the source uses `binlog_format=MIXED` (the MySQL default since 8.0). Any non-deterministic construct in DML (`RAND()`, `UUID()`, certain uses of `NOW()`/`SYSDATE()`, AUTO_INCREMENT touched by a trigger, `FOUND_ROWS()`, `ROW_COUNT()`, `LOAD_FILE()`, loadable functions, and several others) escalates to ROW logging; ROW events for JSON columns carry MySQL's binary JSON image, which MariaDB cannot apply against its `LONGTEXT` JSON storage. Use one of the offline modes for JSON-bearing schemas.

### Notes
- `inplace` and `replace_slave` modes remain reachable via `MODE=<id>` in the environment; they are intentionally not exposed in the numbered menu.
- Operators with existing `MODE=staged` (or other internal-id) scripts and config do not need to change anything — internal identifiers are the canonical form in all non-display contexts.

## [1.1.0-beta] — 2026-05-07

### Added
- New `staged` migration mode — offline two-phase migration via on-disk dump files
- Three sub-phases: `dump_and_load`, `dump_only`, `load_only`
- ... (rest of the bullets from my previous message)

### Changed
- "Migrate application users?" prompt default flipped from `y` to `n`
- "Convert MySQL my.cnf?" prompt default flipped from `y` to `n`
- `pv` invocation now uses `-f -i 10` for orchestrator-visible progress

### Fixed
- (any bug fixes that landed in this release)

## [1.0.0-beta] — 2026-05-06
- Initial Internal beta
