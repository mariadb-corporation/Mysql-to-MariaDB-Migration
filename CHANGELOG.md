# Changelog

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
