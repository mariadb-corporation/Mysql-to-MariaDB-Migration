/*
not_enforced_constraints.sql

CHECK constraints the source carries as NOT ENFORCED. MariaDB has no
unenforced CHECK: the constraint arrives enforced, and if the table already
holds rows the predicate rejects, the load fails with 4025 at that table.

Rows here are candidates, not failures. Whether any row actually violates the
predicate takes a COUNT per constraint, which orchestrator/load_failures.py
runs against the source; a constraint with no violating rows loads fine and is
reported as informational.

ENFORCED (TABLE_CONSTRAINTS) arrived in 8.0.16 and CHECK_CONSTRAINTS in the
same release, so both are probed before the real query runs. On an older
source the result is an empty set of the same shape and assessment continues,
matching the pattern in check_constraints.sql.

Columns (keep in sync with orchestrator/load_failures.py):
  1 constraint_schema  -- MUST stay first: _filter_by_schema() scopes on it
  2 table_name
  3 constraint_name
  4 check_clause
*/

SET @has_enforced := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'TABLE_CONSTRAINTS'
    AND COLUMN_NAME  = 'ENFORCED'
);

SET @has_cc := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'CHECK_CONSTRAINTS'
);

SET @sql := IF(@has_enforced > 0 AND @has_cc > 0,
  'SELECT tc.CONSTRAINT_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
     FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
     JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
       ON cc.CONSTRAINT_NAME   = tc.CONSTRAINT_NAME
      AND cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
    WHERE tc.CONSTRAINT_TYPE = ''CHECK''
      AND tc.ENFORCED = ''NO''
      AND tc.CONSTRAINT_SCHEMA NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY tc.CONSTRAINT_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME',
  'SELECT NULL, NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
