# Serial Streaming Copy (`one_step`) mode: prerequisites, monitoring, and troubleshooting

What has to be true before a Serial Streaming Copy run, how the tool moves
schema and data in a single piped pass, how to watch a run while it is in
flight, and what to do when it breaks.

This is the simplest **offline** mode. `mariadb-dump` reads the source and its
output is piped straight into the `mariadb` client on the target — one process
reading, one process writing, nothing staged to disk on either host. The source
should be quiesced for a consistent cutover; there is no live catch-up phase, so
if you need near-zero downtime, use Replication instead.

Pick this mode when the dataset is small-to-moderate and you want the least
moving parts. When a single serial stream is too slow for the data size, use
Parallel Restartable Streaming Copy. When source and target are not
network-reachable at the same time, or you want an inspectable checkpoint
between reading and writing, use Offline Copy.

## How it works

One pass, hence `one_step`. `mariadb-dump` emits a single SQL stream — schema,
data, and routine/trigger/event definitions together — and the `mariadb` client
replays it into the target as it arrives:

1. **Copy** — `mariadb-dump <source> | mariadb <target>`. Schema and data move in
   the same stream; there is no separate schema-first phase and no on-disk file.
2. **Analyze** — once the stream completes, an `ANALYZE TABLE` phase refreshes the
   target's optimizer statistics (opt-out; see below).
3. **Validate** — a post-run check confirms the target server is healthy and
   reports its version, user authentication plugins, and storage engines.

The full phase order is: an optional target-install step, then preflight, an
optional application-user migration, the precheck suite (source variables and
blockers), the copy itself, the `ANALYZE TABLE` step, and the validate step.

"Serial" is literal — one dump process feeding one load process, in order, start
to finish. There is no manifest and no per-database checkpoint; if a run needs to
be re-done, drop the target schema and run it again.

## Characteristics

- Moves a transactionally consistent snapshot of InnoDB tables
  (`--single-transaction`), provided the source is quiesced for the cutover.
- Moves schema, data, routines, triggers, and events in the single stream
  (`mariadb-dump --routines --triggers --events`) — no separate finalize phase.
- Runs serially, not in parallel. This is the simplest offline mode and the one
  with the fewest knobs; for large datasets, Parallel Restartable Streaming Copy
  will finish sooner.

## Prerequisites

Each item below has a check you can run yourself. The preflight phase checks the
same things and stops the run if one fails.

- **Source is reachable and readable by the migration user.** The `mariadb-dump`
  half runs against the source with `SELECT`, `SHOW VIEW`, `TRIGGER`, `PROCESS`,
  and the transactional-snapshot privileges.
  Check: `mariadb-dump --no-data <db> >/dev/null && echo ok`
- **Target is reachable and writable, and the target databases do not already
  exist.** The phase checks this before dumping and stops with a clear message if
  a target database is present (override with `ALLOW_TARGET_DB_OVERWRITE=1`).
  Check: `mariadb -h <target> -e "SHOW DATABASES"`
- **`mariadb-dump` resolves the source host, and the `mariadb` client resolves
  the target host.** These are two separate binaries; a working launcher
  connection to one does not prove the other resolves the same name. If DNS is
  flaky, substitute the IP.
- **Use `127.0.0.1`, not `localhost`, when targeting a specific TCP port.**
  `localhost` selects the Unix socket and ignores `-P`.
- **`caching_sha2_password` accounts need TLS or an RSA key exchange.** If the
  migration user on a MySQL 8.x source uses `caching_sha2_password`, a plain TCP
  connection without TLS is refused with a generic access-denied error even when
  the credentials are correct. The tool surfaces this as a preflight warning.

## Post-load `ANALYZE TABLE`

After the stream completes, the tool runs `ANALYZE TABLE` on the target to
refresh optimizer statistics, which are empty or stale immediately after a bulk
load.

- Interactively it is an opt-out prompt ("Run ANALYZE TABLE on target after
  load? (y/n)", default `y`). On a non-interactive `migrationctl run` it is on by
  default. Set `ANALYZE_TARGET=0` to skip either way.
- Per-table `ANALYZE` errors are reported but non-fatal; only a target
  connection failure is fatal.
- A report lands at `artifacts/run_one_step_<ts>/analyze_target_report.txt`.

## Monitoring a run

- **`run.log`** under `artifacts/run_one_step_<ts>/` is the primary record — each
  phase's output, the source and target endpoints, and the final
  `FINISH success=True` line.
- The dump is streamed through `pv` when it is available (the preflight logs
  `pv found.`); its elapsed/throughput line appears in the log and on the console
  during the copy.
- The `ANALYZE TABLE` summary (tables analyzed, per-table status, overall `PASS`)
  is written to `run.log` and to `analyze_target_report.txt`.

## Verifying the result

Standard post-migration hygiene: after `FINISH success=True`, confirm row counts
match with an exact `SELECT COUNT(*)` per table on both sides. Use `COUNT(*)`
rather than `information_schema.TABLE_ROWS`, which is an InnoDB estimate and will
not match exactly.

## `max_allowed_packet`

A single row larger than the target's `max_allowed_packet` stops the load with
`ERROR 2006 (Server has gone away)`. The row cannot be split — one row is one
`INSERT`, so there is no seam to break at. MySQL 8.x defaults to 64 MB and
MariaDB to 16 MB, so a source row between those two sizes will dump cleanly and
fail on load.

Before the run phase, the tool compares the two limits. When the target has at
least twice the source's limit there is no reachable overflow and no scan runs.
Otherwise the tool scans tables holding TEXT, BLOB, or JSON columns and reports
any oversized rows, offering to raise the limit on the target:

    Raise max_allowed_packet on the target to 67108864 now? (y/n) [y]:

Answering `y` issues `SET GLOBAL max_allowed_packet` on the target. This applies
to new connections only and does not survive a restart. To persist it, set the
value under `[mysqld]` on the target and restart:

    max_allowed_packet=67108864

Answering `n` allows the run to continue. Serial Streaming Copy checks again at
preflight and exits `7` if oversized rows are still present.

## Troubleshooting

- **`WARNING: option --ssl-verify-server-cert is disabled ...` in preflight.**
  Benign — it reflects the connection's TLS/credential setup, not a failure, and
  the run proceeds.
- **`ERROR ... Access denied` on a MySQL 8.x source with the right password.**
  Usually the `caching_sha2_password`-needs-TLS case. Connect over TLS or switch
  the migration user to `mysql_native_password` on the source.
- **`Target DB already exists` (exit 8).** The phase will not run into a populated
  target. Drop the target schema and re-run, or set `ALLOW_TARGET_DB_OVERWRITE=1`
  if you intend to overwrite.
- **`SET GLOBAL FOREIGN_KEY_CHECKS=0` fails on a managed target** (RDS, MariaDB
  Cloud). Expected — global system-variable writes are blocked there; the tool
  falls back to the session-level setting and the global failure is non-fatal.
- **Host resolves for the launcher but the copy still can't connect.** The
  `mariadb-dump` and `mariadb` binaries resolve names independently of the
  launcher's own client. Substitute the IP for the failing side.
- **Stream is slower than expected on a large database.** Serial by design; use
  Parallel Restartable Streaming Copy for large datasets.

## Worked example

Prompt-free path: put the connection and scope values in `config/migration.yaml`
once, then invoke the orchestrator. Do not export `SRC_HOST`/`SRC_USER`/`SRC_PASS`
by hand — those are exported *by* the launcher to the phase scripts, not read
from your shell.

```
# config/migration.yaml (excerpt)
mode: one_step
source:
  host: 10.0.0.11          # IP, not a name, if DNS is flaky
  port: 3306
  user: migrator
target:
  host: 10.0.0.21
  port: 3306
  user: migrator
databases:
  - sakila
```

```
# quiesce the source, then run:
python3 -m orchestrator.migrationctl run

# confirm the run succeeded:
grep FINISH artifacts/run_one_step_*/run.log
#   -> FINISH success=True message=Run completed successfully.
```

If you would rather drive it interactively, run the launcher and choose:

```
1) Serial Streaming Copy (mariadb-dump)          [OFFLINE]
     Tooling : mariadb-dump (schema + data, single pass)
     Pipeline: Piped end-to-end. No disk staging on either host.
```

## Environment variables

| Variable         | Purpose                                                        | Default |
|------------------|----------------------------------------------------------------|---------|
| `ANALYZE_TARGET` | Run `ANALYZE TABLE` on the target after the load (`0` to skip). | `1`     |
| `MIGRATE_APP_USERS` | Migrate application user accounts (`1` to enable).          | off  |
| `MARIADB_DUMP_BIN` | Path to the dump binary, if not the default on `PATH`.       | (auto)  |
