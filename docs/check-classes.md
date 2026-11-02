# Check classes

Every finding the assessment emits, and what it means for a migration.

`class` answers one question: what happens when the dump is loaded into
MariaDB.

| Class | Meaning |
|---|---|
| `LOAD_FAIL` | The load stops. The client reports the error and the target is left partly migrated. |
| `RUNTIME_FAIL` | Loads clean and the migration reports success. Fails the first time something calls it, possibly long after anyone is watching. |
| `SIGN_OFF` | Loads clean, then behaves differently. Nothing errors; someone needs to see the change and accept it. |
| `INFO` | Resolved on this target. Recorded for completeness. |

`severity` (HIGH/MEDIUM/LOW) is a separate axis, kept from before 1.6.1 so
existing consumers of `report.json` keep working. The two are not
interchangeable: a HIGH `SIGN_OFF` is silent, a MEDIUM `LOAD_FAIL` stops the
load.

**On evidence.** Where a row cites a load, the class was confirmed by
dumping a `gap_zoo` object from MySQL 8.4.7 and loading it against MariaDB
11.8.9, 12.3.3 and 13.1.1. Where it does not, the class is reasoned from the
dump or the documented behaviour and has not been loaded. Both are recorded
the same way on purpose — the Notes column is where the difference lives,
and reasoning has lost to a real load more than once on this project.

---

## Classed findings

These carry a `class` in `report.json` and render in
`load_failure_report.txt`. Defined in `FEATURES` in
`orchestrator/load_failures.py`.

| Finding | Severity | Class | Error | Notes |
|---|---|---|---|---|
| `unknown_charset` | HIGH | LOAD_FAIL | 1115 | Decided against the target's catalog, not its version. |
| `unknown_collation` | HIGH | LOAD_FAIL | 1273 | Same. Target-decided, so a collation can be LOAD_FAIL on one target and INFO on another — visible on 12.3.3 vs 13.1.1. |
| `gb18030_charset` | HIGH | LOAD_FAIL | 1115 | MariaDB has no gb18030. |
| `lang_0900_collation` | HIGH | LOAD_FAIL | 1273 | Not in MariaDB's `mysql_0900_mapping`. `ja` loaded on 11.8.9; `ru`, `zh` listed unsupported in `ctype-uca0900.c`. |
| `table_encryption` | HIGH | LOAD_FAIL | 1911 | Arrives bare and parses on every version tested, then fails at execution. Parsing proves nothing here, so it is not probed. |
| `lateral_in_object` | HIGH | LOAD_FAIL | 1064 | Routine and view bodies arrive bare; probed against the target. |
| `json_arrow_operator` | HIGH | LOAD_FAIL | 1064 | Probed. Parses on 13.1.1, not on 11.8.9 or 12.3.3 (MDEV-13594). |
| `mysql_only_operator` | HIGH | LOAD_FAIL | 1064 | `MEMBER OF`. `JSON_CONTAINS` is the usual substitute (MDEV-38591). |
| `functional_indexes` | HIGH | LOAD_FAIL | 1064 | Key part emitted bare, no version comment. Loaded on all three targets: every one aborts at the `CREATE TABLE` and leaves the schema empty, so the index is never silently dropped. Not probed — the probe needs a writable database, and the target schema does not exist at assess time. |
| `not_enforced_check` | HIGH | LOAD_FAIL | 4025 | Only where rows violate the predicate. Decided by a `COUNT` per constraint against the source, not by the constraint's existence. |
| `schema_default_encryption` | HIGH | LOAD_FAIL | — | Nothing errors: MariaDB drops the clause and tables created afterwards land unencrypted. Classed as a failure rather than a sign-off because the loss is silent and ongoing. |
| `mysql_only_function` | HIGH | RUNTIME_FAIL | 1305 | Function names resolve at execution, not parse. Verified on 12.3.3: loads, migration reports success, first call raises 1305. |
| `invisible_columns` | HIGH | SIGN_OFF | — | Clause sits inside `/*!80023 */`, which MariaDB skips (MDEV-5009), so it cannot be probed and is dropped on every version. |
| `invisible_indexes` | MEDIUM | SIGN_OFF | — | Same mechanism, `/*!80000 */`. Index becomes visible and the optimizer starts using it. |
| `srid_columns` | MEDIUM | SIGN_OFF | — | `/*!80003 */`. `SRS_ID 0` is excluded — it means no reference system was declared, so there is no constraint to lose. |
| `not_enforced_check_clean` | HIGH | SIGN_OFF | — | Loads today; enforcement switches on for future writes. |
| `general_tablespace` | MEDIUM | SIGN_OFF | — | `/*!50100 */`. Reading it needs `PROCESS` on the source; see `tablespace_check_unprivileged`. |
| `autoextend_size` | LOW | SIGN_OFF | — | `/*!80023 */`. Parses on 12.3+, but the dump hides it there too, which is why probing it would mislead. |
| `aliased_0900_collation` | LOW | INFO | — | Untagged `_0900_` collations alias to `uca1400` on the target. Loads and sorts; UCA 9.0.0 vs 14.0.0 weights can still differ. |

---

## Unclassed warnings

These emit a `WarningItem` with severity only. Defined in
`run_assessment_checks` in `orchestrator/checks.py`.

Several are already reported by the classed path under a different name. They
are left in place deliberately — removing them would change `report.json` for
anything consuming it — but a consumer counting findings will count those
objects twice. The overlap is recorded per row.

### Covered by a classed finding

| Check | Severity | Also reported as | Notes |
|---|---|---|---|
| `functional_indexes_present` | HIGH | `load_fail_functional_indexes` | Same rows, now classed. |
| `invisible_columns_present` | LOW | `sign_off_invisible_columns` | Severity disagrees with the classed finding's HIGH. |
| `gis_srid_columns_present` | MEDIUM | `sign_off_srid_columns` | The warning does not exclude `SRS_ID 0`; the classed finding does, so the counts differ by design. |
| `check_constraints_present` | MEDIUM | `load_fail_not_enforced_check`, `sign_off_not_enforced_check_clean` | The classed path splits by violating rows; the warning does not. |
| `encryption_or_compression_detected` | HIGH | `load_fail_table_encryption` | The warning also covers compression, which the classed path does not. Not a pure duplicate. |
| `server_collation_0900` | HIGH | `info_aliased_0900_collation` | Resolves as an alias on targets carrying `uca1400`, so the HIGH overstates it there. |
| `mysql8_table_collations_present` | MEDIUM | collation findings | Inventory-shaped; the classed path reports only what the target lacks. |
| `mysql8_column_collations_present` | MEDIUM | collation findings | Same. |
| `mysql_only_syntax_in_views_routines` | HIGH | `load_fail_lateral_in_object` and the other body findings | Broader net; catches syntax the classed checks do not name individually. |

### Not object-level findings

No class applies. They describe the server, the run, or the assessment
itself rather than something in the dump.

| Check | Severity | Notes |
|---|---|---|
| `sql_mode_review_recommended` | MEDIUM | Source server variable. |
| `lower_case_table_names_nonzero` | MEDIUM | Must match on the target or object resolution changes. A target configuration task, not a dump finding. |
| `event_scheduler_active` | MEDIUM | Source server setting; events migrate separately. |
| `active_plugins_review_recommended` | LOW | Inventory of loaded plugins. |
| `innodb_fast_shutdown_not_0` | MEDIUM | Precondition for inplace mode only. Procedural, not a property of the data. |
| `tablespace_check_unprivileged` | MEDIUM | Fires when the assessment user lacks `PROCESS`, meaning the general-tablespace check could not confirm its own result. A statement about the assessment, not the source. |

### Not yet classed

These are the remaining scope. None has been loaded against a target; the
class column is left empty rather than filled with a guess.

| Check | Severity | Class | Notes |
|---|---|---|---|
| `mysql_sha_or_caching_auth_users` | HIGH | — | Only reachable when application users are migrated. MariaDB has no `caching_sha2_password`; affected users would need a new auth plugin and password reset. Untested. |
| `partial_revokes_detected` | HIGH | — | Same precondition. MariaDB has no partial revokes, so the privilege set cannot be reproduced; what the load actually does is unverified. |
| `mysql_roles_in_use` | HIGH | — | Same precondition. `role_edges` does not transfer. |
| `definer_objects_present` | MEDIUM | — | Same precondition in practice: whether a `DEFINER` clause fails depends on whether that user exists on the target. Untested either way. |
| `json_columns_present` | MEDIUM | — | MySQL JSON is a binary type; MariaDB JSON is a `LONGTEXT` alias with a CHECK. Expected to load, with function and comparison behaviour differing. Untested. |
| `generated_columns_present` | HIGH | — | The column loads; the risk is the expression. A MySQL-only function inside one would behave like `mysql_only_function` — RUNTIME_FAIL, not LOAD_FAIL — but that has not been confirmed for generated columns specifically. |
| `functional_defaults_present` | MEDIUM | — | `DEFAULT (expression)`. MariaDB supports expression defaults; whether the dump's form parses is untested. |
| `partitioned_tables_present` | MEDIUM | — | MariaDB supports partitioning. Subpartitioning and `PARTITION BY KEY` algorithm differences are the likely findings, and neither has been tested. |
| `long_fk_names_detected` | MEDIUM | — | Foreign key names over 60 characters. A length limit is the kind of thing a single `CREATE TABLE` settles; no zoo object exists yet. |
| `multiple_triggers_per_event` | MEDIUM | — | MariaDB has supported multiple triggers per event since 10.2.4, so the question is `FOLLOWS`/`PRECEDES` ordering specifically. Untested. |
| `resource_groups_in_use` | MEDIUM | — | No MariaDB equivalent. Not in 1.6.1 scope and never tested. This is the check the original ticket proposed a fourth class (`DECISION`) for; whether one is needed depends on what a load actually does. |
| `xplugin_active` | HIGH | — | X Protocol on port 33060. Nothing in the dump carries it — the finding is about applications connecting over it, which no class describes well. May belong with the server-level checks above rather than here. |

---

## Adding a check

A class is a claim about what MariaDB does with the dump, so it needs the
dump to answer it. The sequence that settled `functional_indexes`:

1. `SHOW CREATE TABLE` on the source. If the construct is inside a
   `/*!8xxxx */` comment, MariaDB skips it (MDEV-5009) and the class is
   `SIGN_OFF` on every version — target support is irrelevant and probing
   would mislead. If it is bare, the parser sees it and the target decides.
2. Add an object to `tests/fixtures/gap_zoo.sql` with the expected class in
   a comment above it. Isolate it in its own schema first if the zoo's
   existing failures would stop the load before reaching it.
3. Load against each sandbox. Record the error number and whether anything
   landed on the target.
4. Where the construct is bare and the target could legitimately differ, add
   a probe in `orchestrator/probes.py` so the class follows the target
   rather than a version table.
5. Add the entry to `FEATURES`, and to `SIGN_OFF_LAYOUT` if the finding
   comes from its own TSV.

Step 3 is the one that cannot be skipped. On this project it has overturned
the predicted answer often enough to be worth the sandbox time: `SRS_ID 0`
turned out not to be a finding at all, three functions assumed missing were
present, and a collation expected to fail everywhere resolved as an alias.
