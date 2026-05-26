# Changelog

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
