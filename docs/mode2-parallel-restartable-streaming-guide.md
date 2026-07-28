# Parallel Restartable Streaming Copy (`two_step`) mode: prerequisites, monitoring, and troubleshooting

What has to be true before a Parallel Restartable Streaming Copy run, how the
tool moves schema and data, how to watch a run while it is in flight, and what
to do when it breaks.

This is an **offline** mode. Source and target should be quiesced for the copy —
there is no live catch-up phase. If you need near-zero downtime, use Replication
instead. The other offline modes (`one_step`, `staged`) copy the same data by
different mechanics; this mode is the one to reach for when the dataset is large
enough that a single serial stream is too slow.

## How it works

A `two_step` run executes a pipeline of phases. The two headline phases — schema
then data — give the mode its name; the rest run automatically around them:

1. **Preflight** — checks source and target connectivity and the source version
   before anything is moved.
2. **App-user migration** (optional) — copies application users to the target
   when `MIGRATE_APP_USERS=1`; skipped otherwise.
3. **Precheck** — assesses the source for blockers and inventory (version,
   engines, JSON/generated columns, charsets, definers, etc.) and writes TSV
   reports under `artifacts/precheck/`.
4. **Schema, pre-data** — `mariadb-dump` exports the table DDL and applies it to
   the target so the tables exist and are empty. Post-data objects (triggers,
   routines, events) are staged to a `schema_post/` directory for step 6, not
   applied yet.
5. **Data** — `mariadb-mtk` streams the rows from source to target **in
   parallel**. Because the schema already exists, it runs with target-object
   creation off (`-topt=none`) and selects tables with `-t=<db>.*`; the target's
   `FOREIGN_KEY_CHECKS` is turned off for the load and restored afterward.
   Immediately after the transfer, the engine runs a row-count validation pass
   and records the result in the logs (see *Row-count validation* below).
6. **Finalize** — applies the staged post-data DDL (triggers, routines, events)
   to the target.
7. **Analyze** — `ANALYZE TABLE` refreshes the target's optimizer statistics
   after the load (opt-out; see *Post-load ANALYZE TABLE* below).
8. **Validate** — a final check of the target (server version, user auth
   plugins, storage engines) closes the run.

The engine reads its connection and tuning settings from `sqldata.cfg`.

> **Note on naming.** The data engine is `mariadb-mtk`, the MariaDB-packaged
> SQLines Data engine. It was previously distributed as `sqldata`; that legacy
> name is still auto-detected as a fallback, and its config file and several
> env vars still carry the `sqldata`/`SQLINESDATA` prefix. Install it from the
> [MariaDB community page](https://mariadb.com/downloads/community/); if it is
> not on `PATH`, point `SQLINESDATA_BIN` at the binary.

### What "Restartable" means

Restartable refers to **in-session** recovery, not kill-and-resume across
invocations. Within a single run, a data-transfer or DDL statement that fails
with a transient error (timeout, lost connection) is retried automatically. The
retry budget is `-restart_attempts` (default `10`; set `-restart_attempts=0` to
disable). The engine retries immediately after the failure; for reconnect
problems specifically it additionally makes several reconnect attempts, sleeping
progressively longer between them.

`mariadb-mtk` does **not** resume across separate invocations. If a run is
interrupted — killed, host rebooted, hard failure past the retry budget — there
is no checkpoint to pick up from. The correct recovery is an operator decision:
drop the partially-loaded data on the target and start a fresh run. The launcher
reflects this by always starting a fresh run for `two_step` rather than offering
a resume branch.

## Prerequisites

The preflight enforces the connection and privilege items below before the data
step starts. Each item lists how to check it yourself.

### Source (MySQL)

- **Supported version.** MySQL 8.0 and 8.4 are supported. MySQL 5.7 is certified
  for the offline modes (this one included), though it is a boundary case — test
  your own schema before relying on it in production.
  ```sql
  SELECT VERSION();
  ```

- **A read user with `SELECT` on every database being migrated.** `mariadb-mtk`
  reads the tables directly; if it cannot see a table it will not copy it.
  ```sql
  SHOW GRANTS FOR '<src_user>'@'<host>';
  ```

- **Network reachability from the tools host to the source TCP port.** See the
  hostname-resolution note under *Engine / connectivity* — a working `mariadb`
  client connection does not prove the engine can connect.

### Target (MariaDB)

- **Schema present, data empty.** Step 1 creates the tables; the data step
  expects them to exist and to be empty. The launcher runs the target
  pre-existence check on every `two_step` run for this reason — it will not
  quietly load into a half-populated target.

- **A write user with `INSERT` (and DDL for step 1) on the target databases.**
  ```sql
  SHOW GRANTS FOR '<tgt_user>'@'<host>';
  ```

- **Foreign-key handling on managed targets.** On RDS and MariaDB Cloud,
  `SET GLOBAL FOREIGN_KEY_CHECKS=0` is rejected (no `SUPER`). This is expected.
  The tool treats the global `SET` as non-fatal and relies on the **session-level**
  `FOREIGN_KEY_CHECKS=0` in `sqldata.cfg` instead. Confirm that line is present
  in the config the engine actually reads.

### Engine / connectivity

- **`mariadb-mtk` installed and resolvable.** Auto-detected on `PATH`; legacy
  `sqldata` is detected as a fallback. Override with `SQLINESDATA_BIN`.
  ```bash
  command -v mariadb-mtk || command -v sqldata
  echo "$SQLINESDATA_BIN"
  ```

- **The engine resolves the source and target hostnames itself.** `mariadb-mtk`
  has its own resolver path, independent of the `mariadb` client the launcher
  uses. A launcher connection succeeding does **not** confirm the engine can
  resolve the same name. If the engine fails to resolve a host, substitute the
  IP address directly in `sqldata.cfg`.

- **Use `127.0.0.1`, not `localhost`, when targeting a specific TCP port.**
  `localhost` makes the client ignore the port and use the Unix socket;
  `127.0.0.1` forces a TCP connection on the port you specified.

- **`caching_sha2_password` needs a secure transport.** MySQL 8.x users on this
  default plugin require TLS or RSA key exchange. Without it, the connection
  fails as a generic *access denied* even when the credentials are correct — so
  a password that looks right can still be rejected for a transport reason.
  Either connect over TLS or, for the migration user only, switch to
  `mysql_native_password`.

## The `sqldata.cfg` file

`sqldata.cfg` is the `mariadb-mtk` config — the engine reads its connection and
tuning settings from it. Two things about how it is located and populated:

- **It is read from the directory the launcher is started in.** Run
  `mariadb-migrator` from the repo root so the engine picks up the repo's
  `sqldata.cfg`. `sqldata.cfg-example` is the template to copy from.
- **The connection lines are written from your launcher prompt answers** — you
  do not hand-author them. The settings you may still need to adjust by hand are
  the ones noted above and in the worked example: IP-vs-hostname (the engine's
  separate resolver), `127.0.0.1`-vs-`localhost` for a specific TCP port,
  `-restart_attempts` (see *What "Restartable" means*), the session-level
  `foreign_key_checks = 0` required on managed targets, and the large-table
  parallelism settings (see *Parallelism and large tables*).

## Parallelism and large tables

There are two levels of parallelism, both controlled by `mariadb-mtk` settings
in `sqldata.cfg`:

**Table-level** — several tables are transferred at once. The number of
concurrent sessions is set by `-ss` (in the observed configuration, `-ss=4`).
This is why the run log shows several tables started and completing interleaved.

**Intra-table (large tables)** — a single large table can be split into chunks
and its chunks transferred concurrently. It is off unless enabled:

- `-large_tables_parallel=yes` — enable concurrent transfer within a single
  table or partition.
- `-large_tables_rows=1000000` — the row count at which a non-partitioned table
  or partition is treated as "large" and chunked. This value is also the
  estimated chunk size in rows; the actual number of concurrent sessions used is
  governed by `-ss`.
- `-large_tables_resources_pct=70` — the share of resources reserved for large
  tables; large tables are transferred first within that allocation.

**MySQL-source caveats.** Against a MySQL source, intra-table parallelism is
limited:

- `-large_tables_mb` (the size-based threshold) is **not supported from MySQL**
  yet. Use the row-based `-large_tables_rows` threshold instead.
- Only tables with an **auto-increment** column can be chunked, and the chunk
  size is `-large_tables_rows` rows. A large table without an auto-increment key
  transfers as a single session even when it exceeds the threshold.

## Row-count validation

After the parallel transfer, and still inside the data phase, `mariadb-mtk` runs
a validation pass that compares source and target row counts table by table. This is the engine's own
check — separate from both the `ANALYZE TABLE` step and the tool's final
`validate` phase. The result is **recorded in the logs**, not written to a
separate file: the full detail is appended to `run.log` and to each database's
engine log at `mariadb-mtk/<db>/mariadb-mtk.log`.

Scan the run log for the validation result after a run:

```bash
grep -niE 'row.?count|differ' "$RUN_DIR"/run.log
```

A clean run ends with `Row-count validation: all selected database(s) match`,
and per database `row counts OK (all tables equal)`. A mismatch names the
table(s) and prints the differing source and target counts instead.

The parity check is the acceptance gate for the copy, so make it a habit to read
those lines at the end of every run rather than trusting the completion banner
alone. For very large tables it is also worth an independent `COUNT(*)` on both
sides as a second confirmation:

```sql
-- source
SELECT COUNT(*) FROM <db>.<table>;
-- target — should match
SELECT COUNT(*) FROM <db>.<table>;
```

## Post-load ANALYZE TABLE

After a bulk load the target's optimizer statistics are empty or stale, which
can produce poor query plans until they are refreshed. The tool runs
`ANALYZE TABLE` across the migrated databases on the target after the load, so
the optimizer has accurate statistics immediately after cutover.

- It is an **opt-out** prompt — `Run ANALYZE TABLE on target after load? (y/n)`,
  default `y` — shown during setup just after the `Migrate application users?`
  prompt. Set `ANALYZE_TARGET=0` (or answer `n`) to skip it.
- It runs **after the data load and finalize, before the tool's final validate
  phase** — the chain ends `… → finalize → analyze → validate`, so `ANALYZE` is
  not the last step.
- Per-table `ANALYZE` errors are reported but **non-fatal** — only a connection
  failure to the target stops the run. An individual table failing to analyze
  does not fail the migration.
- A report is written to `artifacts/run_two_step_<ts>/analyze_target_report.txt`
  listing the tables analyzed, status counts, and any per-table errors.

This step is shared with the other load-based modes (`one_step`, `staged`); it
is not part of Replication (`binlog`), where replication keeps changing the
tables after the seed.

## Monitoring a run

The data step streams table by table. Watch it live and validate parity after.

Tail the run log in the active run directory (throughput is piped through `pv`):
```bash
tail -f "$RUN_DIR"/run.log
```

Watch for per-table progress and any retry lines from the engine — a burst of
retries that eventually succeeds is the "restartable" behavior working; retries
that exhaust `-restart_attempts` end the run.

After the run, read the row-count validation lines in the log and the analyze
report:
```bash
grep -niE 'row.?count|differ' artifacts/run_two_step_<ts>/run.log
cat artifacts/run_two_step_<ts>/analyze_target_report.txt   # ANALYZE results
```

## Troubleshooting

**Access denied, but the password is definitely correct.**
The user is almost certainly on `caching_sha2_password` and the connection has no
TLS/RSA. Confirm the plugin, then either enable TLS or switch the migration user:
```sql
SELECT user, host, plugin FROM mysql.user WHERE user = '<src_user>';
ALTER USER '<src_user>'@'<host>' IDENTIFIED WITH mysql_native_password BY '<password>';
```

**The `mariadb` client connects, but `mariadb-mtk` cannot reach the host.**
Different binaries, different resolvers. Replace the hostname with its IP in
`sqldata.cfg` and re-run:
```bash
getent hosts <hostname>       # find the IP the OS resolves
# then set that IP as the host in sqldata.cfg
```

**Connecting to `localhost` ignores the port you set.**
`localhost` routes to the Unix socket. Use `127.0.0.1` in the config to force TCP
on the intended port.

**`SET GLOBAL FOREIGN_KEY_CHECKS=0` fails on RDS / MariaDB Cloud.**
Expected — the account lacks `SUPER`. The global `SET` is non-fatal; make sure
the **session-level** `foreign_key_checks=0` line is present in `sqldata.cfg`
and the engine is reading that file.

**The run died partway through.**
There is no resume. Drop the loaded data on the target and start fresh:
```sql
-- fastest clean reset when the whole target DB was for this migration
DROP DATABASE <db>;
CREATE DATABASE <db>;
```
Then re-run from step 1 (schema) so the target is rebuilt before the data step.

**Transient errors mid-run.**
These are auto-retried up to `-restart_attempts` (default 10). If they exhaust
it, raise the
value in `sqldata.cfg` and re-run, or address the underlying cause (network
stability, target lock contention).

**Some tables show a row-count mismatch after load.**
The validation lines in `run.log` and the `mariadb-mtk` logs name the affected
tables. Confirm the source counts are stable (nothing is still writing to the
source), then re-copy those tables.

## Worked example

Concrete values you can adapt. Assume:

- Source: MySQL 8.0 at `10.0.0.11:3306`, user `mig_read`, database `shopdb`
- Target: MariaDB at `10.0.0.22:3306`, user `mig_write`
- Tools host: `mdb-migtools`

**1. `sqldata.cfg` — the settings you touch.**

The connection lines in `sqldata.cfg` are written from the values you enter at
the launcher prompts, so you don't hand-author them. What you *do* adjust:

- **Use IPs, not hostnames**, for the source/target if the engine can't resolve
  the names (it has its own resolver — see *Engine / connectivity*). This is the
  one place you may need to overwrite what the tool wrote.
- **Use `127.0.0.1`, not `localhost`**, when either side is on the tools host and
  you need a specific TCP port.
- **`-restart_attempts`** — the in-run retry budget for transient errors
  (default 10). Raise it if a flaky network is exhausting the default mid-run.
- **Session-level `foreign_key_checks = 0`** — required on managed targets (RDS,
  MariaDB Cloud) where the global `SET` is rejected; confirm this line is present.

**2. Run it.**

The launcher takes source/target connection details from interactive prompts,
not from shell env vars. `SRC_HOST`/`SRC_USER`/`SRC_PASS`/etc. exported in your
shell are ignored — they are variables the launcher *sets and exports to its
phase scripts*, not inputs it reads. Only `MODE` and `SQLINESDATA_BIN` are
honored from the environment.

For a prompt-free run, save a config once and then drive the orchestrator
directly:

```bash
# a) walk the launcher once; answer 'y' to "Include passwords in saved config"
export SQLINESDATA_BIN=/path/to/mariadb-mtk    # only if not on PATH
./mariadb-migrator
#    -> writes config/migration.yaml with connection details (+ passwords)

# b) prompt-free run via the orchestrator
source .venv/bin/activate
python3 -m orchestrator.migrationctl run \
    --config config/migration.yaml --mode two_step \
    --out artifacts/run_two_step_$(date +%Y%m%d_%H%M%S)
```

If the config was saved redacted (the default `n` to the passwords prompt),
supply passwords via env before the orchestrator call — those *are* read on the
direct path:
```bash
export SRC_ADMIN_PASS='S3cret-src' TGT_ADMIN_PASS='S3cret-tgt'
```

**3. Verify.**
```bash
# row-count validation is recorded in the logs, not a separate file
grep -niE 'row.?count|differ' artifacts/run_two_step_<ts>/run.log
cat artifacts/run_two_step_<ts>/analyze_target_report.txt   # ANALYZE results
```
```sql
-- eyeball one table on each side
SELECT COUNT(*) FROM shopdb.orders;   -- source
SELECT COUNT(*) FROM shopdb.orders;   -- target — should match
```

To skip the `ANALYZE TABLE` step, set `ANALYZE_TARGET=0` (or answer `n` at the
prompt); the run is otherwise identical.

If they match and the validation file is empty, the copy is complete. Because
this mode is offline, there is no catch-up step — cut over once you have
confirmed parity.

---
